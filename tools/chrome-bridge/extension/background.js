// PyBridge — MV3 service worker.
// Connects OUT to a Python WebSocket server on localhost and executes commands.
// The extension is the client so the Python side can be a short-lived script.

// Must match bridge.py DEFAULT_PORT. 7461, not 7429: LiteSound's python audio
// engine owns 7429 and answered these dials with a 404 for months.
const WS_URL = "ws://127.0.0.1:7461";
const RETRY_MIN_MS = 1000;
// 5s, not 30s: this ceiling is also the worst-case delay before a freshly
// started script gets a connection, so it trades directly against the whole
// point of the tool. 5s cuts the error spam ~5x while staying well inside the
// 15s default connect_timeout.
const RETRY_MAX_MS = 5000;

let ws = null;
let retryMs = RETRY_MIN_MS;

// ---------------------------------------------------------------- transport

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    ws = null;
    return;
  }

  ws.onopen = () => {
    retryMs = RETRY_MIN_MS;
    console.log("[pybridge] connected to", WS_URL);
  };

  ws.onclose = () => {
    ws = null;
    // A refused connection is logged by Chrome's NETWORK layer, not by JS, so
    // the try/catch above cannot suppress it. Retrying every second therefore
    // means one console error per second and a permanently red extension badge
    // whenever no script is running. Back off instead: the cost of a slower
    // reconnect is one wasted second on the next run, and the alarm below is
    // the real floor anyway.
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
  };

  ws.onerror = () => {
    try { ws.close(); } catch (e) { /* already closing */ }
  };

  ws.onmessage = async (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch (e) {
      return;
    }
    let reply;
    try {
      reply = { id: msg.id, ok: true, result: await handle(msg.cmd, msg.args || {}) };
    } catch (e) {
      reply = { id: msg.id, ok: false, error: String((e && e.message) || e) };
    }
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(reply));
  };
}

