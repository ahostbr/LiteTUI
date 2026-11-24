"""Where the egress policy meets the tools (T1418).

`egress_policy` decides; this module enforces at the choke points and writes the
words and the log line:

* `scan_call`   : the pre-dispatch scan `App._execute_tool` runs on every tool call.
* `Pins` + `build_opener` : `web_fetch`'s pinned, redirect-checked HTTP.
* `refusal` / `record`    : the sentence the model reads and the `logs/egress.jsonl` line.
* `proxy_env`   : the loopback proxy's environment for the shell tools' children.

TOOL PATHS ONLY. Model backends never call anything here (the user, 2026-11-23).
"""
from __future__ import annotations

import http.client
import json
import re
import shlex
import socket
import ssl
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

from litetui import egress_policy as ep
from litetui.egress_policy import EgressBlocked, EgressPolicy, Verdict

# ── the log line ─────────────────────────────────────────────────────────────

#: Overridable by tests. Resolved lazily: `paths.data_root()` is the one place this app keeps its data.
LOG_PATH: Path | None = None
_log_lock = threading.Lock()


def log_path() -> Path:
    if LOG_PATH is not None:
        return LOG_PATH
    from litetui import paths
    return paths.data_root() / "logs" / "egress.jsonl"


def _host_only(target: str) -> str:
    """A target for the log and the refusal: the host, never a path or a query (secrets live there)."""
    t = str(target or "").strip()
    hosts = ep.url_hosts(t) if "://" in t else []
    return (hosts[0] if hosts else t)[:200]


def record(tool: str, verdict: Verdict, *, via: str = "scan") -> None:
    """One JSON line: time, tool, target host, rule, resolved address. No prompt text. Never raises."""
    row = {
        "t": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": tool, "via": via, "target": _host_only(verdict.target),
        "rule": verdict.rule, "kind": verdict.kind, "ip": verdict.ip,
        "data_date": verdict.data_date,
    }
    try:
        p = log_path()
        with _log_lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def refusal(tool: str, verdict: Verdict, *, app=None) -> str:
    """The refusal in words: what, which rule, the data and its date."""
    from litetui.textfmt import tool_denied
    data = verdict.source or "built-in list"
    if verdict.data_date:
        data += f", as of {verdict.data_date}"
    return tool_denied("egress-blocked", app=app, name=tool, target=_host_only(verdict.target),
                       rule=verdict.rule or "blocked", data=data)


# ── the pre-dispatch scan ────────────────────────────────────────────────────

#: Argument values that are executed. The whole text is scanned, as a shell would read it.
EXEC_KEYS = frozenset({"command", "cmd", "script", "code"})
#: Argument names whose value is, by name, a place to connect to.
HOST_KEYS = frozenset({
    "url", "uri", "href", "link", "endpoint", "base_url", "baseurl", "host", "hostname", "server",
    "address", "ip", "target", "domain", "website", "site", "origin", "proxy", "remote", "repo",
    "repository", "source", "destination", "dest", "urls", "hosts", "uris",
})
NET_COMMANDS = frozenset({
    "curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod", "scp", "ssh", "sftp", "rsync",
    "nc", "ncat", "netcat", "telnet", "tnc", "test-netconnection", "ping", "nslookup", "resolve-dnsname",
    "git", "pip", "pip3", "npm", "pnpm", "yarn", "bun", "hf", "huggingface-cli", "ftp", "tftp", "smbclient",
    "aria2c", "axel", "http", "https", "httpie", "start-bitstransfer", "bitsadmin", "certutil", "tracert",
    "pathping", "docker", "podman", "uv", "pipx", "conda", "cargo", "go", "gh", "nuget", "dotnet",
})

_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s'\"<>`]+")
_HOSTLIKE_RE = re.compile(r"(?<![\w.@/\\-])([\w-]+(?:[.。．｡][\w-]+)+)(?![\w-])", re.U)
_V6_RE = re.compile(r"(?<![\w:])(?:(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F:.]*|::[0-9a-fA-F:.]+)")
_SPLIT_RE = re.compile(r"[;&|\n\r]+")
_URLISH_VALUE = re.compile(r"(?i)^[a-z][a-z0-9+.\-]*://\S+$")


