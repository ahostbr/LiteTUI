"""T1418: the egress block on TOOL paths (China per APNIC, Chinese clouds, listed domains).

No GPU, no model, no system setting. Name resolution is a scripted fake and every
"remote" is a scripted server on 127.0.0.1, so a CN address is never contacted.
"""
from __future__ import annotations

import http.server
import ipaddress
import json
import shutil
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from litetui import egress_guard, egress_policy as ep, egress_proxy, egress_refresh
from litetui.plugins import core_tools
from litetui.tool_policy import SHELL_POLICY, WRITE_POLICY, NETWORK_READ_POLICY, CHROME_POLICY, MCP_UNKNOWN_POLICY

DATA = ep.load_data()
def _inside(intervals, family, widest):
    starts, _ends, lens = intervals
    i = next(i for i, n in enumerate(lens) if n <= widest)      # a block big enough that start+3 is inside it
    return family(starts[i] + 3)


CN_V4 = _inside(DATA.sets[0].v4, ipaddress.IPv4Address, 24)
CN_V6 = _inside(DATA.sets[0].v6, ipaddress.IPv6Address, 48)
GOOD_IP = "93.184.216.34"

HOSTS = {
    "good.test": ["127.0.0.1"],            # allowed (loopback: the scripted server)
    "public.test": [GOOD_IP],
    "cn-one.test": [str(CN_V4)],
    "mixed.test": [GOOD_IP, str(CN_V4)],
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    calls: list[str] = []

    def resolver(host):
        calls.append(host)
        return list(HOSTS.get(host, []))

    ep.set_resolver(resolver)
    ep.configure(None)
    monkeypatch.setattr(egress_guard, "LOG_PATH", tmp_path / "logs" / "egress.jsonl")
    egress_proxy.shutdown()
    yield SimpleNamespace(calls=calls, log=tmp_path / "logs" / "egress.jsonl")
    egress_proxy.shutdown()
    ep.set_resolver(None)
    ep.configure(None)


def policy(**kw) -> ep.EgressPolicy:
    return ep.EgressPolicy(resolver=ep._resolver or (lambda h: HOSTS.get(h, [])), **kw)


# ── the data ─────────────────────────────────────────────────────────────────

def test_the_data_file_carries_the_measured_ranges_and_its_dates():
    cn = next(s for s in DATA.sets if s.name == "apnic-cn")
    assert cn.count()[0] >= 5000 and cn.count()[1] >= 1800, cn.count()
    assert cn.source.startswith("https://ftp.apnic.net/") and cn.fetched
    names = {s.name for s in DATA.sets}
    assert {"AS37963", "AS45102", "AS132203", "AS136907", "AS55967"} <= names
    assert "AS34947" not in names                       # the unrelated "Alibaba Travels"
    for dom in ("cn", "aliyuncs.com", "aliyun.com", "alibabacloud.com", "alicdn.com", "qcloud.com", "tencentcs.com",
                "myqcloud.com", "huaweicloud.com", "myhuaweicloud.com", "baidubce.com", "bcebos.com", "modelscope.cn"):
        assert dom in DATA.suffixes, dom
    raw = json.loads(ep.DATA_PATH.read_text(encoding="utf-8"))
    assert all(s.get("source") and s.get("fetched") and "counts" in s for s in raw["ip_sets"])


# ── addresses ────────────────────────────────────────────────────────────────

def test_cn_ipv4_ipv6_and_mapped_addresses_are_refused():
    p = policy()
    assert p.check_ip(str(CN_V4)).blocked
    assert p.check_ip(str(CN_V6)).blocked
    assert p.check_ip(f"::ffff:{CN_V4}").blocked
    assert p.check_ip(f"[{CN_V6}]").blocked
    v = p.check_ip(str(CN_V4))
    assert "apnic-cn" in v.rule and v.data_date and v.source.startswith("https://ftp.apnic.net")


def test_literal_spellings_the_socket_layer_accepts_are_refused():
    p = policy()
    n = int(CN_V4)
    octets = str(CN_V4).split(".")
    forms = [
        str(n),                                                       # decimal
        hex(n),                                                       # 0x...
        ".".join("0%o" % int(o) for o in octets),                     # octal per octet
        f"0x{int(octets[0]):x}.{int(octets[1])}.{int(octets[2])}.{int(octets[3])}",   # mixed
        f"{octets[0]}.{octets[1]}.{(int(octets[2]) << 8) | int(octets[3])}",          # 3-part
    ]
    for form in forms:
        assert ep.parse_ip(form) == CN_V4, form
        assert p.check_ip(form).blocked, form
        assert p.check_host(form).blocked, form
    # 6to4 and NAT64 carry an IPv4 address inside an IPv6 one
    six_to_four = ipaddress.IPv6Address((0x2002 << 112) | (n << 80))
    nat64 = ipaddress.IPv6Address((0x0064FF9B << 96) | n)
    assert p.check_ip(str(six_to_four)).blocked
    assert p.check_ip(str(nat64)).blocked


def test_loopback_private_and_public_addresses_pass():
    p = policy()
    for ok in ("127.0.0.1", "127.1", "::1", "[::1]", "10.1.2.3", "192.168.1.9", "172.16.0.5", "169.254.1.1", "fe80::1",
               "0.0.0.0", GOOD_IP, "2130706433", "::ffff:127.0.0.1"):
        assert not p.check_ip(ok).blocked, ok


# ── names ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", [
    "aliyuncs.com", "oss-cn-hangzhou.ALIYUNCS.com", "a.b.c.d.aliyuncs.com.", "WWW.Baidu.CN", "site.cn",
    "x.qcloud.com", "bucket.cos.myqcloud.com", "xn--fiqs8s", "例子.中国", "例子。中国", "huaweicloud.com",
    "a.modelscope.cn", "x.baidubce.com", "x.bcebos.com", "x.alicdn.com", "ex%61mple.cn",
])
def test_a_blocked_domain_is_refused_at_any_depth_case_and_spelling(host):
    v = policy().check_host(host)
    assert v.blocked and v.kind == "domain", (host, v)
    assert v.data_date and "domain" in v.rule


