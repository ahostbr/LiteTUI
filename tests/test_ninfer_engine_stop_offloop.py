"""/engine stop off-loop + stop-in-progress claim. Fake app/backend/Event, no real stop.

Proves the UI loop stays responsive while the blocking stop runs off-loop, cancellation
JOINS the worker before discard, the claim rejects a double stop and an in-flight start,
and the completion message reflects the TerminalShutdown proof (never a false "stopped").
"""
import asyncio
import threading

import pytest

from litetui import agent_preparation, ninfer_backend
from litetui.ninfer_backend import NInferBackend
from litetui.llm_backend import TerminalShutdown, BackendError
from litetui.plugins.model_switch import _cmd_engine


# ── await_preparation behaviour our stop relies on (Flux's shared helper) ────

@pytest.mark.asyncio
async def test_blocking_stop_runs_offloop_loop_stays_responsive():
    release = threading.Event()
    ran_concurrently = []

    def blocking_stop():
        release.wait(2)
        return "stopped"

    task = asyncio.ensure_future(agent_preparation.await_preparation(blocking_stop))
    await asyncio.sleep(0.01)
    ran_concurrently.append(True)          # the loop ran while the stop blocked in a thread
    release.set()
    assert await task == "stopped" and ran_concurrently == [True]


@pytest.mark.asyncio
async def test_cancel_joins_worker_before_discard():
    release = threading.Event()
    finished = threading.Event()

    def blocking_stop():
        release.wait(2)
        finished.set()
        return "stopped"

    task = asyncio.ensure_future(agent_preparation.await_preparation(blocking_stop))
    await asyncio.sleep(0.01)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.wait(2)                # the owned callback settled — joined, not orphaned


# ── begin_stop / end_stop claim ─────────────────────────────────────────────

def _bare():
    b = object.__new__(NInferBackend)
    b._stopping = False
    b._vram_owner = None
    return b


def test_begin_stop_claims_then_rejects_double():
    b = _bare()
    assert b.begin_stop() is True
    assert b.begin_stop() is False         # double stop rejected, not overlapped
    b.end_stop()
    assert b._stopping is False


def test_begin_stop_fails_closed_on_inflight_start():
    b = _bare()
    b._vram_owner = object()               # a start holds the vram gate
    assert b.begin_stop() is False
    assert b._stopping is False             # claim NOT taken while a start is in flight


@pytest.mark.asyncio
async def test_start_engine_refused_while_stopping():
    b = _bare()
    b._stopping = True
    with pytest.raises(BackendError):
        await b.start_engine()


# ── stop_engine message reflects the TerminalShutdown proof ─────────────────

def _owned_backend(result):
    b = _bare()
    b._owned = type("O", (), {"host": "http://127.0.0.1:9000/v1"})()
    b.shutdown = lambda: result
    return b


def test_stop_message_confirmed_says_stopped():
    b = _owned_backend(TerminalShutdown(owned=True, main_exited=True, retained=False, tree="confirmed"))
    assert b.stop_engine() == "stopped the engine LiteTUI started at http://127.0.0.1:9000/v1."


def test_stop_message_main_only_flags_unconfirmed_children():
    b = _owned_backend(TerminalShutdown(owned=True, main_exited=True, retained=False, tree="unknown"))
    msg = b.stop_engine()
    assert "child-process VRAM release is unconfirmed" in msg


def test_stop_message_retained_is_explicitly_not_stopped():
    b = _owned_backend(TerminalShutdown(owned=True, main_exited=False, retained=True, tree="unknown"))
    msg = b.stop_engine()
    assert "NOT confirmed" in msg and "stopped the engine" not in msg


def test_registry_stop_message_says_signalled_not_stopped(monkeypatch):
    b = _bare()
    b._owned = None
    monkeypatch.setattr(ninfer_backend.ninfer_engine, "registered_entry",
                        lambda: {"owner": "litetui", "pid": 123, "baseUrl": "http://h"})
    monkeypatch.setattr(ninfer_backend.ninfer_engine, "stop_registered", lambda entry: True)
    msg = b.stop_engine()
    assert "signalled" in msg and "exit not confirmed" in msg and "stopped the engine" not in msg


# ── /engine stop command wrapper ────────────────────────────────────────────

class _FakeBackend:
    name = "ninfer"

    def __init__(self, *, can_begin=True):
        self._can_begin = can_begin
        self._stopping = False
        self.ended = False
        self.stop_calls = 0

    def begin_stop(self):
        if self._stopping or not self._can_begin:
            return False
        self._stopping = True
        return True

    def end_stop(self):
        self.ended = True
        self._stopping = False

    def stop_engine(self):
        self.stop_calls += 1
        return "stopped X"


class _FakeApp:
    def __init__(self, backend, worker_raises=False):
        self.backend = backend
        self.messages = []
        self.worker_raises = worker_raises
        self.scheduled = None

    def system_message(self, m):
        self.messages.append(m)

    def run_worker(self, coro, **kw):
        if self.worker_raises:
            raise RuntimeError("scheduler down")
        self.scheduled = coro                # capture for the test to drive


@pytest.mark.asyncio
async def test_command_stop_schedules_offloop_and_ends_after_join():
    app = _FakeApp(_FakeBackend())
    _cmd_engine(app, "/engine", "stop")
    assert app.scheduled is not None and app.backend._stopping is True
    await app.scheduled                       # run the worker coroutine
    assert app.backend.stop_calls == 1 and app.backend.ended is True
    assert app.messages == ["stopped X"]


def test_command_stop_rejects_double():
    backend = _FakeBackend()
    backend._stopping = True                   # a stop is already running
    app = _FakeApp(backend)
    _cmd_engine(app, "/engine", "stop")
    assert app.scheduled is None
    assert any("busy" in m for m in app.messages)


def test_command_stop_schedule_failure_clears_claim_no_phantom():
    app = _FakeApp(_FakeBackend(), worker_raises=True)
    _cmd_engine(app, "/engine", "stop")
    assert app.backend.ended is True           # claim released, no phantom stop
    assert any("could not start the engine-stop worker" in m for m in app.messages)
