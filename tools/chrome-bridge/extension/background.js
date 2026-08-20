// PyBridge — MV3 service worker.
// Connects OUT to a Python WebSocket server on localhost and executes commands.
// The extension is the client so the Python side can be a short-lived script.

const WS_URL = "ws://127.0.0.1:7429";
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