@pytest.mark.parametrize("host", ["example.com", "evilaliyuncs.com", "aliyuncs.com.example.org", "cn.example.org", "notcn", "huggingface.co"])
def test_lookalikes_and_normal_hosts_are_not_refused(host):
    assert not policy().check_host(host).blocked


def test_a_name_with_one_blocked_and_one_allowed_address_is_refused():
    v = policy().check_host("mixed.test")
    assert v.blocked and str(CN_V4) in v.rule
    assert not policy().check_host("public.test").blocked


def test_rebinding_is_refused_by_pinning_not_by_a_second_lookup():
    seq = iter([[GOOD_IP], [str(CN_V4)]])
    p = ep.EgressPolicy(resolver=lambda h: next(seq))
    pins = egress_guard.Pins(p)
    first = pins.lookup("rebind.test")                 # allowed on the first lookup
    assert first.addresses == (GOOD_IP,)
    assert pins.lookup("rebind.test").addresses == (GOOD_IP,)   # same pinned answer, no second DNS
    assert list(seq) == [[str(CN_V4)]]                 # the blocked second answer was never even asked for


def test_setting_off_passes_everything_and_the_exception_beats_the_block():
    off = policy(enabled=False)
    assert not off.check_ip(str(CN_V4)).blocked and not off.check_host("x.cn").blocked
    allow = policy(allow=["x.cn # the user's own mirror", str(CN_V4)])
    v = allow.check_host("sub.x.cn")
    assert not v.blocked and v.kind == "allowed" and "own mirror" in v.note
    assert not allow.check_ip(str(CN_V4)).blocked
    extra = policy(extra=["corp.example", "203.0.113.0/24"])
    assert extra.check_host("a.corp.example").blocked and extra.check_ip("203.0.113.9").blocked


