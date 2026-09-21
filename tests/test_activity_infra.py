"""The narrow idle-infra classification in the shared activity producer.

A proven idle poller (cron.monitor, the inbox seat monitor's post-registration
poll) registers its OWN Worker object via idle_infra_phase while it sleeps, and
produce_activity excludes exactly that object — by identity. Everything else
still counts: a DIFFERENT worker in the same group, an unknown group, a busy
store, an open modal. This is not a name/group bypass and not an id() bypass.

Run only this file:
    python -m pytest tests/test_activity_infra.py
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from textual.worker import WorkerState

import litetui.plugin_reload_activity as pa


def _worker(group="mcp", *, state=WorkerState.RUNNING, name="w"):
    return NS(group=group, state=state, name=name)


def _app(workers, *, pending=False, loading=False, depth=1, idle=None):
    app = NS(
        workers=list(workers),
        store=NS(pending=pending, loading=loading),
        screen_stack=[object() for _ in range(depth)],
        convo_id="c1",
    )
    if idle is not None:
        app._idle_infra_workers = idle
    return app


def _snap(app, **kw):
    return pa.produce_activity(app, **kw).snapshot


# ── idle_infra_phase: register on enter, clear on exit AND on cancellation ─────
def test_idle_infra_phase_registers_then_clears(monkeypatch):
    w = _worker(group="cron")
    monkeypatch.setattr("textual.worker.get_current_worker", lambda: w)
    app = NS()
    assert pa._idle_infra_workers(app) == []
    with pa.idle_infra_phase(app):
        assert any(w is x for x in pa._idle_infra_workers(app))
    assert not any(w is x for x in pa._idle_infra_workers(app))


def test_idle_infra_phase_clears_on_cancellation(monkeypatch):
    import asyncio
    w = _worker(group="cron")
    monkeypatch.setattr("textual.worker.get_current_worker", lambda: w)
    app = NS()
    with pytest.raises(asyncio.CancelledError):
        with pa.idle_infra_phase(app):
            raise asyncio.CancelledError()          # the poll await is cancelled
    assert pa._idle_infra_workers(app) == []        # cleaned up in finally


def test_idle_infra_phase_nested_same_worker_refcounts(monkeypatch):
    w = _worker(group="cron")
    monkeypatch.setattr("textual.worker.get_current_worker", lambda: w)
    app = NS()
    with pa.idle_infra_phase(app):
        with pa.idle_infra_phase(app):
            assert sum(1 for x in pa._idle_infra_workers(app) if x is w) == 2
        # inner exit must NOT clear the outer phase
        assert any(x is w for x in pa._idle_infra_workers(app))
    assert not any(x is w for x in pa._idle_infra_workers(app))


def test_idle_infra_phase_noop_without_a_worker(monkeypatch):
    monkeypatch.setattr("textual.worker.get_current_worker", lambda: None)
    app = NS()
    with pa.idle_infra_phase(app):               # nothing registered, no crash
        assert pa._idle_infra_workers(app) == []


# ── produce_activity: exclude ONLY the registered idle worker, by identity ─────
def test_registered_idle_worker_is_excluded():
    w = _worker(group="cron")
    assert _snap(_app([w], idle=[w])).management_active is False


def test_a_different_same_group_worker_still_blocks():
    idle = _worker(group="mcp", name="dialog-host")
    other = _worker(group="mcp", name="a-real-reconcile")
    snap = _snap(_app([idle, other], idle=[idle]))
    assert snap.mcp_active is True               # the OTHER mcp worker still counts


def test_unknown_group_worker_still_blocks():
    assert _snap(_app([_worker(group="cron")], idle=[])).management_active is True


def test_missing_registry_excludes_nothing():
    # No _idle_infra_workers attribute at all -> fail-closed, worker counts.
    assert _snap(_app([_worker(group="cron")])).management_active is True


def test_malformed_registry_excludes_nothing():
    # A non-list registry must never whitelist a worker.
    app = _app([_worker(group="cron")])
    app._idle_infra_workers = "not-a-list"
    assert _snap(app).management_active is True


def test_exclusion_is_by_identity_not_equality():
    class EqAlways:
        group = "cron"
        state = WorkerState.RUNNING
        name = "w"
        def __eq__(self, other): return True        # overloaded eq must not fool us
        def __hash__(self): return 0
    running = EqAlways()
    registered = EqAlways()                          # equal by __eq__, different object
    snap = _snap(_app([running], idle=[registered]))
    assert snap.management_active is True            # running is NOT `is` registered


# ── store + modal remain blockers (unchanged by the infra work) ───────────────
def test_busy_store_still_blocks_even_with_idle_workers():
    w = _worker(group="cron")
    snap = _snap(_app([w], idle=[w], pending=True))
    assert snap.turn_active is True and snap.management_active is True


def test_own_modal_excluded_but_a_second_modal_blocks():
    own = object()
    stack = [object(), own]                          # base + the dialog's own modal
    app = NS(workers=[], store=NS(pending=False, loading=False),
             screen_stack=stack, convo_id="c1", _idle_infra_workers=[])
    assert _snap(app, ignore_screen=own).management_active is False
    stack.append(object())                           # a SECOND modal on top
    assert _snap(app, ignore_screen=own).management_active is True
