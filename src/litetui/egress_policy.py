"""Egress policy: which network targets a TOOL may not reach (T1418).

Pure Python, no system calls beyond name resolution. The data is a file the app
only ever reads (`data/egress_blocklist.json`, rebuilt by `scripts/egress_refresh.py`
on an explicit `/egress refresh`): every IPv4 and IPv6 block APNIC delegates to CN,
Chinese clouds' ranges announced elsewhere, and a list of domain suffixes.

Design points that each have a test:

* Intervals are sorted integers searched with `bisect`, one list per set and
  family, never a loop over 7,500 networks.
* A name is resolved ONCE and EVERY address it has is checked, so a name with one
  CN address among five public ones is refused. `check_host` returns the checked
  addresses in `Verdict.addresses`; a caller that connects to one of THOSE (and
  does not look the name up again) cannot be rebound between check and connect.
* Literal forms the socket layer accepts (`2130706433`, `0x7f.1`, `0177.0.0.1`,
  `::ffff:1.2.3.4`, 6to4, NAT64) are normalised first.
* Loopback, private and link-local addresses are never blocked: llama, ninfer,
  basalt and the MCP servers live there.
* Hostname rules match by suffix at any depth, case-insensitively, tolerating a
  trailing dot, fullwidth dots and IDNA.

Model backends are NOT in scope (Ryan 2026-11-23): nothing here is called from a
backend path.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import unquote, urlsplit

DATA_PATH = Path(__file__).with_name("data") / "egress_blocklist.json"

#: Used when the data file has no `domains` section (or cannot be read), so the
#: name rules never silently vanish with the file. The file's list replaces it.
DEFAULT_DOMAIN_SUFFIXES = (
    "cn", "xn--fiqs8s", "xn--fiqz9s",
    "aliyuncs.com", "aliyun.com", "alibabacloud.com", "alicdn.com",
    "qcloud.com", "tencentcs.com", "myqcloud.com",
    "huaweicloud.com", "myhuaweicloud.com",
    "baidubce.com", "bcebos.com", "modelscope.cn",
)

_DOTS = str.maketrans({"。": ".", "．": ".", "｡": "."})
_V4_PART = re.compile(r"0[xX][0-9a-fA-F]*|[0-9]+")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


# ── literal parsing ──────────────────────────────────────────────────────────

def parse_v4_loose(text: str) -> ipaddress.IPv4Address | None:
    """`inet_aton` forms, parsed by hand so the answer does not depend on the OS.

    One to four parts, each decimal, `0x` hex or leading-zero octal; the last part
    fills every remaining byte (`127.1`, `2130706433`, `0x7f000001`, `0177.0.0.1`).
    """
    parts = text.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    nums = []
    for part in parts:
        if not _V4_PART.fullmatch(part):
            return None
        try:
            if part[:2] in ("0x", "0X"):
                nums.append(int(part[2:] or "0", 16))
            elif len(part) > 1 and part[0] == "0":
                nums.append(int(part, 8))
            else:
                nums.append(int(part, 10))
        except ValueError:
            return None
    *head, last = nums
    if any(h > 255 for h in head) or last >= 256 ** (4 - len(head)):
        return None
    value = 0
    for h in head:
        value = (value << 8) | h
    return ipaddress.IPv4Address((value << (8 * (4 - len(head)))) | last)


def parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host string IS, or None when it is a name."""
    if not isinstance(text, str):
        return None
    t = text.strip().translate(_DOTS)
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1]
    t = t.split("%", 1)[0]
    if not t:
        return None
    if ":" in t:
        try:
            return ipaddress.IPv6Address(t)
        except ValueError:
            return None
    return parse_v4_loose(t)