def test_no_provider_means_the_block_is_on():
    ep.configure(None)
    assert ep.current().enabled is True
    ep.configure(lambda: ep.Settings(enabled=False))
    assert ep.current().enabled is False
    ep.configure(lambda: (_ for _ in ()).throw(RuntimeError("provider broke")))
    assert ep.current().enabled is True                # a broken provider must not switch the block off


def test_a_missing_data_file_is_reported_and_the_name_rules_survive(tmp_path):
    d = ep.load_data(tmp_path / "nope.json")
    assert d.error and "missing" in d.error
    assert ep.EgressPolicy(d).check_host("x.aliyuncs.com").blocked


# ── refresh (no network) ─────────────────────────────────────────────────────

APNIC = "\n".join([
    "2|apnic|20261124|1|19830613|20261124|+1000",
    "apnic|CN|ipv4|1.0.1.0|256|20110414|allocated",
    "apnic|CN|ipv4|1.0.2.0|512|20110414|allocated",
    "apnic|HK|ipv4|1.2.3.0|256|20110414|allocated",
    "apnic|CN|ipv6|2001:250::|35|20000101|allocated",
])


def _fake_fetch(holder="ALIBABA-CN-NET Alibaba", prefixes=("47.0.0.0/16", "1.0.1.0/24", "47.1.0.0/16")):
    def fetch(url: str) -> bytes:
        if url == egress_refresh.APNIC_URL:
            return APNIC.encode()
        if "as-overview" in url:
            return json.dumps({"data": {"holder": holder}}).encode()
        return json.dumps({"data": {"prefixes": [{"prefix": p} for p in prefixes]}}).encode()
    return fetch


def test_refresh_builds_cn_only_collapses_overlap_and_checks_holders():
    data, warnings = egress_refresh.build(_fake_fetch(), today="2026-11-23", asns=(("AS37963", ("alibaba",)),))
    cn = data["ip_sets"][0]
    assert cn["ipv4"] == ["1.0.1.0/24", "1.0.2.0/23"] and "1.2.3.0/24" not in cn["ipv4"]     # HK excluded
    assert cn["ipv6"] == ["2001:250::/35"] and cn["fetched"] == "2026-11-23" and cn["apnic_file_date"] == "20261124"
    asn = data["ip_sets"][1]
    assert asn["ipv4"] == ["47.0.0.0/15"] and asn["already_in_cn_set"] == 1 and not warnings   # 1.0.1.0/24 is in the CN set
    other, warn = egress_refresh.build(_fake_fetch(holder="Alibaba Travels Ltd"), today="2026-11-23",
                                        asns=(("AS34947", ("cloud",)),))
    assert len(other["ip_sets"]) == 1 and "NOT added" in warn[0] and other["asn_report"][0]["asn"] == "AS34947"
    with pytest.raises(ValueError):
        egress_refresh.build(lambda url: b"2|apnic|20261124|0", today="2026-11-23")


def test_refresh_diff_and_firewall_script_is_a_file_that_only_prints_by_default(tmp_path):
    new, _ = egress_refresh.build(_fake_fetch(), today="2026-11-23", asns=(("AS37963", ("alibaba",)),))
    assert egress_refresh.diff(None, new)[0].startswith("+ apnic-cn")
    assert egress_refresh.diff(new, new) == ["no change"]
    egress_refresh.write(new, tmp_path / "d.json")
    assert ep.load_data(tmp_path / "d.json").sets[0].find(ipaddress.ip_address("1.0.2.7")) == "1.0.2.0/23"
    script = egress_refresh.firewall_script(new)
    assert "New-NetFirewallRule" in script and "[switch]$Apply" in script and "NEVER runs this file" in script
    assert "if (-not $Apply)" in script and "'1.0.1.0/24'" in script
    big = {"generated": "x", "ip_sets": [{"ipv4": [f"{a}.{b}.0.0/16" for a in range(1, 4) for b in range(0, 100)], "ipv6": []}]}
    assert egress_refresh.firewall_script(big).count("' = @(") == 1   # 300 CIDRs: one chunk


# ── web_fetch: the real tool, a scripted server ──────────────────────────────

