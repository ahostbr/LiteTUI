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
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from litetui import runtime_log
from litetui.plugins import PluginManifest

PLUGIN_ID = "glassbox"

#: Frames a subscriber may fall behind before it starts losing them. Roughly
#: two seconds of the busiest realistic stream — long enough to ride out a
#: repaint, short enough that a dead tab does not hoard memory.
QUEUE_MAX = 256

#: Loopback only, always. This publishes what the model is thinking about.
HOST = "127.0.0.1"
DEFAULT_PORT = 7580
MAX_CLIENTS = 32
STOP_SECONDS = 2.0


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

    def do_GET(self) -> None:
        if self.path.split("?")[0] != "/events":
            self.send_error(404)
            return
        self.send_response(200)
        for k, v in sse_headers().items():
            self.send_header(k, v)
        self.end_headers()

        hub = self.server.hub
        with self.server.clients_lock:
            if self.server.stopping.is_set():
                return
            q = hub.subscribe()
            self.server.subscribers.add(q)
        try:
            while not self.server.stopping.is_set():
                try:
                    event = q.get(timeout=15.0)
                    if event is None or self.server.stopping.is_set():
                        break
                    self.wfile.write(sse_frame(event).encode("utf-8"))
                except queue.Empty:
                    # A comment frame. Without it an idle feed looks identical
                    # to a dead one to every intermediary and to the browser's
                    # own reconnect logic.
                    if self.server.stopping.is_set():
                        break
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                    # the tab closed. normal.
        finally:
            with self.server.clients_lock:
                self.server.subscribers.discard(q)
            hub.unsubscribe(q)

    def log_message(self, *_args) -> None:
        """Silence. The stdlib default writes to stderr, which in this app is
        a TUI — see the standing rule about never spraying a terminal."""