def embedded_v4(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses an IPv6 address would actually reach: mapped, 6to4, NAT64, compatible."""
    if ip.version != 6:
        return []
    out = []
    if ip.ipv4_mapped is not None:
        out.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        out.append(ip.sixtofour)
    value = int(ip)
    if ip in _NAT64:
        out.append(ipaddress.IPv4Address(value & 0xFFFFFFFF))
    elif value >> 32 == 0 and value > 1:  # ::a.b.c.d (deprecated IPv4-compatible)
        out.append(ipaddress.IPv4Address(value & 0xFFFFFFFF))
    return out


def normalize_host(host: str) -> str:
    """Lower-case ASCII (IDNA) host without trailing dots, percent-escapes or brackets."""
    h = unquote(str(host or "")).strip().translate(_DOTS)
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.rstrip(".").lower()
    if h and not h.isascii():
        try:
            h = h.encode("idna").decode("ascii")
        except UnicodeError:
            labels = []
            for label in h.split("."):
                try:
                    labels.append(label.encode("idna").decode("ascii") if label and not label.isascii() else label)
                except UnicodeError:
                    labels.append(label)
            h = ".".join(labels)
    return h


# ── verdict ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Verdict:
    blocked: bool
    rule: str = ""          # what matched, in words: "1.0.1.0/24 (apnic-cn)" or "domain aliyuncs.com"
    source: str = ""        # where that rule's data came from
    data_date: str = ""     # when the data was fetched
    target: str = ""        # what was asked about
    ip: str = ""            # the address that matched ("" for a name rule)
    kind: str = ""          # cidr | domain | extra | allowed | ""
    addresses: tuple[str, ...] = field(default=(), compare=False)  # every address checked: connect to one of THESE
    note: str = ""          # an allow entry's note, or a data error

    def describe(self) -> str:
        if not self.blocked:
            return f"{self.target or 'target'}: allowed" + (f" ({self.note})" if self.note else "")
        bits = [self.rule]
        if self.source:
            bits.append(f"data: {self.source}")
        if self.data_date:
            bits.append(f"as of {self.data_date}")
        return f"{self.target}: BLOCKED by {'; '.join(bits)}"


NOT_BLOCKED = Verdict(False)


# ── data ─────────────────────────────────────────────────────────────────────

class _IpSet:
    """One named list of CIDRs, one sorted disjoint interval list per family."""

    __slots__ = ("name", "source", "fetched", "v4", "v6")

    def __init__(self, name: str, source: str, fetched: str, nets: Iterable[str]) -> None:
        self.name, self.source, self.fetched = name, source, fetched
        v4, v6 = [], []
        for raw in nets:
            try:
                net = ipaddress.ip_network(str(raw).strip(), strict=False)
            except ValueError:
                continue
            (v4 if net.version == 4 else v6).append(net)
        self.v4 = self._intervals(v4)
        self.v6 = self._intervals(v6)

    @staticmethod
    def _intervals(nets: list) -> tuple[list[int], list[int], list[int]]:
        starts, ends, lens = [], [], []
        for net in ipaddress.collapse_addresses(nets) if nets else ():
            starts.append(int(net.network_address))
            ends.append(int(net.broadcast_address))
            lens.append(net.prefixlen)
        return starts, ends, lens

    def find(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
        starts, ends, lens = self.v4 if ip.version == 4 else self.v6
        n = int(ip)
        i = bisect_right(starts, n) - 1
        if i >= 0 and n <= ends[i]:
            base = ipaddress.ip_address(starts[i]) if ip.version == 4 else ipaddress.IPv6Address(starts[i])
            return f"{base}/{lens[i]}"
        return None

    def count(self) -> tuple[int, int]:
        return len(self.v4[0]), len(self.v6[0])


@dataclass
class BlockData:
    """The parsed data file: IP sets plus domain suffixes. Built once per file version."""

    sets: list[_IpSet]
    suffixes: frozenset[str]
    domain_source: str
    domain_fetched: str
    generated: str
    error: str = ""     # why the file could not be used, shown by `/egress status`

    def newest(self) -> str:
        dates = [s.fetched for s in self.sets if s.fetched]
        return max(dates) if dates else self.generated


_data_lock = threading.Lock()
_data_cache: dict[str, tuple[float, BlockData]] = {}


def user_data_path() -> Path:
    """Where `/egress refresh` writes: the user's data dir, never the installed package."""
    from litetui import paths
    return paths.data_root() / "egress_blocklist.json"


def data_path() -> Path:
    """The user's refreshed file when there is one, else the one shipped in the package."""
    try:
        user = user_data_path()
        if user.is_file():
            return user
    except Exception:
        pass
    return DATA_PATH


def load_data(path: Path | str | None = None) -> BlockData:
    """Read (and cache by mtime) the blocklist file. Never raises: a bad file is an `error`."""
    p = Path(path) if path else data_path()
    try:
        mtime = p.stat().st_mtime
    except OSError as exc:
        return _fallback(f"data file missing: {p} ({type(exc).__name__})")
    key = str(p)
    with _data_lock:
        hit = _data_cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        sets = [
            _IpSet(str(s.get("name") or "set"), str(s.get("source") or ""), str(s.get("fetched") or ""),
                   [*(s.get("ipv4") or ()), *(s.get("ipv6") or ())])
            for s in raw.get("ip_sets", ())
        ]
        dom = raw.get("domains") or {}
        suffixes = frozenset(normalize_host(x).lstrip(".") for x in (dom.get("suffixes") or DEFAULT_DOMAIN_SUFFIXES))
        data = BlockData(sets, suffixes, str(dom.get("source") or "built-in list"),
                         str(dom.get("fetched") or raw.get("generated") or ""), str(raw.get("generated") or ""))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return _fallback(f"data file unreadable: {p} ({type(exc).__name__}: {exc})")
    with _data_lock:
        _data_cache[key] = (mtime, data)
    return data


def _fallback(error: str) -> BlockData:
    return BlockData([], frozenset(DEFAULT_DOMAIN_SUFFIXES), "built-in list", "", "", error)


# ── policy ───────────────────────────────────────────────────────────────────

def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]
        if addr not in seen:
            seen.append(addr)
    return seen


