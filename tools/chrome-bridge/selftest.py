"""Transport self-test: exercises bridge.py against a FAKE extension.

This proves the Python half end to end - server bind, connect handshake,
id-matched request/reply, error propagation, timeout - without Chrome.
It does NOT prove the extension's chrome.* calls work; only a real browser
does that. Keep the two claims separate.

Run:  python selftest.py
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

from websockets.asyncio.client import connect

from bridge import Chrome, ChromeError, Relay, _image_size

PORT = 7530  # deliberately not the default, so a real extension cannot answer


def make_png(w: int, h: int) -> bytes:
    """A real, valid PNG of known dimensions - so the size check is meaningful."""
    import struct
    import zlib

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def make_jpeg_header(w: int, h: int) -> bytes:
    """Enough of a JPEG for the SOF0 walk. Not a decodable image."""
    import struct

    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x00" * 9
    filler = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    return b"\xff\xd8" + filler + sof


class FakeExtension:
    """Answers the same protocol the service worker answers."""

    def __init__(self, port: int, deaf_cmds: set[str] | None = None):
        self.port = port
        self.deaf_cmds = deaf_cmds or set()
        self.seen: list[dict] = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stop = threading.Event()

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        asyncio.run(self._main())

    async def _main(self):
        for _ in range(100):  # wait for the server to come up
            try:
                ws = await connect("ws://127.0.0.1:{}".format(self.port))
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            return

        async with ws:
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    return
                msg = json.loads(raw)
                self.seen.append(msg)
                cmd, args = msg["cmd"], msg.get("args", {})

                if cmd in self.deaf_cmds:
                    continue  # simulate a hung browser: never reply

                if cmd == "ping":
                    out = {"id": msg["id"], "ok": True, "result": {"pong": True}}
                elif cmd == "navigate":
                    out = {
                        "id": msg["id"],
                        "ok": True,
                        "result": {"tab_id": 7, "url": args["url"], "title": "Fake"},
                    }
                elif cmd == "content":
                    out = {
                        "id": msg["id"],
                        "ok": True,
                        "result": {
                            "url": "https://fake/",
                            "title": "Fake",
                            "text": "hello from the fake page",
                            "html": "<p>hello</p>",
                        },
                    }
                elif cmd == "screenshot":
                    if args.get("tab_id") == -1:  # negative control: no pixels
                        out = {
                            "id": msg["id"],
                            "ok": True,
                            "result": {"data_url": "", "tab_id": -1},
                        }
                    else:
                        import base64 as _b64

                        url = "data:image/png;base64," + _b64.b64encode(
                            make_png(40, 30)
                        ).decode()
                        out = {
                            "id": msg["id"],
                            "ok": True,
                            "result": {
                                "data_url": url,
                                "tab_id": 7,
                                "url": "https://fake/",
                                "title": "Fake",
                                "restored": False,
                            },
                        }
                elif cmd == "click":
                    if args.get("selector") == "#missing":
                        out = {
                            "id": msg["id"],
                            "ok": False,
                            "error": "selector not found: #missing",
                        }
                    else:
                        out = {
                            "id": msg["id"],
                            "ok": True,
                            "result": {"clicked": True, "tag": "button"},
                        }
                else:
                    out = {"id": msg["id"], "ok": False, "error": "unknown cmd: " + cmd}

                await ws.send(json.dumps(out))


def check(label, fn):
    try:
        fn()
    except AssertionError as e:
        print("  FAIL  {}: {}".format(label, e))
        return False
    except Exception as e:
        print("  FAIL  {}: {}: {}".format(label, type(e).__name__, e))
        return False
    print("  ok    {}".format(label))
    return True


def main() -> int:
    results = []

    # ---- negative control FIRST: with no extension, start() must time out.
    t0 = time.time()
    try:
        Chrome(port=PORT, connect_timeout=1.0).start()
        results.append(False)
        print("  FAIL  no-extension: start() succeeded with nothing connected")
    except TimeoutError:
        print("  ok    no-extension: start() raised TimeoutError in {:.1f}s".format(time.time() - t0))
        results.append(True)

    # ---- happy path against the fake
    ext = FakeExtension(PORT).start()
    c = Chrome(port=PORT, connect_timeout=10.0, call_timeout=5.0).start()

    results.append(check("ping round-trips", lambda: _eq(c.ping()["pong"], True)))
    results.append(
        check(
            "navigate carries the url through",
            lambda: _eq(c.navigate("https://example.com")["url"], "https://example.com"),
        )
    )
    results.append(
        check("text() unwraps content.text", lambda: _eq(c.text(), "hello from the fake page"))
    )
    results.append(check("click ok", lambda: _eq(c.click("button")["clicked"], True)))
    results.append(
        check("browser-side error becomes ChromeError", lambda: _raises(ChromeError, lambda: c.click("#missing")))
    )
    results.append(
        check("unknown cmd becomes ChromeError", lambda: _raises(ChromeError, lambda: c._call("bogus")))
    )

    # ---- screenshot: decode, write, measure
    import tempfile

    shot_path = os.path.join(tempfile.mkdtemp(prefix="pybridge-"), "shot.png")
    shot = c.screenshot(path=shot_path)
    results.append(check("screenshot writes the file", lambda: _eq(os.path.exists(shot_path), True)))
    results.append(check("screenshot bytes match on disk", lambda: _eq(os.path.getsize(shot_path), shot["bytes"])))
    results.append(check("screenshot dimensions parsed", lambda: _eq((shot["width"], shot["height"]), (40, 30))))
    results.append(
        check(
            "empty capture becomes ChromeError",
            lambda: _raises(ChromeError, lambda: c.screenshot(tab_id=-1)),
        )
    )
    results.append(
        check("bad format rejected before the wire", lambda: _raises(ValueError, lambda: c.screenshot(format="webp")))
    )

    # ---- header parser, directly (PNG proven above via the wire; JPEG here)
    results.append(check("_image_size reads PNG", lambda: _eq(_image_size(make_png(12, 34)), (12, 34))))
    results.append(
        check("_image_size reads JPEG", lambda: _eq(_image_size(make_jpeg_header(640, 480)), (640, 480)))
    )
    results.append(check("_image_size gives up cleanly", lambda: _eq(_image_size(b"not an image"), (None, None))))

    # ---- ids are unique and every request was actually observed by the peer
    ids = [m["id"] for m in ext.seen]
    results.append(check("request ids unique", lambda: _eq(len(ids), len(set(ids)))))
    results.append(check("peer saw every request", lambda: _ge(len(ext.seen), 6)))

    c.close()
    ext.stop()

    # ---- a silent browser must time out, not hang forever
    ext2 = FakeExtension(PORT + 1, deaf_cmds={"content"}).start()
    c2 = Chrome(port=PORT + 1, connect_timeout=10.0, call_timeout=1.0).start()
    results.append(
        check("silent browser -> TimeoutError", lambda: _raises(TimeoutError, lambda: c2.content()))
    )
    results.append(check("bridge still usable after a timeout", lambda: _eq(c2.ping()["pong"], True)))
    c2.close()
    ext2.stop()

    # ---- relay mode: a persistent middle-man owns 7461, scripts attach to 7462
    RPORT, RCLIENT = 7540, 7541
    threading.Thread(
        target=Relay(port=RPORT, client_port=RCLIENT, quiet=True).run, daemon=True
    ).start()
    time.sleep(0.5)

    c3 = Chrome(port=RPORT, relay_port=RCLIENT, connect_timeout=10.0, call_timeout=5.0).start()
    results.append(check("relay mode auto-detected", lambda: _eq(c3.mode, "relay")))
    results.append(
        check(
            "relay with no extension errors instead of hanging",
            lambda: _raises(ChromeError, c3.ping),
        )
    )

    ext3 = FakeExtension(RPORT).start()
    time.sleep(1.0)
    results.append(check("relay round-trips once the extension attaches", lambda: _eq(c3.ping()["pong"], True)))

    # Two FRESH clients both start their id counters at 1. Fired concurrently,
    # their first calls are both id 1 - so if the relay did not rewrite ids,
    # one would receive the other's reply. Distinct urls make a crossed reply
    # visible rather than merely possible.
    c4 = Chrome(port=RPORT, relay_port=RCLIENT, connect_timeout=10.0, call_timeout=5.0).start()
    c5 = Chrome(port=RPORT, relay_port=RCLIENT, connect_timeout=10.0, call_timeout=5.0).start()
    out: dict[str, str] = {}

    def hit(name, cli, url):
        try:
            out[name] = cli.navigate(url)["url"]
        except Exception as exc:
            out[name] = "ERR:{}".format(exc)

    ta = threading.Thread(target=hit, args=("a", c4, "https://aaa.test"))
    tb = threading.Thread(target=hit, args=("b", c5, "https://bbb.test"))
    ta.start()
    tb.start()
    ta.join(15)
    tb.join(15)
    results.append(
        check(
            "concurrent clients get their OWN replies",
            lambda: _eq(
                (out.get("a"), out.get("b")), ("https://aaa.test", "https://bbb.test")
            ),
        )
    )

    c3.close()
    c4.close()
    c5.close()
    ext3.stop()

    passed, total = sum(results), len(results)
    print("\n{}/{} passed".format(passed, total))
    return 0 if passed == total else 1


def _eq(got, want):
    assert got == want, "expected {!r}, got {!r}".format(want, got)


def _ge(got, want):
    assert got >= want, "expected >= {!r}, got {!r}".format(want, got)


def _raises(exc_type, fn):
    try:
        fn()
    except exc_type:
        return
    except Exception as e:
        raise AssertionError(
            "expected {}, got {}: {}".format(exc_type.__name__, type(e).__name__, e)
        )
    raise AssertionError("expected {}, nothing raised".format(exc_type.__name__))


if __name__ == "__main__":
    raise SystemExit(main())