def _clean_url(u: str) -> str:
    return u.rstrip(".,;:)]}")


def _token_host(token: str) -> str:
    """`user@host:port/path`, `host:path` (scp) -> `host`, for a token after a network command."""
    t = token.strip("'\"")
    if "://" in t:
        return t
    t = t.rsplit("@", 1)[-1]
    if t.startswith("["):
        return t[1:].split("]", 1)[0]
    if t.count(":") == 1:
        t = t.split(":", 1)[0]
    return t.split("/", 1)[0]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=False)
    except ValueError:
        return segment.split()


def scan_text(text: str, policy: EgressPolicy) -> Verdict | None:
    """Everything in a command or script that names a place to connect to. First blocked verdict wins.

    URLs and the arguments of known network commands are resolved (every address checked); a bare
    name or address elsewhere in the text is matched by rule only, with no lookup, so a filename or
    a pasted log line costs no DNS. This is a heuristic for the honest or accidental call: a built
    hostname, base64 or a script that fetches the target goes through, which is what the loopback
    proxy and the firewall script are for.
    """
    if not text:
        return None
    for m in _URL_RE.finditer(text):
        v = policy.check_url(_clean_url(m.group(0)))
        if v.blocked:
            return v
    for segment in _SPLIT_RE.split(text):
        toks = _tokens(segment)
        names = [t.strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].lower() for t in toks]
        names = [n[:-4] if n.endswith(".exe") else n for n in names]
        try:
            first = next(i for i, n in enumerate(names) if n in NET_COMMANDS)
        except StopIteration:
            continue
        for tok in toks[first + 1:]:
            if tok.startswith("-") or "://" in tok:
                continue
            host = _token_host(tok)
            if not host or host.startswith(("$", "%", "@", "(", "{")):
                continue
            if ep.parse_ip(host) is not None or "." in host:
                v = policy.check_host(host)
                if v.blocked:
                    return v
    for m in _HOSTLIKE_RE.finditer(text):
        tok = m.group(1)
        v = policy.check_ip(tok) if ep.parse_ip(tok) is not None else policy.check_name(tok)
        if v.blocked:
            return v
    for m in _V6_RE.finditer(text):
        tok = m.group(0).rstrip(":.")
        if ep.parse_ip(tok) is not None:
            v = policy.check_ip(tok)
            if v.blocked:
                return v
    return None


def _scan_value(key: str, value: Any, policy: EgressPolicy) -> Verdict | None:
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if key in EXEC_KEYS:
            return scan_text(s, policy)
        if len(s) <= 2048 and _URLISH_VALUE.match(s):
            v = policy.check_url(s)
            return v if v.blocked else None
        if key in HOST_KEYS and len(s) <= 253 and not re.search(r"\s", s):
            host = _token_host(s)
            if host:
                v = policy.check_host(host)
                return v if v.blocked else None
        return None
    if isinstance(value, Mapping):
        for k, v in value.items():
            hit = _scan_value(str(k).lower(), v, policy)
            if hit:
                return hit
    elif isinstance(value, (list, tuple)):
        for item in value:
            hit = _scan_value(key, item, policy)
            if hit:
                return hit
    return None


def scan_args(args: Mapping[str, Any], policy: EgressPolicy) -> Verdict | None:
    for k, v in args.items():
        hit = _scan_value(str(k).lower(), v, policy)
        if hit:
            return hit
    return None


def scan_applies(policy) -> bool:
    """A tool is scanned when it can reach a network or run a process. Local content tools
    (read, write, edit, grep) carry prose and file bodies that must never be refused for what they mention."""
    from litetui import tool_policy as tp
    if policy is None:
        return True
    caps = set(getattr(policy, "capabilities", ()) or ())
    if tp.NETWORK in caps or tp.PROCESS_EXECUTION in caps:
        return True
    fn = getattr(policy, "classify_args", None)
    return fn is not None and fn not in (tp.classify_write, tp.classify_pccontrol)


