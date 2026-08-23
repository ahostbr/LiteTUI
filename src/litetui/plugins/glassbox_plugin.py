"""Glass box: the turn, published as signal.

Tier 1 of docs/Plans/2026-08-21-glass-box-thermal-brain/wiring.md. app.py fires
six channels plus one ambient; this plugin is the only thing that listens, and
it turns them into two transports:

  JSONL  the durable record — what you read AFTER a turn to ask what happened.
         No port, no lifecycle, always on. One line per event.
  SSE    the live feed on 127.0.0.1, which is the only transport that can
         animate anything. Started on demand by `/glassbox`.

🔴 THE BROWSER MUST NEVER BE ABLE TO STALL THE TUI. publish() is called from
inside the stream loop, once per tool call and up to five times a second per
continuous channel. Subscribers are bounded queues and a FULL ONE IS DROPPED
rather than waited on: a brain page on a backgrounded tab, or a laptop with its
lid shut, must cost dropped frames and never a frozen agent. Losing frames from
an ambient visualisation is free. Blocking the turn is not.

Why SSE and not a websocket or a poll: the page is served from file://, so it
has no origin a websocket handshake would satisfy cleanly, and a poll cannot
show a token-rate flicker. SSE is one long GET with a wildcard CORS header —
and that header is the whole difference between live and inert, because without
it the browser discards the response before any JS runs and the page shows
nothing with no error anywhere in the app.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from litetui.plugins import PluginManifest

PLUGIN_ID = "glassbox"

#: Frames a subscriber may fall behind before it starts losing them. Roughly
#: two seconds of the busiest realistic stream — long enough to ride out a
#: repaint, short enough that a dead tab does not hoard memory.
QUEUE_MAX = 256

#: Loopback only, always. This publishes what the model is thinking about.
HOST = "127.0.0.1"
DEFAULT_PORT = 7580


class Hub:
    """Fan-out to any number of SSE clients, from the app's thread.

    A set of bounded queues behind a lock. The lock is held only for the
    put_nowait loop, which never blocks, so the stream loop's worst case is the
    cost of N non-blocking puts.
    """

    def __init__(self) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = tuple(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # DROP. See the module docstring: a slow reader costs frames,
                # never the turn. Deliberately silent — a backgrounded tab is
                # the common case, not an error worth telling anyone about.
                pass


class Recorder:
    """Append-only JSONL. Best effort by law: this runs in the stream loop."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def write(self, event: dict) -> None:
        row = {"ts": time.time(), **event}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                # ensure_ascii=False so the ledger's arrows and minus signs
                # read back as themselves rather than as escapes.
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            # A full disk, a locked file, a path the OS refuses. None of it is
            # worth ending a turn over, and there is nowhere to report it from
            # inside an observer that must not raise.
            pass


HUB = Hub()

#: Recording + fan-out are OFF until /glassbox start. See _on_event.
_ENABLED = False


# ── SSE wire format ─────────────────────────────────────────────────────────
def sse_headers() -> dict[str, str]:
    """The brain page is file://, so its Origin is `null`. A wildcard ACAO is
    the difference between a live feed and a page that silently shows nothing.
    Loopback-only bind is what makes the wildcard acceptable."""
    return {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
        # Chrome will otherwise buffer a proxied event stream until it has a
        # chunk worth flushing, which turns a live feed into a stutter.
        "X-Accel-Buffering": "no",
    }


def sse_frame(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:                      # noqa: N802 — stdlib name
        if self.path.split("?")[0] != "/events":
            self.send_error(404)
            return
        self.send_response(200)
        for k, v in sse_headers().items():
            self.send_header(k, v)
        self.end_headers()

        q = HUB.subscribe()
        try:
            while True:
                try:
                    event = q.get(timeout=15.0)
                    self.wfile.write(sse_frame(event).encode("utf-8"))
                except queue.Empty:
                    # A comment frame. Without it an idle feed looks identical
                    # to a dead one to every intermediary and to the browser's
                    # own reconnect logic.
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                    # the tab closed. normal.
        finally:
            HUB.unsubscribe(q)

    def log_message(self, *_args) -> None:
        """Silence. The stdlib default writes to stderr, which in this app is
        a TUI — see the standing rule about never spraying a terminal."""


class Server:
    """Start/stop, idempotent in both directions."""

    def __init__(self) -> None:
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self, port: int = DEFAULT_PORT) -> str:
        if self._httpd is not None:
            return f"already serving on http://{HOST}:{self.port}/events"
        try:
            self._httpd = ThreadingHTTPServer((HOST, port), _Handler)
        except OSError as e:
            self._httpd = None
            return f"[error] cannot bind {HOST}:{port} — {e}"
        self.port = port
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="glassbox-sse", daemon=True
        )
        self._thread.start()
        return f"serving http://{HOST}:{port}/events"

    def stop(self) -> str:
        if self._httpd is None:
            return "not running"
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        self._thread = None
        port, self.port = self.port, None
        return f"stopped (was {HOST}:{port})"


SERVER = Server()


def _cmd_glassbox(app, name: str, arg: str) -> None:
    global _ENABLED
    action = (arg or "").strip().lower()
    if action in ("start", "on", ""):
        _ENABLED = True
        app._system(f"[glassbox] {SERVER.start()}")
        app._system(f"[glassbox] recording to {_RECORDER.path}")
    elif action in ("stop", "off"):
        _ENABLED = False
        app._system(f"[glassbox] {SERVER.stop()}")
    elif action == "status":
        where = f"http://{HOST}:{SERVER.port}/events" if SERVER.running else "not running"
        app._system(f"[glassbox] {where}\n[glassbox] recording to {_RECORDER.path}")
    else:
        app._system("[glassbox] usage: /glassbox [start|stop|status]")


_RECORDER = Recorder(Path("glassbox.jsonl"))


def register(ctx, root: Path | None = None) -> None:
    """One observer, two sinks. Registered unconditionally: the recorder is
    cheap and the hub is free until someone subscribes."""
    global _RECORDER
    base = Path(root) if root is not None else _default_root(ctx)
    _RECORDER = Recorder(base / "glassbox.jsonl")

    def _on_event(event: dict) -> None:
        # OFF UNTIL ASKED. Registering the observer is free; WRITING is not —
        # this runs in the stream loop up to 5x/sec per continuous channel plus
        # every tool call, and a chat app must not pay file I/O for telemetry
        # nobody switched on.
        #
        # Caught by its own test: `costs nothing when nobody is watching`
        # passed in isolation and failed in the full suite, because a real app
        # loads this plugin and therefore ALWAYS had an observer. The property
        # the test named was true of the registry and false of the product.
        # Trade accepted: no post-hoc record of a turn you did not anticipate.
        if not _ENABLED:
            return
        _RECORDER.write(event)
        HUB.publish(event)

    ctx.observe(_on_event)
    ctx.command(
        ("glassbox",), _cmd_glassbox,
        palette="Glass box feed",
        help="start/stop the live SSE feed the brain page reads",
        group="automation",
    )


def _default_root(ctx) -> Path:
    try:
        from litetui import paths
        return Path(paths.ROOT) / "artifacts"
    except Exception:
        return Path.cwd()


PLUGIN = PluginManifest(id=PLUGIN_ID, register=register)