def _entry(text: str) -> tuple[str, str]:
    """`host # note` -> (host, note)."""
    body, _, note = str(text).partition(" #")
    return body.strip(), note.strip()


class EgressPolicy:
    """The decision. Cheap to build per call: the heavy part is the cached `BlockData`."""

    def __init__(self, data: BlockData | None = None, *, enabled: bool = True,
                 extra: Sequence[str] = (), allow: Sequence[str] = (),
                 resolver: Callable[[str], Sequence[str]] | None = None) -> None:
        self.data = data if data is not None else load_data()
        self.enabled = bool(enabled)
        self.resolver = resolver or _default_resolver
        self._extra_domains: list[str] = []
        extra_nets: list[str] = []
        for item in extra or ():
            host, _ = _entry(item)
            if not host:
                continue
            if "/" in host or parse_ip(host) is not None:
                extra_nets.append(host)
            else:
                self._extra_domains.append(normalize_host(host).lstrip("."))
        self._extra_set = _IpSet("your extra list", "egress_extra_blocked setting", "", extra_nets) if extra_nets else None
        self._allow_domains: list[tuple[str, str]] = []
        allow_nets: list[tuple[str, str]] = []
        for item in allow or ():
            host, note = _entry(item)
            if not host:
                continue
            if "/" in host or parse_ip(host) is not None:
                allow_nets.append((host, note))
            else:
                self._allow_domains.append((normalize_host(host).lstrip("."), note))
        self._allow_sets = [(_IpSet(h, "", "", [h]), h, note) for h, note in allow_nets]

    # -- single address -------------------------------------------------------

    def _ip_verdict(self, ip, target: str) -> Verdict:
        for aset, entry, note in self._allow_sets:
            if aset.find(ip):
                return Verdict(False, kind="allowed", target=target, ip=str(ip), note=f"allowed by {entry}" + (f": {note}" if note else ""))
        # The user's own extra list is checked BEFORE the never-blocked ranges: they asked for it by name.
        if self._extra_set is not None and (cidr := self._extra_set.find(ip)):
            return Verdict(True, rule=f"{cidr} (your extra blocked list)", source="egress_extra_blocked setting",
                           target=target, ip=str(ip), kind="extra")
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
            return Verdict(False, target=target, ip=str(ip))
        for aset in self.data.sets:
            if cidr := aset.find(ip):
                return Verdict(True, rule=f"{cidr} ({aset.name})", source=aset.source, data_date=aset.fetched,
                               target=target, ip=str(ip), kind="cidr")
        return Verdict(False, target=target, ip=str(ip))

    def check_ip(self, text) -> Verdict:
        """A literal address in any form the socket layer accepts. A name is not an address: use check_host."""
        target = str(text)
        ip = parse_ip(target)
        if ip is None:
            return Verdict(False, target=target, note="not an IP literal")
        if not self.enabled:
            return Verdict(False, target=target, ip=str(ip), addresses=(str(ip),))
        for cand in (ip, *embedded_v4(ip)):
            v = self._ip_verdict(cand, target)
            if v.blocked:
                return v
            if v.kind == "allowed":
                return Verdict(False, kind="allowed", target=target, ip=str(ip), note=v.note, addresses=(str(ip),))
        return Verdict(False, target=target, ip=str(ip), addresses=(str(ip),))

    # -- names ----------------------------------------------------------------

    def _suffix_hit(self, host: str, suffixes: Iterable[str]) -> str | None:
        wanted = set(suffixes)
        labels = host.split(".")
        for i in range(len(labels)):
            cand = ".".join(labels[i:])
            if cand in wanted:
                return cand
        return None

    def check_name(self, host: str) -> Verdict:
        """The name rules only (no resolution): allow list, extra list, data suffixes."""
        h = normalize_host(host)
        if not h:
            return Verdict(False, target=str(host))
        for dom, note in self._allow_domains:
            if self._suffix_hit(h, (dom,)):
                return Verdict(False, kind="allowed", target=host, note=f"allowed by {dom}" + (f": {note}" if note else ""))
        if self._suffix_hit(h, self._extra_domains):
            hit = self._suffix_hit(h, self._extra_domains)
            return Verdict(True, rule=f"domain {hit} (your extra blocked list)", source="egress_extra_blocked setting",
                           target=host, kind="extra")
        hit = self._suffix_hit(h, self.data.suffixes)
        if hit:
            return Verdict(True, rule=f"domain {hit}", source=self.data.domain_source,
                           data_date=self.data.domain_fetched or self.data.generated, target=host, kind="domain")
        return Verdict(False, target=host)

    def check_host(self, host: str, *, resolve: bool = True) -> Verdict:
        """A host name or literal: the name rules, then EVERY address it resolves to.

        The returned `addresses` are the ones that were checked; connect to one of
        them rather than resolving again. Resolution failure is not a block (the
        connect will fail by itself) and yields no addresses.
        """
        raw = str(host or "")
        if not raw.strip():
            return Verdict(False, target=raw)
        ip = parse_ip(raw)
        if ip is not None:
            return self.check_ip(raw)
        h = normalize_host(raw)
        if not self.enabled:
            return Verdict(False, target=raw, addresses=self._resolve(h) if resolve else ())
        named = self.check_name(h)
        if named.blocked:
            return Verdict(True, named.rule, named.source, named.data_date, raw, "", named.kind)
        if not resolve:
            return Verdict(False, target=raw, kind=named.kind, note=named.note)
        addrs = self._resolve(h)
        if named.kind == "allowed":
            return Verdict(False, kind="allowed", target=raw, note=named.note, addresses=tuple(addrs))
        for a in addrs:
            ipa = parse_ip(a)
            if ipa is None:
                continue
            v = self.check_ip(a)
            if v.blocked:
                return Verdict(True, v.rule + f" - {raw} resolves to {a}", v.source, v.data_date, raw, a, v.kind,
                               addresses=tuple(addrs))
        return Verdict(False, target=raw, addresses=tuple(addrs))

    def _resolve(self, host: str) -> list[str]:
        try:
            return [str(a) for a in self.resolver(host)]
        except (OSError, UnicodeError, ValueError):
            return []

    def check_url(self, url: str, *, resolve: bool = True) -> Verdict:
        """The host of a URL, parsed the way urllib parses it, and (when it differs) the way a browser does."""
        text = str(url or "").strip()
        hosts = url_hosts(text)
        if not hosts:
            return Verdict(False, target=text)
        first = Verdict(False, target=text)
        for h in hosts:
            v = self.check_host(h, resolve=resolve)
            if v.blocked:
                return Verdict(True, v.rule, v.source, v.data_date, text, v.ip, v.kind, v.addresses)
            if not first.addresses and v.addresses:
                first = Verdict(False, target=text, addresses=v.addresses)
        return first


