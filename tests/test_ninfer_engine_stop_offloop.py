"""/engine stop off-loop + start/stop mutual exclusion. Fake app/backend/Event, no real
subprocess. Proves: loop stays responsive while the blocking stop runs off-loop;
cancellation JOINS the worker before discard; a cancelled START keeps its claim until the
spawn thread joins and does not lose an engine it published; the claim never strands
(released on any terminal state incl. cancel-before-first-step); messages never over-claim.
"""
import asyncio
import threading

import pytest

from litetui import agent_preparation, ninfer_backend
from litetui.ninfer_backend import NInferBackend
from litetui.llm_backend import TerminalShutdown, BackendError
from litetui.plugins.model_switch import _cmd_engine


# ── await_preparation behaviour our stop/start rely on (Flux's shared helper) ─

@pytest.mark.asyncio
async def test_blocking_call_runs_offloop_loop_stays_responsive():
    release = threading.Event()
    ran = []

    def blocking():
        release.wait(2)
        return "done"

    task = asyncio.ensure_future(agent_preparation.await_preparation(blocking))
    await asyncio.sleep(0.01)
    ran.append(True)                       # the loop ran while the call blocked in a thread
    release.set()
    assert await task == "done" and ran == [True]


@pytest.mark.asyncio
async def test_cancel_joins_worker_before_discard():
    release = threading.Event()
    finished = threading.Event()

    def blocking():
        release.wait(2)
        finished.set()
        return "done"

    task = asyncio.ensure_future(agent_preparation.await_preparation(blocking))
    await asyncio.sleep(0.01)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.wait(2)                # the callback settled — joined, not orphaned


# ── begin_stop / _starting claim ────────────────────────────────────────────

def _bare():
    import threading
    b = object.__new__(NInferBackend)
    b._starting = False
    b._stopping = False
    b._owned = None
    b._lifecycle_lock = threading.Lock()
    return b


def test_begin_stop_claims_then_rejects_double():
    b = _bare()
    assert b.begin_stop() is True
    assert b.begin_stop() is False
    b.end_stop()
    assert b._stopping is False


def test_begin_stop_fails_closed_while_starting():
    b = _bare()
    b._starting = True                     # a start is in flight (claim held across join)
    assert b.begin_stop() is False
    assert b._stopping is False


@pytest.mark.asyncio
async def test_start_refused_while_stopping():
    b = _bare()
    b._stopping = True
    with pytest.raises(BackendError):
        await b.start_engine()


@pytest.mark.asyncio
async def test_start_refused_while_already_starting():
    b = _bare()
    b._starting = True
    with pytest.raises(BackendError):
        await b.start_engine()


@pytest.mark.asyncio
async def test_cancelled_start_holds_claim_until_join_and_keeps_owner(monkeypatch):
    # A start blocked in its spawn thread: the claim must stay until the thread JOINS,
    # a stop must be refused meanwhile, and an engine the thread published must survive
    # the cancellation (not be lost).
    release = threading.Event()
    published = object()

    class _Owned:
        proc = type("P", (), {"pid": 5})()
        host = "http://h/v1"
        model_id = "m"

    def fake_start(settings, *, healthy, notice=None):
        release.wait(2)
        return _Owned()

    monkeypatch.setattr(ninfer_backend.ninfer_engine, "start", fake_start)
    b = _bare()
    b._settings = object()
    b._health = lambda host, timeout=2.0: True

    task = asyncio.ensure_future(b.start_engine())
    await asyncio.sleep(0.02)
    assert b._starting is True                       # claim held during the spawn
    assert b.begin_stop() is False                   # a stop is refused while starting
    task.cancel()
    release.set()                                    # let the spawn thread finish
    with pytest.raises(asyncio.CancelledError):
        await task
    assert b._starting is False                       # released only AFTER the join
    assert isinstance(b._owned, _Owned)               # the published engine survived


# ── stop_engine message reflects the TerminalShutdown proof ─────────────────

def _owned_backend(result):
    b = _bare()
    b._owned = type("O", (), {"host": "http://127.0.0.1:9000/v1"})()
    b.shutdown = lambda: result
    return b


def test_stop_message_confirmed_is_scoped_to_assigned_members():
    b = _owned_backend(TerminalShutdown(owned=True, main_exited=True, retained=False, tree="confirmed"))
    msg = b.stop_engine()
    assert "assigned members are confirmed gone" in msg
    assert "whole tree" not in msg and "entire" not in msg   # never over-claims


def test_stop_message_main_only_flags_unconfirmed_children():
    b = _owned_backend(TerminalShutdown(owned=True, main_exited=True, retained=False, tree="unknown"))
    assert "child-process VRAM release is unconfirmed" in b.stop_engine()


def test_stop_message_retained_is_explicitly_not_stopped():
    b = _owned_backend(TerminalShutdown(owned=True, main_exited=False, retained=True, tree="unknown"))
    msg = b.stop_engine()
    assert "NOT confirmed" in msg and "stopped the engine" not in msg


def test_registry_no_handle_fails_closed_and_preserves_record(monkeypatch):
    b = _bare()
    b._owned = None
    monkeypatch.setattr(ninfer_backend.ninfer_engine, "registered_entry",
                        lambda: {"owner": "litetui", "kind": "ninfer", "pid": 123, "baseUrl": "http://h"})
    msg = b.stop_engine()                            # real stop_registered fails closed
    assert "cannot safely verify" in msg and "record is preserved" in msg
    assert "was not started by LiteTUI" not in msg   # our own engine is NOT mislabelled external
    assert "stopped the engine" not in msg