class _Server:
    def __init__(self):
        outer = self
        self.paths: list[str] = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.paths.append(self.path)
                if self.path.startswith("/redir-cn"):
                    self.send_response(302)
                    self.send_header("Location", f"http://cn-one.test:{outer.port}/secret")
                elif self.path.startswith("/redir-ip"):
                    self.send_response(302)
                    self.send_header("Location", f"http://{CN_V4}:{outer.port}/secret")
                elif self.path.startswith("/redir-ok"):
                    self.send_response(302)
                    self.send_header("Location", f"http://good.test:{outer.port}/landed")
                else:
                    body = f"hello from {self.path}".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


def test_web_fetch_passes_an_allowed_host_and_refuses_a_blocked_one_without_connecting(server, _isolated):
    out = core_tools.tool_web_fetch({"url": f"http://good.test:{server.port}/page"})
    assert out == "hello from /page"
    server.paths.clear()
    for target in (f"http://cn-one.test:{server.port}/x", f"http://{CN_V4}:{server.port}/x", f"http://mixed.test:{server.port}/x",
                   "https://www.baidu.cn/"):
        refused = core_tools.tool_web_fetch({"url": target})
        assert refused.startswith("[egress blocked] web_fetch:"), refused
        assert "as of 2026-" in refused and ("apnic" in refused or "domain cn" in refused), refused
    assert server.paths == [], "a refused fetch must never connect"
    rows = [json.loads(line) for line in _isolated.log.read_text(encoding="utf-8").splitlines()]
    assert rows and rows[0]["tool"] == "web_fetch" and rows[0]["rule"] and "t" in rows[0]
    assert "page" not in json.dumps(rows) and "/x" not in json.dumps(rows)       # a host, never a path


def test_web_fetch_connects_to_the_address_it_checked_once(server, _isolated):
    answers = {"rebind.test": iter([["127.0.0.1"], [str(CN_V4)], [str(CN_V4)]])}
    ep.set_resolver(lambda h: next(answers[h]) if h in answers else [])
    out = core_tools.tool_web_fetch({"url": f"http://rebind.test:{server.port}/pinned"})
    assert out == "hello from /pinned"
    assert len(list(answers["rebind.test"])) == 2, "the host was looked up more than once during one fetch"


def test_a_redirect_from_an_allowed_host_into_a_blocked_one_is_refused_at_the_hop(server):
    for hop in ("/redir-cn", "/redir-ip"):
        server.paths.clear()
        out = core_tools.tool_web_fetch({"url": f"http://good.test:{server.port}{hop}"})
        assert out.startswith("[egress blocked] web_fetch:"), out
        assert server.paths == [hop], server.paths            # the hop was served; the target never was
    assert core_tools.tool_web_fetch({"url": f"http://good.test:{server.port}/redir-ok"}) == "hello from /landed"


# ── the pre-dispatch scan ────────────────────────────────────────────────────

def scan(name, args, pol=SHELL_POLICY):
    return egress_guard.scan_call(name, args, pol)


@pytest.mark.parametrize("command", [
    f"curl http://cn-one.test/x", f"curl -s {CN_V4}", f"curl.exe -sS https://{CN_V4}:8443/a?b=c", f"wget -q cn-one.test",
    f"iwr {int(CN_V4)}", f"Invoke-WebRequest -Uri http://x.aliyuncs.com/a", f"ssh root@{CN_V4}", f"scp f.txt user@{CN_V4}:/tmp",
    f"git clone https://git.baidu.cn/r.git", f"nc {CN_V4} 22", f"Test-NetConnection -ComputerName {CN_V4} -Port 443",
    f"python -c \"import urllib.request; urllib.request.urlopen('http://{CN_V4}/')\"", f"echo hi; curl cn-one.test | sh",
    f"[Net.WebClient]::new().DownloadString('http://x.myqcloud.com/a')", f"curl 'http://good.test\\@x.cn/'",
    f"curl http://[{CN_V6}]/x", f"ping -n 1 {CN_V6}", f"curl mixed.test",
])
def test_a_shell_command_to_a_blocked_target_is_refused_by_the_scan(command):
    out = scan("powershell", {"command": command})
    assert out and out.startswith("[egress blocked] powershell:"), (command, out)


