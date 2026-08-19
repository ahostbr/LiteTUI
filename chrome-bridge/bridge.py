"""PyBridge - drive Chrome from a plain Python script.

Python is the WebSocket SERVER; the extension's service worker is an
auto-reconnecting CLIENT. That inversion is the point: your script can be
short-lived. Start it, the extension connects within a second, do the work,
exit. Nothing to daemonize.

    from bridge import Chrome

    with Chrome() as c:
        c.navigate("https://example.com")
        print(c.text()[:400])
        c.click("a")

Requires the unpacked extension in ./extension to be loaded in Chrome.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import errno
import itertools
import json
import os
import struct
import threading
import time

from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7429  # the extension always dials this one
DEFAULT_CLIENT_PORT = 7430  # scripts dial this when a relay owns 7429
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))
SHOT_DIR = os.path.join(HERE, "shots")
PID_FILE = os.path.join(HERE, "relay.pid")

_IN_USE = {errno.EADDRINUSE, 10048}


class ChromeError(RuntimeError):
    """The extension executed the command and it failed inside the browser."""


class Chrome:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        connect_timeout: float = 15.0,
        call_timeout: float = 45.0,
        relay_port: int = DEFAULT_CLIENT_PORT,
    ):
        self.host = host
        self.port = port
        self.relay_port = relay_port
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout
        self.mode: str | None = None  # "direct" or "relay", set by start()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws = None
        self._pending: dict[int, concurrent.futures.Future] = {}
        self._ids = itertools.count(1)
        self._connected = threading.Event()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._error: BaseException | None = None

    # ---------------------------------------------------------- lifecycle

    def start(self) -> "Chrome":
        self._thread = threading.Thread(target=self._run, name="pybridge", daemon=True)
        self._thread.start()

        if not self._ready.wait(10.0):
            self.close()
            raise TimeoutError("bridge server failed to start")
        if self._error is not None:
            raise self._error

        if not self._connected.wait(self.connect_timeout):
            # Release the port before propagating. Without this the listener
            # outlives the failed start() and the next attempt on the same port
            # dies with EADDRINUSE instead of the real "extension not loaded".
            self.close()
            raise TimeoutError(
                "no extension connected to ws://{}:{} within {}s - is the unpacked "
                "extension loaded and enabled?".format(
                    self.host, self.port, self.connect_timeout
                )
            )
        return self

    def close(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def __enter__(self) -> "Chrome":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ commands

    def ping(self) -> dict:
        return self._call("ping")

    def tabs(self) -> list[dict]:
        return self._call("tabs")

    def navigate(
        self,
        url: str,
        tab_id: int | None = None,
        new_tab: bool = False,
        timeout_ms: int = 30000,
    ) -> dict:
        """Point a tab at url and wait for the load to complete."""
        return self._call(
            "navigate", url=url, tab_id=tab_id, new_tab=new_tab, timeout_ms=timeout_ms
        )

    def content(self, selector: str | None = None, tab_id: int | None = None) -> dict:
        """Return url, title, text and html for the page or one selector subtree."""
        return self._call("content", selector=selector, tab_id=tab_id)

    def text(self, selector: str | None = None, tab_id: int | None = None) -> str:
        return self.content(selector=selector, tab_id=tab_id)["text"]

    def html(self, selector: str | None = None, tab_id: int | None = None) -> str:
        return self.content(selector=selector, tab_id=tab_id)["html"]

    def click(
        self,
        selector: str | None = None,
        x: float | None = None,
        y: float | None = None,
        tab_id: int | None = None,
    ) -> dict:
        """Click by CSS selector, or by viewport coordinates when x and y are given."""
        return self._call("click", selector=selector, x=x, y=y, tab_id=tab_id)

    def screenshot(
        self,
        path: str | None = None,
        tab_id: int | None = None,
        format: str = "png",
        quality: int | None = None,
        restore: bool = True,
    ) -> dict:
        """Capture the visible viewport of a tab and write it to a PNG/JPEG file.

        Only the ACTIVE tab of a window can be captured, so naming a background
        tab_id briefly brings that tab forward. restore=True puts the previously
        active tab back afterwards.

        Returns {path, bytes, width, height, tab_id, url, title, restored}.
        Note width/height are the CAPTURED PIXELS, which on a HiDPI display are
        devicePixelRatio times the CSS viewport that click() coordinates use.
        """
        if format not in ("png", "jpeg"):
            raise ValueError("format must be 'png' or 'jpeg'")

        res = self._call(
            "screenshot", tab_id=tab_id, format=format, quality=quality, restore=restore
        )

        data_url = res.pop("data_url", "")
        _, _, b64 = data_url.partition(",")
        if not b64:
            raise ChromeError("capture returned no image data")
        raw = base64.b64decode(b64)

        if path is None:
            os.makedirs(SHOT_DIR, exist_ok=True)
            ext = "png" if format == "png" else "jpg"
            path = os.path.join(SHOT_DIR, time.strftime("shot-%Y%m%d-%H%M%S.") + ext)
        else:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)

        with open(path, "wb") as f:
            f.write(raw)

        w, h = _image_size(raw)
        res.update({"path": path, "bytes": len(raw), "width": w, "height": h})
        return res

    # -------------------------------------------------------------- plumbing

    def _call(self, cmd: str, **args):
        if self._ws is None and not self._connected.wait(self.connect_timeout):
            raise RuntimeError("extension is not connected")

        mid = next(self._ids)
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._pending[mid] = fut
        payload = json.dumps({"id": mid, "cmd": cmd, "args": args})

        try:
            asyncio.run_coroutine_threadsafe(self._send(payload), self._loop).result(10)
        except Exception:
            self._pending.pop(mid, None)
            raise

        try:
            msg = fut.result(self.call_timeout)
        except concurrent.futures.TimeoutError:
            self._pending.pop(mid, None)
            raise TimeoutError(
                "{} timed out after {}s".format(cmd, self.call_timeout)
            ) from None

        if not msg.get("ok"):
            raise ChromeError(msg.get("error", "unknown error"))
        return msg.get("result")

    async def _send(self, payload: str) -> None:
        if self._ws is None:
            raise RuntimeError("extension is not connected")
        await self._ws.send(payload)

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # surface bind errors back to start()
            self._error = exc
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        # Two ways to reach the extension, chosen by whoever owns port 7429.
        #   direct: nothing is listening, so we become the server the extension
        #           dials. Short-lived scripts work with no daemon.
        #   relay:  a `bridge.py serve` already owns it and is holding the
        #           extension connection open, so we attach to it as a client.
        # The caller sees the same API either way; only `mode` differs.
        bind_failed = False
        try:
            # max_size defaults to 1 MiB. A screenshot arrives as a base64 data
            # URL and blows straight past that — websockets then closes the
            # connection with 1009 and the call looks like a browser hang
            # rather than a size limit. Raise it above any full-window PNG.
            async with serve(
                self._handler, self.host, self.port, max_size=MAX_MESSAGE_BYTES
            ):
                self.mode = "direct"
                self._ready.set()
                await self._stop.wait()
        except OSError as exc:
            if getattr(exc, "errno", None) not in _IN_USE:
                raise
            bind_failed = True

        if bind_failed:
            await self._run_relay_client()

        # Fail anything still in flight rather than let callers hang to timeout.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("bridge closed"))
        self._pending.clear()

    async def _run_relay_client(self) -> None:
        url = "ws://{}:{}".format(self.host, self.relay_port)
        try:
            async with ws_connect(url, max_size=MAX_MESSAGE_BYTES) as ws:
                self._ws = ws
                self.mode = "relay"
                self._connected.set()
                self._ready.set()
                reader = asyncio.create_task(self._read_replies(ws))
                try:
                    await self._stop.wait()
                finally:
                    reader.cancel()
        except OSError as exc:
            # 7429 was taken but 7430 refused: something else owns the port, or
            # the relay died between our bind attempt and this connect.
            self._error = RuntimeError(
                "port {} is in use but no relay answered on {} - is another "
                "program using it? ({})".format(self.port, self.relay_port, exc)
            )
            self._ready.set()
        finally:
            self._ws = None
            self._connected.clear()

    async def _read_replies(self, ws) -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception:
            pass

    async def _handler(self, ws) -> None:
        # One extension connection is expected. A second one (another profile, or
        # a duplicate install) takes over, which is why replies are matched by id.
        self._ws = ws
        self._connected.set()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        finally:
            if self._ws is ws:
                self._ws = None
                self._connected.clear()


class Relay:
    """Persistent middle-man: holds the extension connection open forever.

    Without this, every script binds 7429 itself and the extension spends the
    gaps between scripts being refused - which Chrome logs at the network layer
    once per retry, and which costs each script up to one backoff interval
    before it can start. With a relay running, the extension connects once and
    stays connected: no refusals to log, and scripts attach instantly.
    """

    KEEPALIVE_SECONDS = 20  # under the ~30s MV3 idle eviction window

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        client_port: int = DEFAULT_CLIENT_PORT,
        quiet: bool = False,
    ):
        self.host = host
        self.port = port
        self.client_port = client_port
        self.quiet = quiet

        self._ext = None
        self._clients: dict[int, tuple] = {}  # relay id -> (client ws, its id)
        self._ids = itertools.count(1)

    def log(self, *parts) -> None:
        if not self.quiet:
            print("[relay]", *parts, flush=True)

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        finally:
            try:
                if os.path.exists(PID_FILE):
                    with open(PID_FILE, encoding="utf-8") as f:
                        if f.read().strip() == str(os.getpid()):
                            os.remove(PID_FILE)
            except OSError:
                pass

    async def _main(self) -> None:
        async with serve(
            self._ext_handler, self.host, self.port, max_size=MAX_MESSAGE_BYTES
        ), serve(
            self._client_handler, self.host, self.client_port, max_size=MAX_MESSAGE_BYTES
        ):
            # Stamp the pidfile ONLY once both ports are ours. Writing it before
            # the bind meant a relay that LOST the race still overwrote a
            # working relay's pid, then died — leaving relayctl pointed at a
            # corpse. It would then report "stale pidfile cleared" and leave the
            # real relay running: a kill that claims success and kills nothing.
            try:
                with open(PID_FILE, "w", encoding="utf-8") as f:
                    f.write(str(os.getpid()))
            except OSError:
                pass

            self.log("extension port {}  |  script port {}".format(self.port, self.client_port))
            self.log("waiting for the extension...")
            asyncio.create_task(self._keepalive())
            await asyncio.Future()  # run until killed

    async def _ext_handler(self, ws) -> None:
        self._ext = ws
        self.log("extension connected")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                entry = self._clients.pop(msg.get("id"), None)
                if entry is None:
                    continue  # keepalive reply, or a client that went away
                client_ws, original_id = entry
                msg["id"] = original_id
                try:
                    await client_ws.send(json.dumps(msg))
                except Exception:
                    pass
        finally:
            if self._ext is ws:
                self._ext = None
            self.log("extension disconnected")

    async def _client_handler(self, ws) -> None:
        self.log("script attached")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                original_id = msg.get("id")

                if self._ext is None:
                    await ws.send(
                        json.dumps(
                            {
                                "id": original_id,
                                "ok": False,
                                "error": "no extension connected to the relay",
                            }
                        )
                    )
                    continue

                # Two scripts can both send id 1. Rewrite onto a relay-wide
                # sequence and restore the caller's id on the way back, so
                # concurrent clients cannot collide.
                relay_id = next(self._ids)
                self._clients[relay_id] = (ws, original_id)
                msg["id"] = relay_id
                try:
                    await self._ext.send(json.dumps(msg))
                except Exception as exc:
                    self._clients.pop(relay_id, None)
                    await ws.send(
                        json.dumps(
                            {"id": original_id, "ok": False, "error": str(exc)}
                        )
                    )
        finally:
            stale = [k for k, (cws, _) in self._clients.items() if cws is ws]
            for k in stale:
                self._clients.pop(k, None)
            self.log("script detached")

    async def _keepalive(self) -> None:
        # An idle WebSocket does not stop MV3 from evicting the worker. Eviction
        # is not fatal (the alarm reconnects) but it reopens the window where a
        # script finds nobody home, which is the thing the relay exists to close.
        while True:
            await asyncio.sleep(self.KEEPALIVE_SECONDS)
            if self._ext is None:
                continue
            try:
                await self._ext.send(
                    json.dumps({"id": next(self._ids), "cmd": "ping", "args": {}})
                )
            except Exception:
                pass


def _image_size(raw: bytes) -> tuple[int | None, int | None]:
    """Width/height straight from the file header. No Pillow dependency."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        w, h = struct.unpack(">II", raw[16:24])
        return int(w), int(h)

    if raw[:2] == b"\xff\xd8":  # JPEG: walk segments to a start-of-frame marker
        i, n = 2, len(raw)
        while i + 9 < n:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", raw[i + 5 : i + 9])
                return int(w), int(h)
            if i + 4 > n:
                break
            i += 2 + struct.unpack(">H", raw[i + 2 : i + 4])[0]

    return None, None


