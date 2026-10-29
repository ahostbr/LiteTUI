"""Readback waits for the exact owned continuation worker; no provider calls."""
from __future__ import annotations

import asyncio

import pytest
import test_rust_queue_continuation
from textual.worker import Worker, WorkerCancelled, WorkerState

from litetui import gui_rpc, seat_authority

saved_app = test_rust_queue_continuation.saved_app
setup = test_rust_queue_continuation.setup


def request(app, command):
    return {"type": "gui.restored_input.settled", "id": "read-settled",
            "session_id": app.convo_id, "operation_id": command["id"], "entry_id": command["entry_id"]}


@pytest.mark.asyncio
async def test_settlement_waits_for_worker_retirement_and_serializes_mutation(saved_app, monkeypatch):
    app = saved_app
    command, _, _ = setup(app, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    class ControlledWorker:
        async def wait(self):
            entered.set()
            await release.wait()

    monkeypatch.setattr(app, "_stream", lambda **_: ControlledWorker())
    await gui_rpc.async_dispatch(app, command)
    monkeypatch.setattr(app, "_chat_running", lambda: not release.is_set())
    pending = asyncio.create_task(gui_rpc.async_dispatch(app, request(app, command)))
    await asyncio.wait_for(entered.wait(), 2)
    assert not pending.done() and app._gui_management_busy
    with pytest.raises(ValueError, match="management"):
        await gui_rpc.async_dispatch(app, command)
    release.set()
    value = await asyncio.wait_for(pending, 2)
    assert value["held"] == 0 and value["entries"] == []
    assert value["operation_id"] == command["id"] and value["entry_id"] == "saved-input"
    assert value["continuation_outcome"] == "accepted"
    assert not app._gui_management_busy


@pytest.mark.asyncio
async def test_known_refusal_has_no_worker_and_returns_still_held_input(saved_app, monkeypatch):
    app = saved_app
    command, _, _ = setup(app, monkeypatch)
    monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: "below floor")
    assert (await gui_rpc.async_dispatch(app, command))["turn"] == "refused"
    value = await gui_rpc.async_dispatch(app, request(app, command))
    assert value["held"] == 1 and value["entries"][0]["can_continue"]
    assert value["continuation_outcome"] == "refused"
    assert value["snapshot"] != command["snapshot"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["operation_id", "entry_id", "session_id"])
async def test_foreign_settlement_is_rejected(saved_app, monkeypatch, field):
    app = saved_app
    command, _, _ = setup(app, monkeypatch)
    monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: "below floor")
    await gui_rpc.async_dispatch(app, command)
    cmd = request(app, command)
    cmd[field] = "foreign"
    with pytest.raises(ValueError, match="Unknown continuation"):
        await gui_rpc.async_dispatch(app, cmd)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["conversation", "backend", "model", "attempt", "busy", "worker-failed"])
async def test_changed_or_failed_worker_never_grants_queue_readback(saved_app, monkeypatch, fault):
    app = saved_app
    command, _, _ = setup(app, monkeypatch)

    class ControlledWorker:
        async def wait(self):
            if fault == "conversation":
                app.conversation = list(app.conversation)
            elif fault == "backend":
                app.backend = object()
            elif fault == "model":
                app.model_id = "changed"
            elif fault == "attempt":
                app._restored_input_attempt = dict(app._restored_input_attempt)
            elif fault == "busy":
                monkeypatch.setattr(app, "_chat_running", lambda: True)
            else:
                raise RuntimeError("worker failed")

    monkeypatch.setattr(app, "_stream", lambda **_: ControlledWorker())
    await gui_rpc.async_dispatch(app, command)
    with pytest.raises((ValueError, RuntimeError)):
        await gui_rpc.async_dispatch(app, request(app, command))
    assert not app._gui_management_busy


@pytest.mark.asyncio
async def test_missing_worker_is_not_a_retired_worker(saved_app, monkeypatch):
    app = saved_app
    command, _, _ = setup(app, monkeypatch)  # controlled _stream returns None
    await gui_rpc.async_dispatch(app, command)
    with pytest.raises(ValueError, match="worker is unavailable"):
        await gui_rpc.async_dispatch(app, request(app, command))


@pytest.mark.asyncio
async def test_cancelled_worker_is_retired_but_caller_cancellation_cannot_stop_it(saved_app, monkeypatch):
    app = saved_app
    command, _, _ = setup(app, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        await release.wait()

    # Exercise the installed Textual Worker.wait implementation, which catches
    # asyncio cancellation internally. The read must not propagate into its task.
    worker: Worker[None] = Worker(app, work)
    worker._task = asyncio.create_task(work())
    worker.state = WorkerState.RUNNING
    monkeypatch.setattr(app, "_stream", lambda **_: worker)
    await gui_rpc.async_dispatch(app, command)
    await asyncio.wait_for(entered.wait(), 2)
    read = asyncio.create_task(gui_rpc.async_dispatch(app, request(app, command)))
    await asyncio.sleep(0)
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read
    assert not worker._task.done() and not worker._task.cancelling()
    release.set()
    await worker._task
    assert (await gui_rpc.async_dispatch(app, request(app, command)))["held"] == 0

    class CancelledWorker:
        async def wait(self):
            raise WorkerCancelled("already retired")
    app._restored_input_attempt["worker"] = CancelledWorker()
    assert (await gui_rpc.async_dispatch(app, request(app, command)))["held"] == 0