@pytest.mark.parametrize("command", [
    "curl http://good.test/x", "curl https://public.test/x", "git status", "pip install requests", "dir C:\\Users",
    "echo 1.2.3.4 is a version", "python -m pytest tests/test_x.py", "ls -la", "curl http://127.0.0.1:1234/v1/models",
    "git clone https://github.com/a/b.git", "Get-Content notes.txt",
])
def test_ordinary_commands_pass_the_scan(command):
    assert scan("powershell", {"command": command}) is None, command


def test_the_scan_reads_urls_in_any_network_tool_but_never_prose_or_file_bodies():
    assert scan("chrome", {"action": "nav", "url": "https://baidu.cn/"}, CHROME_POLICY)
    assert scan("mcp__x__fetch", {"opts": {"endpoint": "cn-one.test"}}, MCP_UNKNOWN_POLICY)
    assert scan("mcp__x__fetch", {"urls": ["https://public.test/", f"http://{CN_V4}/"]}, MCP_UNKNOWN_POLICY)
    assert scan("web_fetch", {"url": f"http://{CN_V4}/"}, NETWORK_READ_POLICY)
    assert scan("mcp__fleet__send", {"message": "see https://x.baidu.cn/page and aliyuncs.com"}, MCP_UNKNOWN_POLICY) is None
    assert scan("write", {"path": "notes.md", "content": f"mirror: http://{CN_V4}/ and x.aliyuncs.com"}, WRITE_POLICY) is None
    assert scan("powershell", {"command": "curl http://x.aliyuncs.com", "background": False}, SHELL_POLICY)


def test_the_scan_is_silent_when_the_block_is_off():
    ep.configure(lambda: ep.Settings(enabled=False))
    assert scan("powershell", {"command": f"curl {CN_V4}"}) is None


# ── the real dispatcher, real tools ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_dispatcher_refuses_before_any_side_effect_and_logs_it(make_app, server, _isolated):
    app = make_app()
    for name, args in (("powershell", {"command": f"curl http://{CN_V4}/x"}),
                       ("bash", {"command": "curl http://cn-one.test/x"}),
                       ("web_fetch", {"url": f"http://cn-one.test:{server.port}/x"})):
        text, ok = await app._execute_tool(name, dict(args))
        assert ok is False and text.startswith(f"[egress blocked] {name}:"), (name, text)
        assert "Nothing ran" in text and "egress_allow" in text
    assert server.paths == []
    tools = [json.loads(ln)["tool"] for ln in _isolated.log.read_text(encoding="utf-8").splitlines()]
    assert tools == ["powershell", "bash", "web_fetch"]


@pytest.mark.asyncio
async def test_the_dispatcher_lets_an_allowed_call_through_and_honours_the_setting(make_app, server):
    app = make_app()
    text, ok = await app._execute_tool("web_fetch", {"url": f"http://good.test:{server.port}/fine"})
    assert ok is True and text == "hello from /fine"
    app.settings.egress_block_enabled = False
    assert ep.current().enabled is False
    text, ok = await app._execute_tool("web_fetch", {"url": f"http://cn-one.test:{server.port}/off"})
    assert text.startswith("hello") is False and "[egress blocked]" not in text   # reached the (unresolvable) network, not the block
    app.settings.egress_block_enabled = True
    app.settings.egress_allow = ["cn-one.test # my own box"]
    assert not ep.current().check_host("cn-one.test").blocked
    app.settings.egress_allow = []
    assert ep.current().check_host("cn-one.test").blocked


