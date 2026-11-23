"""A loopback filtering forward proxy for the shell tools' child processes (T1418, layer B).

LiteTUI starts it on demand inside its own process (threads, no child process, nothing
to orphan, no console) and sets HTTP_PROXY / HTTPS_PROXY / ALL_PROXY for powershell and
bash children. It is CONNECT-aware, resolves a name once, checks EVERY address with the
same `egress_policy`, and connects to the address it checked.

Covers what honours the proxy variables (curl, pwsh 7 Invoke-WebRequest, git, pip, npm,
python requests, hf, most CLIs). It does NOT cover a program that ignores them or opens a
raw socket: only the generated firewall script (a file Ryan runs himself) does.
"""
from __future__ import annotations

import atexit
import socket
import threading
from urllib.parse import urlsplit

from litetui import egress_guard, egress_policy as ep

_HEAD_MAX = 64 * 1024
_CONNECT_TIMEOUT_S = 15.0
_READ_HEAD_TIMEOUT_S = 30.0


class EgressProxy:
    def __init__(self) -> None:
        self._srv: socket.socket | None = None
        self._lock = threading.Lock()
        self._live: set[socket.socket] = set()
        self.port = 0
        self.refused = 0
        self.passed = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self._srv is not None

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(64)
        self._srv, self.port = srv, srv.getsockname()[1]
        threading.Thread(target=self._accept, args=(srv,), name="egress-proxy", daemon=True).start()

    def stop(self) -> None:
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        with self._lock:
            live, self._live = list(self._live), set()
        for s in live:
            try:
                s.close()
            except OSError:
                pass

    # -- plumbing -------------------------------------------------------------

    def _accept(self, srv: socket.socket) -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), name="egress-proxy-conn", daemon=True).start()

    def _track(self, s: socket.socket) -> None:
        with self._lock:
            self._live.add(s)

    def _untrack(self, *socks: socket.socket) -> None:
        with self._lock:
            for s in socks:
                self._live.discard(s)
        for s in socks:
            try:
                s.close()
            except OSError:
                pass

    @staticmethod
    def _reply(conn: socket.socket, status: str, body: str, headers: str = "") -> None:
        data = body.encode("utf-8", "replace")
        head = (f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n{headers}\r\n")
        try:
            conn.sendall(head.encode("ascii", "replace") + data)
        except OSError:
            pass

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _read_head(self, conn: socket.socket) -> tuple[bytes, bytes] | None:
        conn.settimeout(_READ_HEAD_TIMEOUT_S)
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(8192)
                if not chunk:
                    return None
                buf += chunk
                if len(buf) > _HEAD_MAX:
                    return None
        except OSError:
            return None
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    def _open_remote(self, conn: socket.socket, host: str, port: int) -> socket.socket | None:
        pol = ep.current()
        v = pol.check_host(host)
        if v.blocked:
            self.refused += 1
            egress_guard.record("shell", v, via="proxy")
            self._reply(conn, "403 LiteTUI egress blocked",
                        f"LiteTUI egress block: {host} is blocked ({v.rule}; data: {v.source}, as of {v.data_date}). "
                        "This is the user's setting; do not route around it.\n",
                        headers="X-LiteTUI-Egress: blocked\r\n")
            return None
        err: OSError | None = None
        for addr in v.addresses:
            try:
                remote = socket.create_connection((addr, port), _CONNECT_TIMEOUT_S)
                remote.settimeout(None)
                return remote
            except OSError as exc:
                err = exc
        self._reply(conn, "502 Bad Gateway", f"LiteTUI proxy could not reach {host}:{port} ({err or 'did not resolve'})\n")
        return None

    def _serve(self, conn: socket.socket) -> None:
        self._track(conn)
        remote = None
        try:
            got = self._read_head(conn)
            if got is None:
                return
            head, rest = got
            lines = head.split(b"\r\n")
            parts = lines[0].decode("latin-1").split()
            if len(parts) != 3:
                self._reply(conn, "400 Bad Request", "bad request line\n")
                return
            method, target, version = parts
            conn.settimeout(None)
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                if not host:
                    host, port = target, "443"
                host = host.strip("[]")
                remote = self._open_remote(conn, host, int(port) if port.isdigit() else 443)
                if remote is None:
                    return
                self._track(remote)
                self.passed += 1
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    remote.sendall(rest)
            else:
                u = urlsplit(target)
                if not u.scheme or not u.hostname:
                    self._reply(conn, "400 Bad Request", "this is a proxy: send an absolute URI\n")
                    return
                try:
                    port = u.port or (443 if u.scheme == "https" else 80)
                except ValueError:
                    self._reply(conn, "400 Bad Request", "bad port\n")
                    return
                remote = self._open_remote(conn, u.hostname, port)
                if remote is None:
                    return
                self._track(remote)
                self.passed += 1
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                keep = [ln for ln in lines[1:]
                        if not ln.lower().startswith((b"proxy-connection:", b"proxy-authorization:", b"connection:"))]
                out = [f"{method} {path} {version}".encode("latin-1"), *keep, b"Connection: close", b"", b""]
                remote.sendall(b"\r\n".join(out) + rest)
            t = threading.Thread(target=self._pump, args=(conn, remote), daemon=True)
            t.start()
            self._pump(remote, conn)
            t.join(timeout=5)
        except (OSError, ValueError):
            pass
        finally:
            self._untrack(*(s for s in (conn, remote) if s is not None))


_lock = threading.Lock()
_proxy: EgressProxy | None = None
#: Why the last start failed ("" when it did not), shown by `/egress status`.
last_error = ""


def ensure_started() -> EgressProxy | None:
    global _proxy, last_error
    with _lock:
        if _proxy is not None and _proxy.running:
            return _proxy
        p = EgressProxy()
        try:
            p.start()
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            egress_guard.record("shell", ep.Verdict(False, rule=f"proxy did not start: {last_error}"), via="proxy-error")
            return None
        last_error = ""
        _proxy = p
        return p


def current() -> EgressProxy | None:
    return _proxy if _proxy is not None and _proxy.running else None


def shutdown() -> None:
    global _proxy
    with _lock:
        p, _proxy = _proxy, None
    if p is not None:
        p.stop()


atexit.register(shutdown)
