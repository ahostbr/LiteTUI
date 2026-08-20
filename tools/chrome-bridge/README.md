# PyBridge — drive Chrome from a Python script

A Chrome MV3 extension plus a Python module. Your script navigates tabs, reads page
contents, and clicks elements. No `--remote-debugging-port`, no CDP, no MCP server.

## Why the extension is the client

Python runs the WebSocket **server** on `127.0.0.1:7429`; the extension's service worker
connects **out** to it and retries forever.

That inversion is the whole design. If Python were the client it would need the browser to
be listening first, so you would need a long-lived daemon. Instead your script is the
server: run it, the extension connects within about a second, do the work, exit. When the
script dies the extension just goes back to retrying.

## Install (once)

1. `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. **Load unpacked** → `C:\Projects\chrome-bridge\extension`

To confirm: `python bridge.py ping` → `{"pong": true, "version": "0.3.0"}`.
The reported version comes from the RUNNING worker, so it is also how you tell whether a
reload actually took effect after editing `extension/`.

## Kill the idle errors: run the relay

Left alone, the extension dials 7429 every few seconds and Chrome logs
`ERR_CONNECTION_REFUSED` each time no script is running. That is not a fault - it is what
an outbound client does when nobody is home - but it is noisy, and it costs each script up
to one backoff interval (~5s) before it can start.

Run a persistent relay and both problems disappear:

```bash
python bridge.py serve
```

The relay owns 7429 and holds the extension connection open forever, so there are no
refusals to log. Scripts need no changes: `Chrome()` tries to bind 7429, finds the relay
there, and attaches to port 7430 as a client instead. Measured on a real browser:

| | no relay | relay running |
|---|---|---|
| script connect time | ~5.3 s | **~0.003 s** |
| idle console errors | one per retry, forever | **none** |

Concurrent scripts are safe: the relay rewrites message ids onto its own sequence and
restores each caller's id on the way back, so two clients that both start counting at 1
cannot receive each other's replies.

### relayctl - the control TUI

```bash
python relayctl.py
```

```
  PyBridge relay C:\Projects\chrome-bridge
  --------------------------------------------------------------------
    status   RUNNING  extension v0.3.0  up 14s
    pid      204852
    ports    7429 extension   7430 scripts
  --------------------------------------------------------------------
    log (relay.out.log)
      [relay] extension connected
      [relay] script attached
  --------------------------------------------------------------------
    s start   k kill   r restart   c clear log   q quit
```

Three states, not two, because "relay up" and "extension attached" fail separately:

| badge | meaning |
|---|---|
| `RUNNING` | relay up **and** the extension answered a real ping |
| `NO EXTENSION` | relay up, extension has not dialled in yet (transient after a restart) |
| `STOPPED` | nothing listening |

Liveness is probed by speaking the actual protocol to port 7430, **not** by trusting
`relay.pid` — a stale pidfile and a live relay look identical otherwise. It deliberately
does not use `Chrome()` for the probe: `Chrome()` binds 7429 when it is free, so a
down relay would leave the monitor holding the very port it was checking and reporting
itself healthy.

`start` launches the relay detached (`DETACHED_PROCESS`), so it outlives the TUI and never
inherits its console.

### Starting it by hand instead

To run it detached on Windows so it survives the terminal that started it:

```powershell
Start-Process python -ArgumentList 'C:\Projects\chrome-bridgeridge.py','serve' `
  -WindowStyle Hidden -RedirectStandardOutput relay.out.log -RedirectStandardError relay.err.log
```

## Use

```python
from bridge import Chrome

with Chrome() as c:
    c.navigate("https://example.com")       # waits for load to complete
    print(c.text())                         # visible text of the page
    print(c.text("h1"))                     # ...or of one selector's subtree
    c.click("a")                            # click by CSS selector
    c.click(x=400, y=300)                   # ...or by viewport coordinates
```

| Method | Returns |
|---|---|
| `ping()` | `{pong, version}` — `Chrome.mode` is `"direct"` or `"relay"` |
| `tabs()` | list of `{id, url, title, active, windowId, status}` |
| `navigate(url, tab_id=None, new_tab=False, timeout_ms=30000)` | `{tab_id, url, title}` |
| `content(selector=None, tab_id=None)` | `{url, title, text, html}` |
| `text(selector=None)` / `html(selector=None)` | `str` |
| `click(selector=None, x=None, y=None, tab_id=None)` | `{clicked, tag, text, x, y}` |
| `screenshot(path=None, tab_id=None, format="png", quality=None, restore=True)` | `{path, bytes, width, height, tab_id, url, title, restored}` |