class _OwnedHTTPServer(ThreadingHTTPServer):
    """Retain accepted sockets and handlers until bounded joined retirement."""

    block_on_close = False

    def __init__(self, address):
        super().__init__(address, _Handler)
        self.timeout = 0.1
        self.stopping = threading.Event()
        self.hub = HUB
        self.clients: dict[threading.Thread, socket.socket] = {}
        self.clients_lock = threading.Lock()
        self.subscribers: set[queue.Queue] = set()

    def process_request(self, request, client_address):
        with self.clients_lock:
            self.clients = {t: sock for t, sock in self.clients.items() if t.is_alive()}
            if self.stopping.is_set() or len(self.clients) >= MAX_CLIENTS:
                self.shutdown_request(request)
                return
            request.settimeout(0.25)
            thread = threading.Thread(
                target=self.process_request_thread,
                args=(request, client_address),
                name="glassbox-client",
                daemon=True,
            )
            self.clients[thread] = request
            try:
                thread.start()
            except RuntimeError:
                self.clients.pop(thread, None)
                self.shutdown_request(request)
                raise

    def run(self):
        try:
            while not self.stopping.is_set():
                self.handle_request()
        except (OSError, ValueError):
            if not self.stopping.is_set():
                runtime_log.record_error(
                    "glassbox_listener_failed",
                    detail="Listener loop ended unexpectedly",
                )
            self.stopping.set()

    def handle_error(self, _request, _address):
        runtime_log.record_error(
            "glassbox_handler_failed", detail="An owned request handler failed"
        )

    def stop_clients(self):
        with self.clients_lock:
            clients = tuple(self.clients.items())
            subscribers = tuple(self.subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(None)
            except queue.Full:
                pass  # A reader with queued data observes stopping before its next write.
        for _thread, connection in clients:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        return tuple(thread for thread, _ in clients)


class Server:
    """Retain the exact listener and request threads until every owner joins."""

    def __init__(self) -> None:
        self._httpd: _OwnedHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.port: int | None = None
        self.owner = None
        self.instance: str | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return (
                self._httpd is not None
                and not self._httpd.stopping.is_set()
                and self._httpd.socket.fileno() >= 0
                and self._thread is not None
                and self._thread.is_alive()
            )

    def snapshot(self) -> dict:
        with self._lock:
            state = (
                "running"
                if self.running
                else "stopped"
                if self._httpd is None
                else "stopping"
            )
            return {
                "state": state,
                "port": self.port if state == "running" else None,
                "listener": self.instance,
            }

    def start(self, port: int | None = None, *, owner=None) -> str:
        with self._lock:
            if self._httpd is not None:
                if self.owner is not owner:
                    return "[error] listener belongs to another app"
                if self.running:
                    return f"already serving on http://{HOST}:{self.port}/events"
                return "[error] previous listener is still stopping"
            port = DEFAULT_PORT if port is None else port
            try:
                listener = _OwnedHTTPServer((HOST, port))
            except OSError as exc:
                runtime_log.record_error(
                    "glassbox_bind_failed",
                    detail=f"bind {HOST}:{port}: {type(exc).__name__}: {exc}",
                    exc=exc,
                )
                return "[error] could not bind the glassbox listener"
            thread = threading.Thread(
                target=listener.run, name="glassbox-sse", daemon=True
            )
            self._httpd, self._thread = listener, thread
            self.owner, self.instance = owner, str(uuid.uuid4())
            self.port = listener.server_address[1]
            try:
                thread.start()
            except RuntimeError:
                listener.server_close()
                self._httpd = self._thread = None
                self.owner = self.instance = self.port = None
                return "[error] could not start the glassbox listener worker"
            return f"serving http://{HOST}:{self.port}/events"

    def stop(self, *, owner=None) -> str:
        with self._lock:
            if self._httpd is None:
                return "not running"
            if owner is not None and self.owner is not owner:
                return "[error] listener belongs to another app"
            listener, thread = self._httpd, self._thread
            listener.stopping.set()
            listener.server_close()
            deadline = time.monotonic() + STOP_SECONDS
            clients = listener.stop_clients()
            if thread is not None:
                thread.join(max(0, deadline - time.monotonic()))
            # The joined accept loop makes this the final admitted set.
            clients = tuple(set(clients) | set(listener.stop_clients()))
            for client in clients:
                client.join(max(0, deadline - time.monotonic()))
            if (
                thread is not None
                and thread.is_alive()
                or any(t.is_alive() for t in clients)
            ):
                return "[error] glassbox shutdown retains unfinished workers"
            self._httpd = self._thread = None
            self.owner = self.instance = None
            port, self.port = self.port, None
            return f"stopped (was {HOST}:{port})"

SERVER = Server()


def cached_status() -> dict:
    """Sample the in-process recording choice and actual retained listener."""
    listener = SERVER._httpd
    port = None
    if listener is not None:
        host, port = listener.server_address
        if host != HOST or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Glassbox listener has an unexpected address")
        stopping = getattr(listener, "stopping", None)
        if stopping is not None and stopping.is_set():
            port = None
    return {"recording": _ENABLED, "port": port}


def _cmd_glassbox(app, name: str, arg: str) -> None:
    global _ENABLED, _CONTROL_REVISION
    retained = getattr(app, "_glassbox_owned_server", None)
    if (
        _RECORDER_OWNER is not app
        or SERVER._httpd is not None
        and SERVER.owner is not app
        or retained is not None
        and retained._httpd is not None
        and retained is not SERVER
    ):
        raise ValueError(
            "Glassbox registration belongs to another app or retained listener"
        )
    action = (arg or "").strip().lower()
    if action in ("start", "on", ""):
        app._glassbox_owned_server = SERVER
        _ENABLED = True
        _CONTROL_REVISION += 1
        app.system_message(f"[glassbox] {SERVER.start(owner=app)}")
        app.system_message(f"[glassbox] recording to {_RECORDER.path}")
    elif action in ("stop", "off"):
        _ENABLED = False
        _CONTROL_REVISION += 1
        app.system_message(f"[glassbox] {SERVER.stop(owner=app)}")
    elif action == "status":
        where = (
            f"http://{HOST}:{SERVER.port}/events" if SERVER.running else "not running"
        )
        app.system_message(
            f"[glassbox] {where}\n[glassbox] recording to {_RECORDER.path}"
        )
    else:
        app.system_message("[glassbox] usage: /glassbox [start|stop|status]")

_RECORDER = Recorder(Path("glassbox.jsonl"))
_RECORDER_OWNER = None
_CONTROL_REVISION = 0


def register(ctx, root: Path | None = None) -> None:
    """One observer, two sinks. Registered unconditionally: the recorder is
    cheap and the hub is free until someone subscribes."""
    global _RECORDER, _RECORDER_OWNER, _ENABLED
    if SERVER.owner is not None and SERVER.owner is not ctx.app:
        raise ValueError("Another app owns the Glassbox listener")
    if _RECORDER_OWNER is not ctx.app:
        _ENABLED = False
    _RECORDER_OWNER = ctx.app
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
        if not _ENABLED or _RECORDER_OWNER is not ctx.app:
            return
        _RECORDER.write(event)
        HUB.publish(event)

    ctx.observe(_on_event)
    ctx.command(
        ("glassbox", "/glassbox"), _cmd_glassbox,
        palette="Glass box feed",
        help="start/stop the live SSE feed the brain page reads",
        group="automation",
    )


def stop_owned(app):
    """Disable this app's recorder and join only its exact retained listener."""
    global _ENABLED, _CONTROL_REVISION, _RECORDER_OWNER
    if _RECORDER_OWNER is app and _ENABLED:
        _ENABLED = False
        _CONTROL_REVISION += 1
    retained = getattr(app, "_glassbox_owned_server", None)
    server = retained if retained is not None else SERVER
    if server.owner is app:
        server.stop(owner=app)
        if server._httpd is not None:
            raise RuntimeError(
                "Glassbox retains unfinished listener or handler workers"
            )
    app._glassbox_owned_server = None
    if _RECORDER_OWNER is app:
        _RECORDER_OWNER = None

def _default_root(ctx) -> Path:
    try:
        from litetui import paths
        return paths.data_root() / "artifacts"
    except Exception:
        return Path.cwd()


PLUGIN = PluginManifest(id=PLUGIN_ID, register=register)