def url_hosts(url: str) -> list[str]:
    """Hosts a URL could be read to mean. urllib's first; then a lenient parse (backslash, userinfo tricks)."""
    out: list[str] = []
    try:
        h = urlsplit(url).hostname
    except ValueError:
        h = None
    if h:
        out.append(h)
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://([^/?#]*)", url)
    if m:
        netloc = m.group(1)
        netloc = re.split(r"[\\]", netloc, maxsplit=1)[0] if "\\" in netloc else netloc
        netloc = netloc.rsplit("@", 1)[-1]
        if netloc.startswith("["):
            lenient = netloc[1:].split("]", 1)[0]
        else:
            lenient = netloc.split(":", 1)[0]
        # the part after the LAST @ of the whole authority, in case a backslash hid it
        full = m.group(1).rsplit("@", 1)[-1]
        full = full[1:].split("]", 1)[0] if full.startswith("[") else full.split(":", 1)[0]
        for cand in (lenient, full):
            if cand and cand not in out:
                out.append(cand)
    return out


# ── module-level wiring ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    extra: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()


_provider: Callable[[], Settings] | None = None
_resolver: Callable[[str], Sequence[str]] | None = None


def configure(provider: Callable[[], Settings] | None) -> None:
    """The app installs a LIVE settings provider (like `tools_disabled`). With none
    installed the block is ON: a missing provider must never silently disable it."""
    global _provider
    _provider = provider