Omit `tab_id` and it targets the active tab of the last focused window.

CLI, same surface:

```bash
python bridge.py tabs
python bridge.py nav https://example.com --new-tab
python bridge.py text h1
python bridge.py click "a.more"
python bridge.py shot                  # -> shots/shot-YYYYmmdd-HHMMSS.png
python bridge.py shot out.jpg --jpeg --quality 60
```

## Tests

```bash
python selftest.py    # transport only: fake extension, no Chrome needed
python demo.py        # real browser, requires the extension loaded
```

`selftest.py` proves the Python half — bind, handshake, id-matched replies, error
propagation, call timeout — against a fake peer. **It does not prove any `chrome.*` call
works.** Only `demo.py` against a real browser does that. Keep those two claims separate.

## Limits worth knowing before you hit them

- **Restricted pages refuse injection.** `chrome://*`, the Web Store, and other
  extensions' pages cannot be scripted; `content` and `click` raise there. `navigate` and
  `tabs` still work.
- **`click` is a synthetic DOM event**, not an OS-level click. It fires
  pointerdown/mousedown/pointerup/mouseup/click on the element. That drives real web apps
  fine, but it is not what a page sees from a hardware mouse — `isTrusted` is `false`.
  For anything that checks that, use `pccontrol.py` instead.
- **Coordinates are viewport-relative**, not screen-relative. They are not the same
  numbers `pccontrol` uses.
- **`screenshot` cannot capture restricted pages at all** — `chrome://*`, the Web Store,
  and other extensions. `<all_urls>` does not cover those schemes, so Chrome falls back to
  `activeTab`, which a scripted extension never holds (it is only granted after a user
  clicks the extension icon). Chrome's own error blames `activeTab` and sends you hunting
  through permissions; the bridge rewrites it to name the URL instead. **Proven with a
  control**: `chrome://version` refused while an http tab in the same run captured fine.
- **`screenshot` captures the VISIBLE VIEWPORT only** — not the full scrollable page, and
  not a background tab. `chrome.tabs.captureVisibleTab` photographs the active tab of a
  window and there is no API for anything else, so naming a background `tab_id` briefly
  brings that tab forward. `restore=True` (the default) puts the previously active tab
  back, but the user will see the flicker. Capturing the already-active tab is silent.
- **Screenshot pixels are not CSS pixels.** The returned `width`/`height` are the captured
  bitmap, which on a HiDPI display is `devicePixelRatio` times the CSS viewport. Do not
  feed a coordinate read off a screenshot straight into `click(x=, y=)` without dividing.
- **A capture crosses the socket as a base64 data URL**, so the server raises the
  `websockets` frame cap to 64 MiB. The default is 1 MiB, which a full-window PNG exceeds
  — and the failure presents as a dropped connection, not as a size error. Use
  `format="jpeg", quality=60` for something much smaller.
- **One extension connection.** A second Chrome profile with the extension installed will
  take over the socket. Replies are matched by id, so an in-flight call still resolves,
  but subsequent commands go to whoever connected last.
- **MV3 workers get evicted after ~30s idle.** A `chrome.alarms` tick every 30s wakes the
  worker and reconnects, so the first call after a long gap can take a moment. Nothing to
  configure; it is why `alarms` is in the manifest.
- **The extension logs `ERR_CONNECTION_REFUSED` while no script is running, and that
  cannot be fully suppressed.** Chrome logs a refused WebSocket at the network layer, not
  from JS, so the `try/catch` around `new WebSocket()` never sees it. Reconnects back off
  1s → 2s → 4s → 5s (capped) to keep it to a trickle rather than one per second. The cap
  is deliberately 5s and not higher: it is *also* the worst-case wait before a freshly
  started script gets a connection. Errors on the extension card while idle are expected,
  not a fault. **Running `bridge.py serve` removes them entirely** — see above. A Chrome
  restart does not and cannot help: restarting Chrome restarts the *client*; what is
  missing is the *server*.
- **`navigate` waits for `status === "complete"`**, which is the load event, not
  network-idle. A single-page app that renders after that will need a poll on `text()`.

## Files

```
extension/manifest.json    MV3 manifest, permissions: tabs, scripting, alarms
extension/background.js    service worker: WS client, command dispatch, injected funcs
bridge.py                  Python: WS server, sync Chrome class, CLI
selftest.py                transport self-test against a fake extension
demo.py                    real-browser smoke test
relayctl.py                TUI: start / kill / restart / monitor the relay
shots/                     default output directory for screenshot()
relay.pid, relay.*.log     written by the relay; managed by relayctl
```