// An MV3 worker is evicted after ~30s idle. WebSocket traffic resets that timer
// (Chrome 116+), but nothing resets it while the server is DOWN — so the alarm
// is the only thing that gets us reconnected after the worker dies.
chrome.alarms.create("pybridge-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();

// ---------------------------------------------------------------- dispatch

async function handle(cmd, a) {
  switch (cmd) {
    case "ping":
      return { pong: true, version: chrome.runtime.getManifest().version };

    case "tabs": {
      const tabs = await chrome.tabs.query({});
      return tabs.map((t) => ({
        id: t.id, url: t.url, title: t.title,
        active: t.active, windowId: t.windowId, status: t.status,
      }));
    }

    case "navigate":
      return await navigate(a.url, a.tab_id, a.new_tab, a.timeout_ms || 30000);

    case "content":
      return await inject(await resolveTab(a.tab_id), readPage, [a.selector || null]);

    case "click":
      return await inject(await resolveTab(a.tab_id), clickPage, [
        a.selector || null,
        a.x === undefined ? null : a.x,
        a.y === undefined ? null : a.y,
      ]);

    case "write":
      return await inject(await resolveTab(a.tab_id), writePage, [
        a.selector || null,
        a.text === undefined ? "" : String(a.text),
        a.clear !== false,
        a.enter === true,
      ]);

    case "screenshot":
      return await screenshot(
        a.tab_id,
        a.format || "png",
        a.quality === undefined ? null : a.quality,
        a.restore !== false
      );

    default:
      throw new Error("unknown cmd: " + cmd);
  }
}

async function resolveTab(tabId) {
  if (tabId) return tabId;
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("no active tab to target");
  return tab.id;
}

async function navigate(url, tabId, newTab, timeoutMs) {
  if (!url) throw new Error("navigate requires a url");

  let id;
  if (newTab) {
    // create() starts loading immediately, so attach the waiter around it.
    const created = await chrome.tabs.create({ url });
    id = created.id;
    await waitComplete(id, timeoutMs);
  } else {
    id = await resolveTab(tabId);
    // Attach the listener BEFORE triggering the nav — otherwise a fast load can
    // fire "complete" before we are listening and we wait out the full timeout.
    const done = waitComplete(id, timeoutMs);
    await chrome.tabs.update(id, { url });
    await done;
  }

  const t = await chrome.tabs.get(id);
  return { tab_id: id, url: t.url, title: t.title };
}

function waitComplete(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error("navigate timed out after " + timeoutMs + "ms"));
    }, timeoutMs);

    function listener(id, info) {
      if (id === tabId && info.status === "complete") {
        cleanup();
        resolve();
      }
    }
    function cleanup() {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function isRestricted(url) {
  const u = String(url || "");
  return (
    /^(chrome|chrome-extension|devtools|edge|about|view-source|chrome-untrusted):/i.test(u) ||
    /^https:\/\/chromewebstore\.google\.com/i.test(u) ||
    /^https:\/\/chrome\.google\.com\/webstore/i.test(u)
  );
}

async function screenshot(tabId, format, quality, restore) {
  let target;
  if (tabId) {
    target = await chrome.tabs.get(tabId);
  } else {
    const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (!t) throw new Error("no active tab to capture");
    target = t;
  }

  const windowId = target.windowId;
  let previousActiveId = null;

  // captureVisibleTab photographs the ACTIVE tab of a window and nothing else —
  // there is no way to capture a background tab. So the target has to be brought
  // forward. That is a visible side effect on someone's browser, so remember
  // what was in front and put it back.
  if (!target.active) {
    const [wasActive] = await chrome.tabs.query({ active: true, windowId });
    previousActiveId = wasActive ? wasActive.id : null;
    await chrome.tabs.update(target.id, { active: true });
    await new Promise((r) => setTimeout(r, 200)); // let the compositor paint
  }

  const opts = { format };
  if (format === "jpeg" && quality !== null) opts.quality = quality;

  try {
    let dataUrl;
    try {
      dataUrl = await chrome.tabs.captureVisibleTab(windowId, opts);
    } catch (e) {
      // Chrome blames activeTab here ("has not been in invoked"), which sends
      // you looking at permissions. The real cause is almost always the URL:
      // <all_urls> does not cover chrome://, the Web Store, or other extensions,
      // so capture falls back to activeTab — which a scripted extension never
      // holds, because activeTab requires a user gesture on the icon.
      if (isRestricted(target.url)) {
        throw new Error(
          "cannot capture a restricted page (" +
            String(target.url).split("/").slice(0, 3).join("/") +
            ") - <all_urls> does not cover it and activeTab needs a user click"
        );
      }
      throw e;
    }
    const fresh = await chrome.tabs.get(target.id);
    return {
      data_url: dataUrl,
      tab_id: target.id,
      url: fresh.url,
      title: fresh.title,
      restored: previousActiveId !== null && restore,
    };
  } finally {
    if (restore && previousActiveId !== null && previousActiveId !== target.id) {
      try {
        await chrome.tabs.update(previousActiveId, { active: true });
      } catch (e) {
        /* the tab was closed while we were capturing */
      }
    }
  }
}

async function inject(tabId, func, args) {
  const frames = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  const res = frames && frames[0];
  if (!res) throw new Error("injection produced no result (restricted page?)");
  if (res.result && res.result.error) throw new Error(res.result.error);
  return res.result;
}

// ------------------------------------------------- injected into the page
// These run in the page's world. They are serialized to source, so they cannot
// close over anything above — everything they need arrives via args.

function readPage(selector) {
  const root = selector ? document.querySelector(selector) : document.body;
  if (!root) return { error: "selector not found: " + selector };
  return {
    url: location.href,
    title: document.title,
    text: (root.innerText || "").trim(),
    html: root.innerHTML,
  };
}

// Type into a field the way a person does — and, more importantly, the way a
// FRAMEWORK notices.
//
// 🔴 THE REACT TRAP, which is why `el.value = x` is not enough. React installs
// its OWN `value` setter on the element instance and remembers the last value
// it wrote. Assigning `el.value` goes through that setter, so React's tracker
// concludes nothing changed, ignores the input event, and reverts the field on
// the next render. The field visibly fills and then empties, which reads as
// "the site rejected it" rather than "we wrote it wrong". Calling the
// PROTOTYPE's native setter bypasses the instance property, so the tracker sees
// a genuine change. Vue and Svelte have the same shape.
function writePage(selector, text, clear, pressEnter) {
  const el = selector ? document.querySelector(selector) : document.activeElement;
  if (!el || el === document.body) {
    return {
      error: selector
        ? "selector not found: " + selector
        : "no focused element — pass a selector, or click the field first",
    };
  }

  el.scrollIntoView({ block: "center", inline: "center" });
  el.focus();

  const tag = el.tagName.toLowerCase();

  if (el.isContentEditable) {
    if (clear) el.textContent = "";
    // execCommand is deprecated and still the only thing that inserts into a
    // rich editor with the caret and events those editors actually listen for.
    const ok = document.execCommand("insertText", false, text);
    if (!ok) el.textContent = (clear ? "" : el.textContent) + text;
    el.dispatchEvent(
      new InputEvent("input", { bubbles: true, data: text, inputType: "insertText" })
    );
  } else if (tag === "select") {
    el.value = text;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    if (el.value !== text) {
      return { error: "no <option> with value " + JSON.stringify(text) };
    }
  } else if (tag === "input" || tag === "textarea") {
    const proto =
      tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    const next = clear ? text : (el.value || "") + text;
    if (setter) setter.call(el, next);
    else el.value = next;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    return {
      error:
        "<" + tag + "> is not writable — needs an input, textarea, select, or " +
        "contenteditable element",
    };
  }

  if (pressEnter) {
    // Key events only. A real form.requestSubmit() here would double-submit any
    // form that already acts on Enter, and a duplicate order is worse than a
    // form that needs its button clicked.
    const k = {
      bubbles: true, cancelable: true,
      key: "Enter", code: "Enter", keyCode: 13, which: 13,
    };
    el.dispatchEvent(new KeyboardEvent("keydown", k));
    el.dispatchEvent(new KeyboardEvent("keypress", k));
    el.dispatchEvent(new KeyboardEvent("keyup", k));
  }

  // Echo what the field HOLDS, not just that we wrote. It is read back
  // immediately, so a framework that reverts on its next render will still
  // look fine here — confirm a form with a `text` read when it matters.
  return {
    written: true,
    tag,
    value: String(el.value ?? el.textContent ?? "").slice(0, 200),
  };
}

function clickPage(selector, x, y) {
  let el = null;
  if (selector) {
    el = document.querySelector(selector);
    if (!el) return { error: "selector not found: " + selector };
  } else if (x !== null && y !== null) {
    el = document.elementFromPoint(x, y);
    if (!el) return { error: "no element at viewport point " + x + "," + y };
  } else {
    return { error: "click needs either a selector or x and y" };
  }

  el.scrollIntoView({ block: "center", inline: "center" });
  const r = el.getBoundingClientRect();
  const cx = x !== null ? x : r.left + r.width / 2;
  const cy = y !== null ? y : r.top + r.height / 2;
  const opts = {
    bubbles: true, cancelable: true, view: window,
    clientX: cx, clientY: cy, button: 0, buttons: 1,
  };

  el.dispatchEvent(new PointerEvent("pointerdown", opts));
  el.dispatchEvent(new MouseEvent("mousedown", opts));
  el.dispatchEvent(new PointerEvent("pointerup", opts));
  el.dispatchEvent(new MouseEvent("mouseup", opts));
  el.dispatchEvent(new MouseEvent("click", opts));

  return {
    clicked: true,
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.value || "").trim().slice(0, 120),
    x: cx, y: cy,
  };
}
