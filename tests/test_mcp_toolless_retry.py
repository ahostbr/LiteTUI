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


# ── the SHIPPED /mcp stop route, during a real (unpatched) retry backoff ──────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("script", ["tools", "empty"], ids=["CONTROL_healthy_server", "retry_pending"])
async def test_a_real_mcp_stop_during_the_retry_backoff_is_served_not_refused(tmp_path, monkeypatch, script):
    """T0124, Orchestrator's characterisation: type the real `/mcp stop` (mcp_manage._cmd_mcp, with
    its own busy gate) while the retry is asleep in its 30 s backoff. The control arm has a
    healthy server, so no retry is pending: it says whether the test app itself is idle."""
    import json

    from _settle import settle_until

    from litetui.plugins import mcp_manage

    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"srv": {"command": "x"}}}), encoding="utf-8")
    monkeypatch.setattr(MCPManager, "_build", lambda self, name, sc: ScriptedServer(name, script))
    monkeypatch.setattr(app_mod, "MCP_RETRY_DELAYS", (30.0, 30.0, 30.0))
    # This characterizes eager startup retry; lazy seats intentionally never dial at boot.
    a = app_mod.LiteTUI(mcp_lazy=False)
    a.available_models, a.model_id = ["a-model"], "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    said: list[str] = []
    a.system_message = lambda text: said.append(str(text))

    async with a.run_test(size=(120, 40)) as pilot:
        assert await settle_until(pilot, lambda: "srv" in a.mcp.servers, n=200), said
        # A fresh conversation is "pending" and the gate reads that as a turn in progress,
        # for any /mcp verb, retry or not. Born, the way app.py does it at materialisation.
        a.store.pending = False
        # The test app's own inbox poller is not the subject (no fleet here); in production it
        # is registered idle infra while it polls.
        for w in list(a.workers):
            if w.group == "inbox":
                w.cancel()
        await pilot.pause()
        from litetui.plugin_reload_activity import produce_activity
        gate = [(r.source, r.detail) for r in produce_activity(a).reasons]
        mcp_manage._cmd_mcp(a, "mcp", "stop srv")
        served = await settle_until(pilot, lambda: "srv" not in a.mcp.servers, n=60)
    assert served, (f"/mcp stop was not served during the backoff; the app said: {said!r}; "
                    f"the gate's reasons: {gate!r}")