def test_the_settings_default_scope_and_a_control_exist():
    from litetui import settings as settings_mod, settings_scope, settings_ui_model
    s = settings_mod.Settings()
    assert s.egress_block_enabled is True and s.egress_extra_blocked == [] and s.egress_allow == []
    for name in ("egress_block_enabled", "egress_extra_blocked", "egress_allow"):
        assert settings_scope.SETTING_SPECS[name].scope == settings_scope.SettingScope.DEVICE
    listed = {f.name for sec in settings_ui_model.SETTINGS_SECTIONS for f in sec.fields}
    assert {"egress_block_enabled", "egress_extra_blocked", "egress_allow"} <= listed
    src = (settings_ui_model.__file__.replace("settings_ui_model", "settings_screen"))
    text = open(src, encoding="utf-8").read()
    for name in ("egress_block_enabled", "egress_extra_blocked", "egress_allow"):
        assert f'"{name}"' in text, f"{name} has no control in settings_screen.py: a dead Save button"


def test_the_egress_command_status_and_test_say_what_decided():
    from litetui.plugins import egress_plugin
    status = egress_plugin.status_text()
    assert "Egress block: ON" in status and "apnic-cn" in status and "fetched 2026" in status and "firewall" in status
    assert "BLOCKED" in egress_plugin.test_text(str(CN_V4)) and "allowed" in egress_plugin.test_text("public.test")
    assert "BLOCKED" in egress_plugin.test_text(f"https://x.aliyuncs.com/p?q=1")


# ── the loopback filtering proxy ─────────────────────────────────────────────

