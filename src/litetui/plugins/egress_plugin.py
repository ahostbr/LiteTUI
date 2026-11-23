"""/egress: show, test and refresh the tool-path network block (T1418).

    /egress status            what is blocked, from which data and date, the proxy, the log
    /egress test <host|ip|url>  the verdict for one target, with the rule that decided it
    /egress refresh           fetch APNIC + RIPEstat, show the diff, write the data file

`refresh` is the ONLY place the app fetches the lists, and only because the user typed it.
"""
from __future__ import annotations

from litetui import egress_guard, egress_policy as ep, egress_proxy, egress_refresh
from litetui.plugins import PluginManifest

_USAGE = "usage: /egress status | test <host|ip|url> | refresh"


def status_text() -> str:
    pol = ep.current()
    d = pol.data
    lines = [f"Egress block: {'ON' if pol.enabled else 'OFF'} (tool paths only; model backends are not affected)"]
    if d.error:
        lines.append(f"  DATA PROBLEM: {d.error} -- only the built-in domain list is active")
    for s in d.sets:
        v4, v6 = s.count()
        lines.append(f"  {s.name}: {v4} IPv4 + {v6} IPv6 CIDRs, fetched {s.fetched or '?'}")
    lines.append(f"  domains: {len(d.suffixes)} suffixes ({d.domain_source})")
    lines.append(f"  data file: {ep.data_path()} (newest set {d.newest() or '?'})")
    if pol._extra_domains or pol._extra_set is not None:
        lines.append("  plus your extra blocked list (Settings -> egress_extra_blocked)")
    if pol._allow_domains or pol._allow_sets:
        lines.append("  and your exceptions (Settings -> egress_allow)")
    proxy = egress_proxy.current()
    if proxy is not None:
        lines.append(f"  shell proxy: {proxy.url}, {proxy.passed} passed, {proxy.refused} refused")
    elif egress_proxy.last_error:
        lines.append(f"  shell proxy: NOT RUNNING ({egress_proxy.last_error}); shell commands are covered by the command scan only")
    else:
        lines.append("  shell proxy: starts with the first shell command")
    lines.append(f"  log: {egress_guard.log_path()}")
    lines.append("  Not covered: a program that ignores the proxy variables or opens a raw socket, and the "
                 "Claude backend's own Bash/WebFetch. scripts/egress_firewall_rules.ps1 is the only cover for those "
                 "(a file you run yourself; LiteTUI never does).")
    return "\n".join(lines)


def test_text(target: str) -> str:
    target = target.strip()
    if not target:
        return _USAGE
    pol = ep.current()
    v = pol.check_url(target) if "://" in target else pol.check_host(target)
    out = [v.describe()]
    if v.addresses:
        out.append("  addresses: " + ", ".join(v.addresses))
    return "\n".join(out)


def _refresh(app) -> None:
    say = lambda text: app.call_from_thread(app.system_message, text)  # noqa: E731
    try:
        data, warnings = egress_refresh.build()
        path = ep.user_data_path()
        changes = egress_refresh.diff(egress_refresh.load_existing(ep.data_path()), data)
        egress_refresh.write(data, path)
    except Exception as exc:  # network, schema or disk: say so, change nothing
        say(f"/egress refresh failed, nothing written: {type(exc).__name__}: {exc}")
        return
    ep._data_cache.clear()
    say("\n".join(["/egress refresh: wrote " + str(path), *[f"  {c}" for c in changes],
                   *[f"  warning: {w}" for w in warnings]]))


def _cmd_egress(app, name: str, arg: str) -> None:
    word, _, rest = (arg or "").strip().partition(" ")
    word = word.lower()
    if word in ("", "status"):
        app.system_message(status_text())
    elif word == "test":
        app.system_message(test_text(rest))
    elif word == "refresh":
        app.system_message("/egress refresh: fetching APNIC and RIPEstat ...")
        app.run_worker(lambda: _refresh(app), thread=True, name="egress-refresh")
    else:
        app.system_message(_USAGE)


def _register(ctx) -> None:
    ctx.command(
        ("/egress",), _cmd_egress,
        palette="Egress block",
        help="Show or test the China network block on tool paths, or refresh its lists: status | test <target> | refresh.",
        group="safety",
        order=60,
    )


PLUGIN = PluginManifest(id="egress", register=_register)