def scan_call(name: str, args: Mapping[str, Any], policy, *, app=None) -> str | None:
    """The refusal text for this tool call, or None. Blocking (DNS): call it off the event loop."""
    if not isinstance(args, Mapping) or not scan_applies(policy):
        return None
    pol = ep.current()
    if not pol.enabled:
        return None
    try:
        v = scan_args(args, pol)
    except Exception as exc:  # a scanner bug must never take a tool call down; the log says so
        record(name, Verdict(False, rule=f"scan error {type(exc).__name__}", target=""), via="scan-error")
        return None
    if v is None or not v.blocked:
        return None
    record(name, v, via="scan")
    return refusal(name, v, app=app)


# ── web_fetch: pinned connect, every redirect hop checked ────────────────────

class Pins:
    """One fetch's resolutions. A host is resolved and checked ONCE; the connection uses those
    addresses, so a second lookup that answers differently (rebinding) is never made."""

    def __init__(self, policy: EgressPolicy) -> None:
        self.policy = policy
        self._cache: dict[str, Verdict] = {}

    def lookup(self, host: str) -> Verdict:
        key = ep.normalize_host(host)
        v = self._cache.get(key)
        if v is None:
            v = self._cache[key] = self.policy.check_host(host)
        if v.blocked:
            raise EgressBlocked(v)
        return v

    def check_url(self, url: str) -> None:
        for host in ep.url_hosts(url):
            self.lookup(host)


def _connect_pinned(conn: http.client.HTTPConnection, pins: Pins) -> socket.socket:
    v = pins.lookup(conn.host)
    if not v.addresses:
        raise OSError(f"{conn.host}: did not resolve")
    err: OSError | None = None
    for addr in v.addresses:
        try:
            sock = socket.create_connection((addr, conn.port), conn.timeout, conn.source_address)
            break
        except OSError as exc:
            err = exc
    else:
        raise err or OSError(f"{conn.host}: no reachable address")
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    return sock


def build_opener(pins: Pins) -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()

    class HTTPConn(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = _connect_pinned(self, pins)
            if self._tunnel_host:
                self._tunnel()

    class HTTPSConn(http.client.HTTPSConnection):
        def connect(self) -> None:
            sock = _connect_pinned(self, pins)
            self.sock = sock
            if self._tunnel_host:
                self._tunnel()
                sock = self.sock
            self.sock = self._context.wrap_socket(sock, server_hostname=self._tunnel_host or self.host)

    class HTTPH(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(HTTPConn, req)

    class HTTPSH(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(HTTPSConn, req, context=ctx)

    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            pins.check_url(urljoin(req.full_url, newurl))
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(HTTPH, HTTPSH, Redirect)


def open_checked(req: urllib.request.Request, timeout: float, policy: EgressPolicy | None = None):
    """`urlopen` with the egress policy on every hop. Raises EgressBlocked with the verdict."""
    pins = Pins(policy or ep.current())
    pins.check_url(req.full_url)
    return build_opener(pins).open(req, timeout=timeout)


# ── the shell tools' proxy environment ───────────────────────────────────────

def proxy_env(env: dict[str, str]) -> dict[str, str]:
    """Point a shell child at the loopback filtering proxy. No-op when the block is off or the proxy
    cannot start (the failure is logged once and `/egress status` shows it)."""
    pol = ep.current()
    if not pol.enabled:
        return env
    from litetui import egress_proxy
    proxy = egress_proxy.ensure_started()
    if proxy is None:
        return env
    url = proxy.url
    out = dict(env)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        out[k] = url
        out[k.lower()] = url
    no = "localhost,127.0.0.1,::1,[::1]"
    out["NO_PROXY"] = out["no_proxy"] = no
    return out
