"""App-glue for MCP maintenance: the /mcp reconcile command + gates, the
_execute_tool dispatch pause, and the worker flag lifecycle.

Fake app + fake mcp — no real transports/network. (The _submit_text / _flush
send-gate edits reuse the existing queue/retry machinery and are exercised by
the mounted-host suite; flagged in the report.)

Run only this file:
    python -m pytest tests/test_mcp_command_glue.py
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from textual.worker import WorkerState

import litetui.plugins.mcp_manage as mm

#: The scheduling-only tests use a fake run_worker that records the schedule and
#: never runs the worker, so the coroutine run_guarded wraps is intentionally
#: never awaited here (in production _guarded runs and awaits/closes it).
pytestmark = pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning")


@pytest.fixture(autouse=True)
def _idle_children(monkeypatch):
    monkeypatch.setattr("litetui.plugin_reload_children.children_pending", lambda *a: False)


class _FakeMcp:
    def __init__(self, reconcile_ret=None):
        self._ret = reconcile_ret or {"a": "connected"}
        self.reconciled = 0

    def reconcile(self):
        self.reconciled += 1
        return self._ret

    def reload_configs(self):
        return {}

    def connect(self, n):
        return None


def _app(*, native=False, maint=False, workers=None, reconcile_ret=None):
    msgs: list[str] = []
    events: list = []
    backend = NS(name="codex", app_server=object()) if native else NS(name="lmstudio")

    class _FakeTask:
        # run_guarded attaches a done-callback to worker._task; the scheduling
        # tests never run the worker, so this just accepts the callback.
        def add_done_callback(self, cb):
            pass

    def _run_worker(coro, **k):
        events.append("worker")
        coro.close()          # the _guarded wrapper; we assert scheduling, not execution
        return NS(_task=_FakeTask(), cancel=lambda: None)

    app = NS(
        backend=backend, _mcp_maintenance=maint, workers=workers or [],
        store=NS(pending=False, loading=False), screen_stack=[object()],
        convo_id="c1", mcp=_FakeMcp(reconcile_ret), system_message=msgs.append,
        rebuild_mcp_dispatch=lambda: events.append("rebuild"),
        run_worker=_run_worker,
    )
    app._msgs = msgs
    app._events = events
    return app


def _last(app):
    return app._msgs[-1] if app._msgs else ""


# ── /mcp reconcile command + gate ─────────────────────────────────────────────
def test_reconcile_happy_claims_and_schedules():
    app = _app()
    mm._cmd_mcp(app, "/mcp", "reconcile")
    assert app._mcp_maintenance is True          # claimed synchronously
    assert "worker" in app._events               # off-loop worker scheduled
    assert "started" in _last(app).lower()


def test_reconcile_rejected_when_already_maintaining():
    app = _app(maint=True)
    mm._cmd_mcp(app, "/mcp", "reconcile")
    assert "worker" not in app._events
    assert "maintenance is in progress" in _last(app).lower()


def test_reconcile_native_requires_restart():
    app = _app(native=True)
    mm._cmd_mcp(app, "/mcp", "reconcile")
    assert app._mcp_maintenance is False and "worker" not in app._events
    assert "restart" in _last(app).lower()


def test_reconcile_defers_when_busy():
    app = _app(workers=[NS(group="chat", state=WorkerState.RUNNING)])
    mm._cmd_mcp(app, "/mcp", "reconcile")
    assert app._mcp_maintenance is False and "worker" not in app._events
    assert "defer" in _last(app).lower()


def test_connect_verb_gated_native_restart():
    app = _app(native=True)
    mm._cmd_mcp(app, "/mcp", "connect somesrv")
    assert "restart" in _last(app).lower()


def test_connect_verb_rejected_during_maintenance():
    app = _app(maint=True)
    mm._cmd_mcp(app, "/mcp", "connect somesrv")
    assert "maintenance is in progress" in _last(app).lower()


# ── worker flag lifecycle ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reconcile_worker_clears_flag_after_settle_and_rebuilds():
    app = _app(maint=True, reconcile_ret={"a": "connected", "b": "disconnected"})
    await mm._reconcile_worker(app)
    assert app._mcp_maintenance is False          # cleared only after settle
    assert "rebuild" in app._events               # dispatch rebuilt in finally
    assert app.mcp.reconciled == 1
    assert "a: connected" in _last(app) and "b: disconnected" in _last(app)


@pytest.mark.asyncio
async def test_reconcile_worker_clears_flag_even_on_error():
    app = _app(maint=True)
    def _boom():
        raise RuntimeError("kaboom")
    app.mcp.reconcile = _boom
    await mm._reconcile_worker(app)
    assert app._mcp_maintenance is False          # flag never stuck
    assert "rebuild" in app._events
    assert "failed" in _last(app).lower()


# ── dispatch gate ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_execute_tool_paused_during_maintenance():
    from litetui.app import LiteTUI
    app = NS(tools_enabled=True, _mcp_maintenance=True, _rpc_emit=lambda e: None)
    out, ok = await LiteTUI._execute_tool(app, "anytool", {})
    assert ok is False and "maintenance" in out.lower()


# ── maintenance completion Event + parent-wake preclaim guard (batch 3) ───────
@pytest.mark.asyncio
async def test_reconcile_worker_sets_completion_event():
    import asyncio
    app = _app(maint=True)
    app._mcp_maintenance_done = asyncio.Event()
    await mm._reconcile_worker(app)
    assert app._mcp_maintenance is False and app._mcp_maintenance_done.is_set()


@pytest.mark.asyncio
async def test_wake_parent_defers_before_claim_during_maintenance():
    from litetui.agent_parent_wake import wake_parent
    claimed = []
    receipts = NS(claim_wake=lambda p, c: (claimed.append((p, c)) or ["id"]),
                  finish_wake=lambda *a: None)
    app = NS(_chat_running=lambda: False, _gui_quitting=False, _mcp_maintenance=True,
             _pending_input=[], _stop_requested=False, backend=NS(name="lmstudio"),
             convo_id="c1")
    out = await wake_parent(app, parent="p", receipts=receipts)
    assert out == [] and claimed == []       # deferred BEFORE claiming any receipt


# ── _stream MCP-maintenance gate (_await_mcp_maintenance) ──────────────────────
# The gate is extracted from _stream so its negatives are testable without a
# live turn. e942e60 had a fail-open (`done is None => break => run`); these pin
# every path OpenBolt named: missing/stale Event => defer (not spin, not run),
# and convo/backend/stop/quit change during the wait => defer (not normal-return).
def _gate_app(**over):
    base = dict(_mcp_maintenance=True, convo_id="c1", backend=object(),
                _stop_requested=False, _gui_quitting=False)
    base.update(over)
    return NS(**base)


@pytest.mark.asyncio
async def test_gate_noop_when_not_maintaining():
    from litetui.app import LiteTUI
    await LiteTUI._await_mcp_maintenance(_gate_app(_mcp_maintenance=False))  # returns


@pytest.mark.asyncio
async def test_gate_defers_when_event_missing():
    from litetui.app import LiteTUI
    from litetui.turn_deferral import TurnDeferred
    with pytest.raises(TurnDeferred):                       # flag True, no Event
        await LiteTUI._await_mcp_maintenance(_gate_app())


@pytest.mark.asyncio
async def test_gate_defers_when_event_already_set():
    import asyncio
    from litetui.app import LiteTUI
    from litetui.turn_deferral import TurnDeferred
    ev = asyncio.Event(); ev.set()
    with pytest.raises(TurnDeferred):                       # would busy-spin otherwise
        await LiteTUI._await_mcp_maintenance(_gate_app(_mcp_maintenance_done=ev))


@pytest.mark.asyncio
async def test_gate_resumes_when_flag_cleared_and_event_set():
    import asyncio
    from litetui.app import LiteTUI
    ev = asyncio.Event()
    app = _gate_app(_mcp_maintenance_done=ev)

    async def finish():
        await asyncio.sleep(0)                              # let the gate reach wait()
        app._mcp_maintenance = False                        # order as _reconcile_worker
        ev.set()

    await asyncio.gather(LiteTUI._await_mcp_maintenance(app), finish())  # no raise


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [
    lambda app: setattr(app, "convo_id", "c2"),
    lambda app: setattr(app, "backend", object()),
    lambda app: setattr(app, "_stop_requested", True),
    lambda app: setattr(app, "_gui_quitting", True),
])
async def test_gate_defers_on_context_change_during_wait(mutate):
    import asyncio
    from litetui.app import LiteTUI
    from litetui.turn_deferral import TurnDeferred
    ev = asyncio.Event()
    app = _gate_app(_mcp_maintenance_done=ev)               # flag stays True

    async def change():
        await asyncio.sleep(0)
        mutate(app)                                         # context moves mid-wait
        ev.set()

    with pytest.raises(TurnDeferred):
        await asyncio.gather(LiteTUI._await_mcp_maintenance(app), change())


# ── rebuild-blocked: a failed dispatch rebuild BLOCKS, never resumes stale ─────
@pytest.mark.asyncio
async def test_reconcile_worker_blocks_on_rebuild_failure():
    import asyncio
    app = _app(maint=True)
    app._mcp_maintenance_done = asyncio.Event()

    def boom():
        raise RuntimeError("map broke")

    app.rebuild_mcp_dispatch = boom
    await mm._reconcile_worker(app)
    assert app._mcp_maintenance is False              # in-flight flag cleared
    assert app._mcp_maintenance_done.is_set()         # waiters woken (no infinite wait)
    assert app._mcp_dispatch_blocked                   # map marked stale
    assert "blocked" in _last(app).lower()             # reported, not swallowed


@pytest.mark.asyncio
async def test_reconcile_worker_clears_block_on_rebuild_success():
    app = _app(maint=True)
    app._mcp_dispatch_blocked = "stale from an earlier failed rebuild"
    await mm._reconcile_worker(app)
    assert app._mcp_dispatch_blocked is None           # recovery: current map installed
    assert "rebuild" in app._events


@pytest.mark.asyncio
async def test_gate_defers_when_dispatch_blocked_at_entry():
    from litetui.app import LiteTUI
    from litetui.turn_deferral import TurnDeferred
    app = _gate_app(_mcp_maintenance=False, _mcp_dispatch_blocked="stale")
    with pytest.raises(TurnDeferred):                  # blocked even with no reconcile in flight
        await LiteTUI._await_mcp_maintenance(app)


@pytest.mark.asyncio
async def test_gate_defers_when_blocked_after_reconcile():
    import asyncio
    from litetui.app import LiteTUI
    from litetui.turn_deferral import TurnDeferred
    ev = asyncio.Event()
    app = _gate_app(_mcp_maintenance_done=ev)          # maint True, not blocked yet

    async def finish():
        await asyncio.sleep(0)
        app._mcp_maintenance = False
        app._mcp_dispatch_blocked = "rebuild failed"   # blocked AFTER settle
        ev.set()

    with pytest.raises(TurnDeferred):
        await asyncio.gather(LiteTUI._await_mcp_maintenance(app), finish())


@pytest.mark.asyncio
async def test_execute_tool_blocked_when_dispatch_stale():
    from litetui.app import LiteTUI
    app = NS(tools_enabled=True, _mcp_maintenance=False,
             _mcp_dispatch_blocked="stale", _rpc_emit=lambda e: None)
    out, ok = await LiteTUI._execute_tool(app, "anytool", {})
    assert ok is False and "blocked" in out.lower()


# ── read-path MCPBusy feedback + bounded/sanitized error reasons (piece 2) ─────
def test_safe_reload_reports_busy_without_echoing_exception():
    from litetui.mcp_client import MCPBusy
    app = _app()

    def boom():
        raise MCPBusy("busy: internal claim detail")

    app.mcp.reload_configs = boom
    msg = mm._safe_reload(app)
    assert msg and "maintenance is in progress" in msg.lower()
    assert "internal claim detail" not in msg          # bounded: no exception text


def test_safe_reload_ok_returns_none():
    assert mm._safe_reload(_app()) is None


def test_list_verb_surfaces_busy_instead_of_raising():
    from litetui.mcp_client import MCPBusy
    app = _app()

    def boom():
        raise MCPBusy("busy")

    app.mcp.reload_configs = boom
    mm._cmd_mcp(app, "/mcp", "list")                    # must not raise
    assert "maintenance is in progress" in _last(app).lower()


@pytest.mark.asyncio
async def test_reconcile_block_reason_is_bounded_no_secret_leak():
    import asyncio
    app = _app(maint=True)
    app._mcp_maintenance_done = asyncio.Event()
    secret = "http://internal.host/secret-token-abc123"

    def boom():
        raise RuntimeError(secret)

    app.rebuild_mcp_dispatch = boom
    await mm._reconcile_worker(app)
    assert app._mcp_dispatch_blocked
    assert secret not in app._mcp_dispatch_blocked      # sanitized
    assert "RuntimeError" in app._mcp_dispatch_blocked  # type kept
    assert secret not in _last(app)                     # nor in the user message


# ── verb routing through the coordinator (piece 3) ─────────────────────────────
def test_connect_verb_schedules_offloop_not_sync():
    app = _app()
    mm._cmd_mcp(app, "/mcp", "connect somesrv")
    assert app._mcp_maintenance is True      # claimed synchronously by the gate
    assert "worker" in app._events           # routed off-loop, not a sync app.mcp.connect


def test_remove_verb_schedules_offloop():
    app = _app()
    mm._cmd_mcp(app, "/mcp", "remove somesrv")
    assert app._mcp_maintenance is True and "worker" in app._events


@pytest.mark.asyncio
async def test_mutation_worker_runs_op_settles_and_reports():
    import asyncio
    app = _app(maint=True)
    app._mcp_maintenance_done = asyncio.Event()
    calls = []
    await mm._mutation_worker(app, lambda: calls.append("op"), lambda r: "Connected x.")
    assert calls == ["op"]                        # op ran off-loop
    assert app._mcp_maintenance is False          # settled
    assert app._mcp_maintenance_done.is_set()     # waiters woken
    assert "rebuild" in app._events               # dispatch rebuilt
    assert "Connected x." in _last(app)


@pytest.mark.asyncio
async def test_mutation_worker_reports_busy_bounded():
    import asyncio
    from litetui.mcp_client import MCPBusy
    app = _app(maint=True)
    app._mcp_maintenance_done = asyncio.Event()

    def op():
        raise MCPBusy("internal busy detail")

    await mm._mutation_worker(app, op, lambda r: "should not be used")
    assert "maintenance is in progress" in _last(app).lower()
    assert "internal busy detail" not in _last(app)
    assert app._mcp_maintenance is False          # settled despite the raise


@pytest.mark.asyncio
async def test_mutation_worker_settles_and_bounds_op_error():
    import asyncio
    app = _app(maint=True)
    app._mcp_maintenance_done = asyncio.Event()
    secret = "http://internal/token-xyz"

    def op():
        raise RuntimeError(secret)

    await mm._mutation_worker(app, op, lambda r: "unused")
    assert app._mcp_maintenance is False
    assert "failed" in _last(app).lower()
    assert secret not in _last(app)               # bounded — no raw error text


# ── shared gate helper + scheduling-failure + activity-exception (piece 4) ─────
def test_blocked_reason_none_when_idle():
    assert mm._mutation_blocked_reason(_app()) is None


def test_blocked_reason_reports_maintenance():
    msg = mm._mutation_blocked_reason(_app(maint=True))
    assert msg and "maintenance is in progress" in msg.lower()


def test_blocked_reason_reports_native_restart():
    msg = mm._mutation_blocked_reason(_app(native=True))
    assert msg and "restart" in msg.lower()


def test_blocked_reason_defers_when_activity_probe_raises(monkeypatch):
    app = _app()

    def _boom(*a, **k):
        raise RuntimeError("probe boom detail")

    monkeypatch.setattr("litetui.plugin_reload_activity.produce_activity", _boom)
    msg = mm._mutation_blocked_reason(app)
    assert msg and "try again shortly" in msg.lower()   # fail-closed defer, not proceed
    assert "probe boom detail" not in msg                # bounded


def test_claim_and_run_unsticks_maintenance_on_scheduling_failure():
    app = _app()

    def boom_run_worker(coro, **k):
        raise RuntimeError("cannot schedule")

    app.run_worker = boom_run_worker
    ok = mm._claim_and_run(app, mm._reconcile_worker(app))
    assert ok is False
    assert app._mcp_maintenance is False               # un-stuck, not wedged forever
    assert app._mcp_maintenance_done.is_set()          # waiters woken
    assert "could not start" in _last(app).lower()


# ── run_guarded lifecycle (immediate pre-first-step cancel, fail-closed) ───────
def _loop_run_worker(tasks):
    import asyncio
    def rw(coro, **k):
        t = asyncio.get_event_loop().create_task(coro)
        tasks.append(t)
        return NS(_task=t, cancel=lambda: t.cancel())
    return rw


@pytest.mark.asyncio
async def test_run_guarded_runs_op_and_fires_cleanup_once():
    import asyncio
    from litetui.agent_preparation import run_guarded
    ran, cleaned, tasks = [], [], []

    async def op():
        ran.append(1)

    w = run_guarded(NS(run_worker=_loop_run_worker(tasks)), op(),
                    group="mcp", cleanup=lambda: cleaned.append(1))
    assert w is not None
    await asyncio.sleep(0.02)
    assert ran == [1] and cleaned == [1]


@pytest.mark.asyncio
async def test_run_guarded_cleanup_on_cancel_before_first_step():
    import asyncio
    from litetui.agent_preparation import run_guarded
    ran, cleaned, tasks = [], [], []

    async def op():
        ran.append(1)

    run_guarded(NS(run_worker=_loop_run_worker(tasks)), op(),
                group="mcp", cleanup=lambda: cleaned.append(1))
    tasks[0].cancel()                       # cancel BEFORE the loop runs the guarded coro
    await asyncio.sleep(0.02)
    assert ran == []                        # op never entered the thread
    assert cleaned == [1]                   # cleanup still fired exactly once


@pytest.mark.asyncio
async def test_run_guarded_missing_task_fails_closed():
    from litetui.agent_preparation import run_guarded
    cleaned, reported, ran = [], [], []

    async def op():
        ran.append(1)

    def rw(coro, **k):
        return NS(_task=None, cancel=lambda: None)   # no task hook

    w = run_guarded(NS(run_worker=rw), op(), group="mcp",
                    cleanup=lambda: cleaned.append(1), report=lambda e: reported.append(e))
    assert w is None and cleaned == [1] and reported == [None] and ran == []


@pytest.mark.asyncio
async def test_run_guarded_schedule_failure_fails_closed():
    from litetui.agent_preparation import run_guarded
    cleaned, reported = [], []

    async def op():
        pass

    def rw(coro, **k):
        raise RuntimeError("cannot schedule")

    w = run_guarded(NS(run_worker=rw), op(), group="mcp",
                    cleanup=lambda: cleaned.append(1),
                    report=lambda e: reported.append(type(e).__name__))
    assert w is None and cleaned == [1] and reported == ["RuntimeError"]
