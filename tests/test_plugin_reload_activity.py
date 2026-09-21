"""Tests for litetui.plugin_reload_activity.produce_activity.

Hermetic: SimpleNamespace app, real WorkerState. The CANCELLABLE slot is
process-global, so an autouse fixture saves/restores it around every test.

Run only this file:
    python -m pytest tests/test_plugin_reload_activity.py
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from textual.worker import WorkerState

import litetui.plugin_reload_activity as pa
from litetui.plugin_reload_state import can_commit_candidate


@pytest.fixture(autouse=True)
def _reset_cancellable():
    from litetui import ttyguard
    saved = ttyguard.CANCELLABLE.get("proc")
    ttyguard.CANCELLABLE["proc"] = None
    try:
        yield
    finally:
        ttyguard.CANCELLABLE["proc"] = saved


def _w(group: str, state=WorkerState.RUNNING) -> NS:
    return NS(group=group, state=state)


def _app(**over) -> NS:
    base = dict(workers=[], store=NS(pending=False, loading=False), screen_stack=[object()])
    base.update(over)
    return NS(**base)


def _clear(**over):
    """produce_activity on an otherwise-idle app; children probe says False."""
    over.setdefault("children_pending", lambda: False)
    cp = over.pop("children_pending")
    return pa.produce_activity(_app(**over), children_pending=cp)


def _fields(report):
    return {f: getattr(report.snapshot, f) for f in
            ("turn_active", "tool_active", "management_active", "children_active", "mcp_active")}


# ── baseline ─────────────────────────────────────────────────────────────────
def test_idle_app_all_clear():
    r = _clear()
    assert _fields(r) == {f: False for f in _fields(r)}
    assert r.unreadable == ()
    assert can_commit_candidate(r.snapshot) is True
    assert r.app_active is False


# ── worker groups ────────────────────────────────────────────────────────────
def test_chat_worker_is_turn():
    r = _clear(workers=[_w("chat")])
    assert r.snapshot.turn_active is True


def test_tasks_worker_is_tool():
    r = _clear(workers=[_w("tasks")])
    assert r.snapshot.tool_active is True


def test_mcp_worker_is_mcp_and_management():
    r = _clear(workers=[_w("mcp")])
    assert r.snapshot.mcp_active is True
    assert r.snapshot.management_active is True


def test_child_wake_worker_is_children():
    r = _clear(workers=[_w("child-wake")])
    assert r.snapshot.children_active is True


def test_init_worker_is_management():
    r = _clear(workers=[_w("init")])
    assert r.snapshot.management_active is True


def test_unknown_group_is_conservative_management():
    r = _clear(workers=[_w("some-future-group")])
    assert r.snapshot.management_active is True


def test_pending_worker_counts_as_busy():
    r = _clear(workers=[_w("chat", state=WorkerState.PENDING)])
    assert r.snapshot.turn_active is True


def test_terminal_worker_is_ignored():
    r = _clear(workers=[_w("chat", state=WorkerState.SUCCESS),
                        _w("tasks", state=WorkerState.CANCELLED)])
    assert can_commit_candidate(r.snapshot) is True


def test_workers_absent_is_required_and_forces_busy():
    app = NS(store=NS(pending=False, loading=False), screen_stack=[object()])  # no .workers
    r = pa.produce_activity(app, children_pending=lambda: False)
    assert "app.workers" in r.unreadable
    assert all(getattr(r.snapshot, f) for f in
               ("turn_active", "tool_active", "management_active", "children_active", "mcp_active"))


# ── store (required) ─────────────────────────────────────────────────────────
def test_store_none_is_unreadable_busy():
    r = pa.produce_activity(_app(store=None), children_pending=lambda: False)
    assert "app.store" in r.unreadable
    assert r.snapshot.management_active is True
    assert r.snapshot.turn_active is True


def test_store_pending_is_busy_not_unreadable():
    r = _clear(store=NS(pending=True, loading=False))
    assert r.snapshot.management_active is True and r.snapshot.turn_active is True
    assert "app.store" not in r.unreadable


def test_store_loading_is_busy():
    r = _clear(store=NS(pending=False, loading=True))
    assert r.snapshot.turn_active is True


# ── screen stack (required) ──────────────────────────────────────────────────
def test_modal_open_is_management():
    r = _clear(screen_stack=[object(), object()])
    assert r.snapshot.management_active is True


def test_screen_stack_absent_is_unreadable_management():
    app = NS(workers=[], store=NS(pending=False, loading=False))  # no screen_stack
    r = pa.produce_activity(app, children_pending=lambda: False)
    assert "app.screen_stack" in r.unreadable
    assert r.snapshot.management_active is True


# ── agent operations (optional lazy) ─────────────────────────────────────────
def test_agent_operations_live_is_children():
    ops = NS(tasks={"x": NS(done=lambda: False)})
    r = _clear(_agent_operations=ops)
    assert r.snapshot.children_active is True


def test_agent_operations_all_done_is_clear():
    ops = NS(tasks={"x": NS(done=lambda: True)})
    r = _clear(_agent_operations=ops)
    assert r.snapshot.children_active is False


def test_agent_operations_absent_is_ok():
    r = _clear()  # no _agent_operations
    assert r.snapshot.children_active is False


def test_agent_operations_malformed_is_children_busy():
    ops = NS(tasks={"x": object()})  # object() has no .done()
    r = _clear(_agent_operations=ops)
    assert r.snapshot.children_active is True
    assert "app._agent_operations" in r.unreadable


# ── monitor threads (optional lazy) ──────────────────────────────────────────
def test_monitor_threads_nonempty_is_tool():
    r = _clear(_monitor_threads={object()})
    assert r.snapshot.tool_active is True


def test_monitor_threads_empty_is_clear():
    r = _clear(_monitor_threads=set())
    assert r.snapshot.tool_active is False


def test_monitor_threads_absent_is_ok():
    r = _clear()
    assert r.snapshot.tool_active is False


# ── CANCELLABLE process scope ────────────────────────────────────────────────
def test_cancellable_is_tool_busy_process_scoped():
    from litetui import ttyguard
    ttyguard.CANCELLABLE["proc"] = object()
    r = _clear()
    assert r.snapshot.tool_active is True
    # process-global signal only => the gate blocks, but this App is not "active"
    assert r.app_active is False
    assert any(x.scope == "process" for x in r.reasons)


# ── children probe (exact bool; unknown => busy) ─────────────────────────────
def test_children_probe_false_is_clear():
    assert pa.produce_activity(_app(), children_pending=lambda: False).snapshot.children_active is False


def test_children_probe_none_default_is_busy():
    r = pa.produce_activity(_app())   # no probe supplied
    assert r.snapshot.children_active is True


def test_children_probe_nonbool_is_busy():
    r = pa.produce_activity(_app(), children_pending=lambda: "yes")
    assert r.snapshot.children_active is True


def test_children_probe_raises_is_busy():
    def boom():
        raise RuntimeError("db down")
    r = pa.produce_activity(_app(), children_pending=boom)
    assert r.snapshot.children_active is True


# ── tasks.live_for_app (background procs + subagents) ─────────────────────────
def test_live_for_app_bg_and_subs(monkeypatch):
    monkeypatch.setattr("litetui.tasks.live_for_app", lambda app: (["sub"], ["bg"]))
    r = _clear()
    assert r.snapshot.tool_active is True       # bg
    assert r.snapshot.children_active is True    # subs


def test_live_for_app_raises_is_unreadable_busy(monkeypatch):
    def boom(app):
        raise RuntimeError("task store corrupt")
    monkeypatch.setattr("litetui.tasks.live_for_app", boom)
    r = _clear()
    assert r.snapshot.tool_active is True and r.snapshot.children_active is True
    assert "tasks.live_for_app" in r.unreadable
