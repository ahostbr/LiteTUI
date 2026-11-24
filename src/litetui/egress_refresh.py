"""Rebuild `data/egress_blocklist.json` from APNIC and RIPEstat (T1418).

Explicit user action only (`/egress refresh`, or `python scripts/egress_refresh.py`);
the app never fetches these lists on its own. Pure functions plus one `fetch`
that tests replace, so nothing here needs the network to be tested.

Sources:
* APNIC `delegated-apnic-latest`, rows `apnic|CN|ipv4|ipv6` (country code CN only:
  Hong Kong, Macau and Taiwan are separate codes and are NOT included, the user, 2026-11-23).
* RIPEstat `announced-prefixes` for the Chinese clouds' ASNs, each ASN's holder
  name re-checked with `as-overview` before it is kept (AS34947 is a travel
  company that merely shares the word "Alibaba": it is not on the list).
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Callable

from litetui import egress_policy

APNIC_URL = "https://ftp.apnic.net/stats/apnic/delegated-apnic-latest"
RIPESTAT = "https://stat.ripe.net/data/{what}/data.json?resource={asn}&sourceapp=litetui-egress-refresh"

#: (ASN, words one of which must appear in the RIPEstat holder name, lower-case).
ASNS = (
    ("AS37963", ("alibaba", "aliyun")), ("AS45102", ("alibaba", "aliyun")), ("AS24429", ("alibaba", "aliyun", "taobao")),
    ("AS134963", ("alibaba", "aliyun")), ("AS203513", ("alibaba", "aliyun")),
    ("AS402205", ("alibaba", "aliyun")), ("AS402206", ("alibaba", "aliyun")), ("AS402207", ("alibaba", "aliyun")),
    ("AS45090", ("tencent",)), ("AS132203", ("tencent",)),
    ("AS136907", ("huawei",)),
    ("AS55967", ("baidu",)), ("AS38365", ("baidu",)),
)

DOMAINS = list(egress_policy.DEFAULT_DOMAIN_SUFFIXES)
FIREWALL_CHUNK = 1000
FETCH_TIMEOUT_S = 120

Fetch = Callable[[str], bytes]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "litetui-egress-refresh"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
        return r.read()


# ── parsing ──────────────────────────────────────────────────────────────────

def parse_apnic(text: str) -> tuple[str, list, list]:
    """(file date, IPv4 networks, IPv6 networks) for every `apnic|CN|...` row."""
    file_date = ""
    v4: list = []
    v6: list = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) >= 3 and parts[0] == "2" and parts[1] == "apnic":
            file_date = parts[2]
        if len(parts) < 5 or parts[0] != "apnic" or parts[1] != "CN":
            continue
        if parts[2] == "ipv4":
            first = ipaddress.IPv4Address(parts[3])
            v4.extend(ipaddress.summarize_address_range(first, first + int(parts[4]) - 1))
        elif parts[2] == "ipv6":
            v6.append(ipaddress.IPv6Network(f"{parts[3]}/{parts[4]}"))
    return file_date, v4, v6


def _collapse(nets: list) -> list:
    return list(ipaddress.collapse_addresses(sorted(nets, key=lambda n: (int(n.network_address), n.prefixlen))))


def _set_record(name: str, kind: str, source: str, fetched: str, v4: list, v6: list, **extra) -> dict:
    v4, v6 = _collapse(v4) if v4 else [], _collapse(v6) if v6 else []
    return {
        "name": name, "kind": kind, "source": source, "fetched": fetched, **extra,
        "counts": {"ipv4_cidrs": len(v4), "ipv4_addresses": sum(n.num_addresses for n in v4), "ipv6_cidrs": len(v6)},
        "ipv4": [str(n) for n in v4], "ipv6": [str(n) for n in v6],
    }


def build(fetcher: Fetch = fetch, today: str | None = None, asns=ASNS) -> tuple[dict, list[str]]:
    """The whole data document plus the warnings met on the way (a skipped ASN, a short list)."""
    today = today or _dt.date.today().isoformat()
    warnings: list[str] = []
    report: list[dict] = []
    raw = fetcher(APNIC_URL).decode("utf-8", "replace")
    file_date, v4, v6 = parse_apnic(raw)
    if not v4 or not v6:
        raise ValueError(f"APNIC file had {len(v4)} CN IPv4 and {len(v6)} CN IPv6 networks; refusing to write an empty list")
    cn = _set_record("apnic-cn", "country", APNIC_URL, today, v4, v6, apnic_file_date=file_date,
                     note="APNIC delegations with country code CN (HK, MO and TW are separate codes and not included)")
    sets = [cn]
    cn_index = egress_policy._IpSet("cn", "", "", [*cn["ipv4"], *cn["ipv6"]])
    for asn, words in asns:
        try:
            overview = json.loads(fetcher(RIPESTAT.format(what="as-overview", asn=asn)))["data"]
            holder = str(overview.get("holder") or "")
            if not any(w in holder.lower() for w in words):
                warnings.append(f"{asn}: holder {holder!r} names none of {words}; NOT added")
                report.append({"asn": asn, "holder": holder, "status": "holder name did not match; not added"})
                continue
            prefixes = json.loads(fetcher(RIPESTAT.format(what="announced-prefixes", asn=asn)))["data"]["prefixes"]
        except Exception as exc:  # network or schema trouble must not silently drop an ASN
            warnings.append(f"{asn}: {type(exc).__name__}: {exc}; NOT added")
            report.append({"asn": asn, "holder": "", "status": f"fetch failed: {type(exc).__name__}"})
            continue
        n4, n6, covered = [], [], 0
        for item in prefixes:
            try:
                net = ipaddress.ip_network(item["prefix"], strict=False)
            except (ValueError, KeyError, TypeError):
                continue
            if net.prefixlen == 0:
                continue
            a, b = cn_index.find(net.network_address), cn_index.find(net.broadcast_address)
            if a and a == b:
                covered += 1  # already inside the APNIC CN set: the overlap is collapsed away
                continue
            (n4 if net.version == 4 else n6).append(net)
        if not n4 and not n6:
            warnings.append(f"{asn}: no announced prefixes outside the CN set ({covered} covered); not added")
            report.append({"asn": asn, "holder": holder, "status": f"nothing outside the CN set ({covered} prefixes already covered)"})
            continue
        sets.append(_set_record(asn, "asn", RIPESTAT.format(what="announced-prefixes", asn=asn), today, n4, n6,
                                holder=holder, already_in_cn_set=covered))
    data = {
        "schema": 1, "generated": today,
        "ip_sets": sets,
        "asn_report": report,
        "domains": {"source": "curated by LiteTUI (suffix match, any depth)", "fetched": today, "suffixes": DOMAINS},
    }
    return data, warnings


# ── output ───────────────────────────────────────────────────────────────────

def dumps(data: dict) -> str:
    """One line per set keeps the file diffable without 15,000 lines of CIDRs."""
    head = {k: v for k, v in data.items() if k not in ("ip_sets", "domains")}  # incl. asn_report
    lines = [json.dumps(s, separators=(",", ":"), ensure_ascii=False) for s in data["ip_sets"]]
    return ("{" + json.dumps(head, separators=(",", ":"))[1:-1] + ',"ip_sets":[\n'
            + ",\n".join(lines) + '\n],"domains":' + json.dumps(data["domains"], separators=(",", ":")) + "}\n")


def write(data: dict, path: Path | str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(dumps(data), encoding="utf-8", newline="\n")
    os.replace(tmp, p)


def summary(data: dict) -> list[str]:
    out = []
    for s in data["ip_sets"]:
        c = s["counts"]
        out.append(f"{s['name']:<10} v4 {c['ipv4_cidrs']:>5} CIDRs {c['ipv4_addresses']:>12,} addresses | v6 {c['ipv6_cidrs']:>5} CIDRs"
                   + (f" | {s['holder']}" if s.get("holder") else ""))
    out.append(f"domains: {len(data['domains']['suffixes'])} suffixes")
    return out


def diff(old: dict | None, new: dict) -> list[str]:
    """What changes between the file on disk and the freshly built one, by set."""
    old_sets = {s["name"]: s for s in (old or {}).get("ip_sets", ())}
    lines = []
    for s in new["ip_sets"]:
        o = old_sets.get(s["name"])
        if o is None:
            lines.append(f"+ {s['name']}: new set, {s['counts']['ipv4_cidrs']} v4 / {s['counts']['ipv6_cidrs']} v6 CIDRs")
            continue
        for fam in ("ipv4", "ipv6"):
            a, b = set(o.get(fam, ())), set(s.get(fam, ()))
            if a != b:
                lines.append(f"~ {s['name']} {fam}: +{len(b - a)} -{len(a - b)} (now {len(b)})")
    for name in old_sets.keys() - {s["name"] for s in new["ip_sets"]}:
        lines.append(f"- {name}: removed")
    od, nd = set((old or {}).get("domains", {}).get("suffixes", ())), set(new["domains"]["suffixes"])
    if od != nd:
        lines.append(f"~ domains: +{sorted(nd - od)} -{sorted(od - nd)}")
    return lines or ["no change"]


def firewall_script(data: dict) -> str:
    """A reviewed `New-NetFirewallRule` script. A FILE ONLY: LiteTUI never runs it."""
    chunks: list[tuple[str, list[str]]] = []
    for fam, label in (("ipv4", "v4"), ("ipv6", "v6")):
        cidrs = [c for s in data["ip_sets"] for c in s.get(fam, ())]
        # collapse across sets so the rules carry no duplicates
        nets = _collapse([ipaddress.ip_network(c) for c in cidrs])
        for i in range(0, len(nets), FIREWALL_CHUNK):
            chunks.append((f"{label}-{i // FIREWALL_CHUNK + 1:03d}", [str(n) for n in nets[i:i + FIREWALL_CHUNK]]))
    total = sum(len(c) for _, c in chunks)
    out = [
        "<#",
        "  LiteTUI egress firewall rules (T1418). GENERATED by egress_refresh from data/egress_blocklist.json",
        f"  generated {data['generated']}: {len(chunks)} outbound BLOCK rules covering {total} CIDRs (China per APNIC,",
        "  plus Alibaba, Tencent, Huawei Cloud and Baidu ranges announced elsewhere).",
        "",
        "  LiteTUI NEVER runs this file. It is for you to read and, if you want it, run yourself from an",
        "  elevated PowerShell. It is a SYSTEM setting: it blocks these ranges for EVERY program on this PC,",
        "  not just LiteTUI. It is the only layer that stops a raw socket from any process.",
        "",
        "  Default run (no switch) only PRINTS what it would do.   -Apply   creates the rules.",
        "  -Remove removes exactly the rules this script created (names start with 'LiteTUI-Egress-').",
        "#>",
        "[CmdletBinding()]",
        "param([switch]$Apply, [switch]$Remove)",
        "$ErrorActionPreference = 'Stop'",
        "$prefix = 'LiteTUI-Egress-'",
        "if ($Remove) {",
        "  $existing = Get-NetFirewallRule -DisplayName ($prefix + '*') -ErrorAction SilentlyContinue",
        "  Write-Host ('{0} existing LiteTUI egress rules' -f @($existing).Count)",
        "  if ($existing) { $existing | Remove-NetFirewallRule }",
        "  return",
        "}",
        "$rules = [ordered]@{",
    ]
    for name, cidrs in chunks:
        out.append(f"  '{name}' = @(" + ",".join(f"'{c}'" for c in cidrs) + ")")
    out += [
        "}",
        "foreach ($k in $rules.Keys) {",
        "  $display = $prefix + $k",
        "  if (-not $Apply) { Write-Host ('would create {0}: {1} ranges' -f $display, $rules[$k].Count); continue }",
        "  Get-NetFirewallRule -DisplayName $display -ErrorAction SilentlyContinue | Remove-NetFirewallRule",
        "  New-NetFirewallRule -DisplayName $display -Direction Outbound -Action Block -RemoteAddress $rules[$k] -Profile Any | Out-Null",
        "  Write-Host ('created {0}' -f $display)",
        "}",
        "if (-not $Apply) { Write-Host 'Dry run. Re-run with -Apply to create the rules, -Remove to delete them.' }",
        "",
    ]
    return "\n".join(out)


def write_firewall(data: dict, path: Path | str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(firewall_script(data), encoding="utf-8", newline="\n")


def load_existing(path: Path | str = egress_policy.DATA_PATH) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
