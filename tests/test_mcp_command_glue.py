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

    def _run_worker(coro, **k):
        events.append("worker")
        coro.close()          # we assert scheduling, not execution, here

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