def set_resolver(resolver: Callable[[str], Sequence[str]] | None) -> None:
    """Replace name resolution for the whole process (tests, the live proof)."""
    global _resolver
    _resolver = resolver


def current() -> EgressPolicy:
    try:
        s = _provider() if _provider is not None else Settings()
    except Exception:
        s = Settings()
    return EgressPolicy(enabled=s.enabled, extra=tuple(s.extra or ()), allow=tuple(s.allow or ()), resolver=_resolver)


class EgressBlocked(Exception):
    """Raised inside a connection or redirect when the target is refused. Deliberately
    NOT an OSError: urllib wraps OSError into URLError and would hide it."""

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(verdict.describe())
        self.verdict = verdict


# a short resolution cache so one command scan with many mentions does not repeat DNS
class ResolveCache:
    def __init__(self, ttl: float = 30.0) -> None:
        self.ttl = ttl
        self._d: dict[str, tuple[float, list[str]]] = {}

    def wrap(self, resolver: Callable[[str], Sequence[str]]) -> Callable[[str], list[str]]:
        def run(host: str) -> list[str]:
            hit = self._d.get(host)
            now = time.monotonic()
            if hit and now - hit[0] < self.ttl:
                return hit[1]
            value = list(resolver(host))
            self._d[host] = (now, value)
            return value
        return run