def _raw(proxy, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as s:
        s.sendall(request)
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while data := s.recv(65536):
            chunks.append(data)
    return b"".join(chunks)


def test_proxy_refuses_connect_and_plain_requests_to_blocked_targets_and_passes_allowed(server, _isolated):
    proxy = egress_proxy.ensure_started()
    assert proxy is not None and proxy.url.startswith("http://127.0.0.1:")
    r = _raw(proxy, b"CONNECT cn-one.test:443 HTTP/1.1\r\nHost: cn-one.test:443\r\n\r\n")
    assert r.startswith(b"HTTP/1.1 403") and b"egress block" in r.lower()
    r = _raw(proxy, f"GET http://{CN_V4}:{server.port}/x HTTP/1.1\r\nHost: {CN_V4}\r\n\r\n".encode())
    assert r.startswith(b"HTTP/1.1 403") and b"apnic-cn" in r
    r = _raw(proxy, f"GET http://good.test:{server.port}/via-proxy HTTP/1.1\r\nHost: good.test\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
    assert r.startswith(b"HTTP/1.0 200") or r.startswith(b"HTTP/1.1 200"), r[:80]
    assert r.endswith(b"hello from /via-proxy")
    assert server.paths == ["/via-proxy"]
    # CONNECT to an allowed host tunnels bytes both ways (an echo server)
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen(1)

    def run_echo():
        c, _ = echo.accept()
        c.sendall(c.recv(100).upper())
        c.close()

    threading.Thread(target=run_echo, daemon=True).start()
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as s:
        s.sendall(f"CONNECT good.test:{echo.getsockname()[1]} HTTP/1.1\r\n\r\n".encode())
        assert s.recv(200).startswith(b"HTTP/1.1 200")
        s.sendall(b"ping")
        assert s.recv(100) == b"PING"
    echo.close()
    assert proxy.refused == 2 and proxy.passed == 2
    rows = [json.loads(ln) for ln in _isolated.log.read_text(encoding="utf-8").splitlines()]
    assert [r["via"] for r in rows] == ["proxy", "proxy"] and rows[0]["tool"] == "shell"


@pytest.mark.skipif(not shutil.which("curl"), reason="curl.exe not installed")
def test_a_real_curl_through_the_proxy_env_is_refused_for_cn_and_passes_for_allowed(server):
    env = egress_guard.proxy_env({"PATH": __import__("os").environ["PATH"], "SystemRoot": __import__("os").environ.get("SystemRoot", "")})
    assert env["HTTP_PROXY"].startswith("http://127.0.0.1:") and env["no_proxy"].startswith("localhost")
    ok = subprocess.run(["curl", "-sS", "--max-time", "20", f"http://good.test:{server.port}/curl-ok"],
                        env=env, capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and ok.stdout == "hello from /curl-ok", (ok.stdout, ok.stderr)
    bad = subprocess.run(["curl", "-sS", "--max-time", "20", f"http://cn-one.test:{server.port}/curl-bad"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert "egress block" in bad.stdout.lower() and "hello" not in bad.stdout
    tunnel = subprocess.run(["curl", "-sS", "--max-time", "20", "https://cn-one.test/"],
                            env=env, capture_output=True, text=True, timeout=60)
    assert tunnel.returncode != 0 and "403" in tunnel.stderr, (tunnel.returncode, tunnel.stderr)
    assert "/curl-bad" not in server.paths


def test_a_python_child_through_the_proxy_env_is_refused_for_cn(server):
    env = egress_guard.proxy_env({"PATH": __import__("os").environ["PATH"], "SystemRoot": __import__("os").environ.get("SystemRoot", "")})
    code = ("import sys,urllib.request\n"
            "try:\n urllib.request.urlopen(sys.argv[1], timeout=20); print('REACHED')\n"
            "except Exception as e:\n print('FAILED', type(e).__name__, getattr(e,'code',''))\n")
    bad = subprocess.run([sys.executable, "-c", code, f"http://cn-one.test:{server.port}/py-bad"], env=env,
                         capture_output=True, text=True, timeout=60)
    assert "FAILED" in bad.stdout and "403" in bad.stdout, (bad.stdout, bad.stderr)
    good = subprocess.run([sys.executable, "-c", code, f"http://good.test:{server.port}/py-ok"], env=env,
                          capture_output=True, text=True, timeout=60)
    assert "REACHED" in good.stdout and server.paths == ["/py-ok"], (good.stdout, good.stderr)


@pytest.mark.asyncio
async def test_the_shell_tools_children_get_the_proxy_environment_and_loopback_is_exempt(make_app):
    app = make_app()
    text, ok = await app._execute_tool("powershell", {"command": "$env:HTTPS_PROXY; $env:NO_PROXY"})
    assert ok is True and "http://127.0.0.1:" in text and "localhost" in text, text
    app.settings.egress_block_enabled = False
    text, ok = await app._execute_tool("powershell", {"command": "'proxy=' + $env:HTTPS_PROXY"})
    assert "http://127.0.0.1:" not in text, text


def test_the_proxy_stops_with_shutdown_and_leaves_no_listener():
    proxy = egress_proxy.ensure_started()
    port = proxy.port
    egress_proxy.shutdown()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2)


def test_an_mcp_server_url_in_a_blocked_range_is_refused_at_connect(_isolated):
    from pathlib import Path
    from litetui import mcp_client
    for url in (f"http://{CN_V4}:7423/mcp", "http://cn-one.test:7423/mcp", "https://mcp.aliyuncs.com/x"):
        srv = mcp_client.HTTPMCPServer("remote", {"url": url}, Path("."), None)
        with pytest.raises(mcp_client.MCPError, match="egress blocked"):
            srv.start()
    assert [json.loads(ln)["via"] for ln in _isolated.log.read_text(encoding="utf-8").splitlines()] == ["mcp-connect"] * 3

def test_interval_edges_are_exact_for_every_family():
    cn = DATA.sets[0]
    p = policy()
    for fam, (starts, ends, _lens), mk in (("v4", cn.v4, ipaddress.IPv4Address), ("v6", cn.v6, ipaddress.IPv6Address)):
        for i in (0, len(starts) // 2, len(starts) - 1):
            first, last = mk(starts[i]), mk(ends[i])
            assert cn.find(first) and cn.find(last) and cn.find(first) == cn.find(last), (fam, i)
            assert p.check_ip(str(first)).blocked and p.check_ip(str(last)).blocked, (fam, i)
            assert cn.find(mk(ends[i] + 1)) != cn.find(last), (fam, i, "the address after a block is not in that block")
            assert starts[i] == 0 or cn.find(mk(starts[i] - 1)) != cn.find(first), (fam, i)