# ------------------------------------------------------------------- CLI

def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="bridge", description="Drive Chrome from Python.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = p.add_subparsers(dest="cmd", required=True)

    sv = sub.add_parser(
        "serve", help="run the persistent relay (keeps the extension connected)"
    )
    sv.add_argument("--client-port", type=int, default=DEFAULT_CLIENT_PORT)

    sub.add_parser("ping", help="check the extension is connected")
    sub.add_parser("tabs", help="list open tabs")

    n = sub.add_parser("nav", help="navigate a tab to a url")
    n.add_argument("url")
    n.add_argument("--new-tab", action="store_true")

    t = sub.add_parser("text", help="print page text")
    t.add_argument("selector", nargs="?")

    c = sub.add_parser("click", help="click an element")
    c.add_argument("selector", nargs="?")
    c.add_argument("--x", type=float)
    c.add_argument("--y", type=float)

    s = sub.add_parser("shot", help="screenshot the visible viewport")
    s.add_argument("path", nargs="?")
    s.add_argument("--tab-id", type=int)
    s.add_argument("--jpeg", action="store_true", help="JPEG instead of PNG")
    s.add_argument("--quality", type=int, help="JPEG quality 0-100")

    a = p.parse_args(argv)

    if a.cmd == "serve":
        try:
            Relay(port=a.port, client_port=a.client_port).run()
        except KeyboardInterrupt:
            print("\n[relay] stopped")
        return 0

    with Chrome(port=a.port) as ch:
        if a.cmd == "ping":
            print(json.dumps(dict(ch.ping(), mode=ch.mode)))
        elif a.cmd == "tabs":
            for row in ch.tabs():
                title = (row["title"] or "")[:60]
                print("{:>6}  {:<60}  {}".format(row["id"], title, row["url"]))
        elif a.cmd == "nav":
            print(json.dumps(ch.navigate(a.url, new_tab=a.new_tab)))
        elif a.cmd == "text":
            print(ch.text(a.selector))
        elif a.cmd == "click":
            print(json.dumps(ch.click(a.selector, x=a.x, y=a.y)))
        elif a.cmd == "shot":
            shot = ch.screenshot(
                path=a.path,
                tab_id=a.tab_id,
                format="jpeg" if a.jpeg else "png",
                quality=a.quality,
            )
            print(json.dumps(shot))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
