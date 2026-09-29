"""A started MCP server that never lists its tools is retried, then named. T0124 (was T1097).

Measured 2026-09-27 (mcp.log): three VibeUE proxies started 15:33:27/31/35 and each
answered LiteTUI's `tools/list` ~8 ms after start. The proxy discovers its upstream
tools only on a later CallToolRequest; two seats made one (15:34:10, 15:34:56), the
third had no tool to call, so it never would. `start()` took the first list as final,
marked the server "connected", and nothing looked again - a silently toolless seat.

NO REAL SERVER IS SPAWNED: the subject is the manager's bookkeeping and the app's
retry loop, so `_build` returns a scripted stub (same approach as test_mcp_lifecycle).
"""

from __future__ import annotations

import pytest

from litetui import app as app_mod
from litetui.mcp_client import MCPBusy, MCPManager

pytestmark = pytest.mark.real_mcp_load


class ScriptedServer:
    """`tools` lists two tools; `empty` answers with none; `late` answers `initialize`
    then never lists (start() raises after the handshake); `refused` never starts."""

    def __init__(self, name, script):
        self.name, self.script = name, script
        self.tools: list[dict] = []
        self.initialized = False

    def start(self):
        if self.script == "refused":
            raise RuntimeError("connection refused")
        self.initialized = True
        if self.script == "late":
            raise RuntimeError("timed out after 30s waiting for response")
        self.tools = [{"name": "alpha"}, {"name": "beta"}] if self.script == "tools" else []

    def stop(self):
        pass


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    """A manager whose n-th server is `scripts[n]` (the last script repeats)."""
    m = MCPManager(tmp_path)
    m.scripts = []
    m.built = []

    def _build(self, name, sc):
        i = min(len(m.built), len(m.scripts) - 1)
        srv = ScriptedServer(name, m.scripts[i])
        m.built.append(srv)
        return srv

    monkeypatch.setattr(MCPManager, "_build", _build)
    m.configs = {"srv": {"command": "x"}}
    return m


@pytest.fixture
def app(mgr, monkeypatch):
    a = app_mod.LiteTUI()
    a.mcp = mgr
    a.said = []
    a.system_message = lambda text: a.said.append(str(text))
    a._update_header = lambda: None
    monkeypatch.setattr(app_mod, "MCP_RETRY_DELAYS", (0.0, 0.0, 0.0))
    return a


def test_a_connected_server_with_no_tools_is_named_by_describe(mgr):
    mgr.scripts = ["empty"]
    assert mgr.connect("srv") is None
    row = mgr.describe()[0]
    assert row["state"] == "connected" and row["tools"] == 0
    assert "0 tools" in row["error"], f"an empty server reads as healthy: {row!r}"
    mgr.scripts = ["tools"]
    mgr.reconnect("srv")
    assert mgr.describe()[0]["error"] is None


@pytest.mark.parametrize("script,expect", [
    ("empty", True), ("late", True), ("tools", False), ("refused", False),
])
def test_toolless_started_separates_started_from_never_started(mgr, script, expect):
    mgr.scripts = [script]
    mgr.connect("srv")
    assert mgr.toolless_started("srv") is expect


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["empty", "late"], ids=["late_list", "slow_list_timeout"])
async def test_a_late_tools_list_recovers_and_reaches_the_dispatch_map(app, mgr, first):
    mgr.scripts = [first, "tools"]
    mgr.connect("srv")
    assert mgr.tool_count("srv") == 0
    await app._mcp_retry_toolless(["srv"])
    assert mgr.tool_count("srv") == 2 and len(mgr.built) == 2
    assert "mcp__srv__alpha" in app.mcp_dispatch, "specs are per-turn; the dispatch map is cached"
    assert any("after retry" in s for s in app.said) and not any("[!]" in s for s in app.said)


@pytest.mark.asyncio
@pytest.mark.parametrize("script", ["empty", "late"])
async def test_a_server_that_never_lists_is_retried_a_bounded_number_of_times_then_named(app, mgr, script):
    mgr.scripts = [script]
    mgr.connect("srv")
    await app._mcp_retry_toolless(["srv"])
    assert len(mgr.built) == 1 + len(app_mod.MCP_RETRY_DELAYS)
    assert app.said and "[!] mcp srv" in app.said[-1] and "no tools" in app.said[-1]


@pytest.mark.asyncio
async def test_a_refused_server_never_started_and_is_not_retried(app, mgr):
    mgr.scripts = ["refused"]
    mgr.connect("srv")
    await app._mcp_retry_toolless(["srv"])
    assert len(mgr.built) == 1 and not app.said


@pytest.mark.asyncio
async def test_a_busy_manager_costs_a_round_not_the_loop(app, mgr):
    mgr.scripts = ["empty"]
    mgr.connect("srv")

    def busy(name):
        raise MCPBusy("MCP maintenance in progress")

    mgr.reconnect = busy
    await app._mcp_retry_toolless(["srv"])
    assert "[!] mcp srv" in app.said[-1]


# ── a human decision DURING the retry wait wins (leader's lifecycle concern, T0124) ──
def _act_once_during_the_wait(monkeypatch, act):
    """The retry sleeps 5/15/45 s. Run `act` (a /mcp verb or a setting) at the first sleep."""
    import asyncio
    real, done = asyncio.sleep, []

    async def sleep(delay):
        if not done:
            done.append(1)
            act()
        await real(0)

    monkeypatch.setattr(app_mod.asyncio, "sleep", sleep)


@pytest.mark.asyncio
async def test_an_explicit_disconnect_during_the_wait_is_not_undone(app, mgr, monkeypatch):
    mgr.scripts = ["empty"]
    mgr.connect("srv")
    _act_once_during_the_wait(monkeypatch, lambda: mgr.disconnect("srv"))
    await app._mcp_retry_toolless(["srv"])
    assert "srv" not in mgr.servers, f"a human stopped it; the retry reconnected it ({len(mgr.built)} builds)"


@pytest.mark.asyncio
async def test_a_server_disabled_during_the_wait_is_not_reconnected(app, mgr, monkeypatch):
    mgr.scripts = ["empty"]
    mgr.connect("srv")
    _act_once_during_the_wait(monkeypatch, lambda: setattr(app.settings, "mcp_disabled_servers", ["srv"]))
    await app._mcp_retry_toolless(["srv"])
    assert len(mgr.built) == 1, f"disabled during the wait, yet rebuilt {len(mgr.built)} times"


@pytest.mark.asyncio
async def test_disconnect_clears_the_late_failure_mark(app, mgr):
    mgr.scripts = ["late"]
    mgr.connect("srv")
    assert "srv" in mgr.late_failures
    mgr.disconnect("srv")
    assert "srv" not in mgr.late_failures, "a stopped server keeps its retry eligibility"


@pytest.mark.asyncio
@pytest.mark.parametrize("act", ["disconnect", "disable"])
async def test_a_human_decision_landing_after_the_reconnect_finishes_is_not_announced(app, mgr, act):
    """reconnect() holds the maintenance claim, so a /mcp disconnect DURING it is refused
    (MCPBusy), not lost. The remaining window is between its return and our announce."""
    mgr.scripts = ["empty", "tools"]
    mgr.connect("srv")
    real = mgr.reconnect

    def reconnect_then_the_human_acts(name):
        err = real(name)
        if act == "disconnect":
            mgr.disconnect(name)
        else:
            app.settings.mcp_disabled_servers = [name]
        return err

    mgr.reconnect = reconnect_then_the_human_acts
    await app._mcp_retry_toolless(["srv"])
    assert len(mgr.built) == 2
    assert not any("after retry" in s for s in app.said), app.said