def test_stop_registered_fails_closed_no_kill_no_unregister(monkeypatch):
    from litetui import ninfer_engine
    side = []
    monkeypatch.setattr(ninfer_engine, "unregister_host", lambda h: side.append(("unregister", h)))
    monkeypatch.setattr(ninfer_engine.ttyguard, "run", lambda *a, **k: side.append(("taskkill", a)))
    good = {"owner": "litetui", "kind": "ninfer", "pid": 123, "baseUrl": "http://h"}
    assert ninfer_engine.stop_registered(good) is False
    assert side == []                                # no taskkill, no unregister — record kept
    assert ninfer_engine.stop_registered({"owner": "other", "kind": "ninfer", "pid": 1}) is False
    assert ninfer_engine.stop_registered({"owner": "litetui", "kind": "ninfer", "pid": 0}) is False
    assert ninfer_engine.stop_registered({"owner": "litetui", "kind": "ninfer", "pid": True}) is False


# ── /engine stop command wrapper ────────────────────────────────────────────

class _FakeBackend:
    name = "ninfer"

    def __init__(self):
        self._stopping = False
        self.ended = False
        self.stop_calls = 0

    def begin_stop(self):
        if self._stopping:
            return False
        self._stopping = True
        return True

    def end_stop(self):
        self.ended = True
        self._stopping = False

    def stop_engine(self):
        self.stop_calls += 1
        return "stopped X"


class _FakeWorker:
    """Mimics the bit of textual.worker.Worker we depend on: a `_task` set by run_worker."""
    def __init__(self, task):
        self._task = task
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeApp:
    def __init__(self, backend, *, worker_raises=False, task_none=False):
        self.backend = backend
        self.messages = []
        self.worker_raises = worker_raises
        self.task_none = task_none
        self.last_worker = None

    def system_message(self, m):
        self.messages.append(m)

    def run_worker(self, coro, **kw):
        if self.worker_raises:
            raise RuntimeError("scheduler down")     # command must coro.close()
        if self.task_none:
            self.last_worker = _FakeWorker(None)     # command must fail closed
            return self.last_worker
        self.last_worker = _FakeWorker(asyncio.ensure_future(coro))
        return self.last_worker


@pytest.mark.asyncio
async def test_command_stop_runs_as_worker_and_releases_after_join():
    app = _FakeApp(_FakeBackend())
    _cmd_engine(app, "/engine", "stop")
    assert app.backend._stopping is True and app.last_worker is not None
    await app.last_worker._task
    await asyncio.sleep(0)                            # let the done-callback fire
    assert app.backend.stop_calls == 1 and app.backend.ended is True
    assert app.messages == ["stopped X"]


@pytest.mark.asyncio
async def test_command_stop_cancel_before_body_still_releases_claim():
    app = _FakeApp(_FakeBackend())
    _cmd_engine(app, "/engine", "stop")
    app.last_worker._task.cancel()                   # cancel before the worker body runs
    await asyncio.sleep(0)
    await asyncio.sleep(0)                            # let the done-callback fire
    assert app.backend.ended is True                 # claim released via task terminal state
    assert app.backend.stop_calls == 0               # stop_engine never ran


def test_command_stop_rejects_double():
    backend = _FakeBackend()
    backend._stopping = True
    app = _FakeApp(backend)
    _cmd_engine(app, "/engine", "stop")
    assert app.last_worker is None and any("busy" in m for m in app.messages)


def test_command_stop_schedule_failure_clears_claim_no_phantom():
    app = _FakeApp(_FakeBackend(), worker_raises=True)
    _cmd_engine(app, "/engine", "stop")
    assert app.backend.ended is True                 # claim released, no phantom stop
    assert any("could not start the engine-stop worker" in m for m in app.messages)


def test_command_stop_untracked_worker_fails_closed():
    app = _FakeApp(_FakeBackend(), task_none=True)   # worker._task is not a real Task
    _cmd_engine(app, "/engine", "stop")
    assert app.backend.ended is True                 # fail closed: released, no strand
    assert app.last_worker.cancelled is True         # worker cancelled before abandoning
    assert any("could not be tracked" in m for m in app.messages)


@pytest.mark.asyncio
async def test_real_textual_worker_task_is_a_real_task_and_observable():
    # The fake worker cannot certify Worker._task's lifecycle; a MOUNTED Textual app can.
    from textual.app import App

    released = []

    async def _work():
        return "ok"

    app = App()
    async with app.run_test():
        worker = app.run_worker(_work(), name="probe", exit_on_error=False)
        task = getattr(worker, "_task", None)
        assert isinstance(task, asyncio.Task)        # real asyncio.Task, as the command requires
        task.add_done_callback(lambda _t: released.append(True))
        await worker.wait()
        await asyncio.sleep(0)                        # let the done-callback fire
    assert released == [True]                         # terminal-state cleanup fires on the real task


@pytest.mark.asyncio
async def test_start_refused_while_an_owned_engine_is_tracked():
    b = _bare()
    b._owned = object()                     # an unresolved owned engine exists
    with pytest.raises(BackendError):
        await b.start_engine()              # must not overwrite/lose the handle
