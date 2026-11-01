"""The captured manual Run RPC joins its exact worker and preserves its receipt."""
from __future__ import annotations

import asyncio

import pytest
from test_rust_backend_transition import switch_app as controlled_app
from textual.worker import WorkerCancelled, WorkerFailed

from litetui import background_input, gui_rpc, scheduler, seat_authority

switch_app = controlled_app
pytestmark = pytest.mark.asyncio


def prepare(app, monkeypatch, *, captured=True):
    job = scheduler.Job(prompt="controlled scheduled prompt", schedule="* * * * *", id="controlled-job", run_count=41)
    app.jobs[:] = [job]
    monkeypatch.setattr(background_input, "enabled", lambda owner: owner is app and captured)
    monkeypatch.setattr(scheduler, "refresh", lambda jobs, root: None)
    monkeypatch.setattr(seat_authority, "withheld", lambda *args: None)
    return job, {"type": "gui.jobs.run", "id": "manual-run", "job_id": job.id}


def receipt(job, staged=False):
    result = {"delivered": not staged, "job_id": job.id, "invocation_id": "11111111-2222-4333-8444-555555555555"}
    if staged:
        result.update(awaiting_work_capture=True, work_recapture_required=True,
                      work_shutdown={"exact": "old owned bridge", "sequence": 7})
    else:
        result["run_count"] = 42
    return result


@pytest.mark.parametrize("staged", [False, True])
async def test_run_waits_for_exact_worker_and_preserves_delivery_or_staging_receipt(switch_app, monkeypatch, staged):
    app = switch_app
    job, cmd = prepare(app, monkeypatch)
    expected = receipt(job, staged)
    entered, release = asyncio.Event(), asyncio.Event()
    workers = []
    async def invoke():
        entered.set()
        await release.wait()
        return expected
    def fire(selected, *, manual):
        assert selected is job and manual is True
        worker = app.run_worker(invoke(), group="captured-job-test", exit_on_error=False)
        workers.append(worker)
        return worker
    monkeypatch.setattr(app, "_fire_job", fire)
    pending = asyncio.create_task(gui_rpc.async_dispatch(app, cmd))
    await asyncio.wait_for(entered.wait(), 3)
    assert not pending.done() and len(workers) == 1 and not workers[0].is_finished
    release.set()
    result = await asyncio.wait_for(pending, 3)
    assert result == expected and result is not expected
    assert workers[0].is_finished and job.run_count == 41
    assert result["delivered"] is (not staged)


@pytest.mark.parametrize("failure", ["failed", "cancelled"])
async def test_worker_failure_and_cancellation_never_become_a_delivered_ack(switch_app, monkeypatch, failure):
    app = switch_app
    job, cmd = prepare(app, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    workers = []
    async def invoke():
        entered.set()
        await release.wait()
        raise RuntimeError("Canonical job execution failed")
    def fire(*args, **kwargs):
        worker = app.run_worker(invoke(), group="captured-job-test", exit_on_error=False)
        workers.append(worker)
        return worker
    monkeypatch.setattr(app, "_fire_job", fire)
    pending = asyncio.create_task(gui_rpc.async_dispatch(app, cmd))
    await asyncio.wait_for(entered.wait(), 3)
    if failure == "cancelled":
        workers[0].cancel()
    else:
        release.set()
    with pytest.raises(WorkerCancelled if failure == "cancelled" else WorkerFailed):
        await asyncio.wait_for(pending, 3)
    assert workers[0].is_finished and job.run_count == 41


async def test_rpc_cancellation_joins_accepted_invocation_without_cancelling_or_replaying_it(switch_app, monkeypatch):
    app = switch_app
    job, cmd = prepare(app, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    workers = []
    async def invoke():
        entered.set()
        await release.wait()
        return receipt(job)
    def fire(*args, **kwargs):
        worker = app.run_worker(invoke(), group="captured-job-test", exit_on_error=False)
        workers.append(worker)
        return worker
    monkeypatch.setattr(app, "_fire_job", fire)
    pending = asyncio.create_task(gui_rpc.async_dispatch(app, cmd))
    await asyncio.wait_for(entered.wait(), 3)
    for _ in range(2):
        pending.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not pending.done() and not workers[0].is_finished
    release.set()
    assert await asyncio.wait_for(pending, 3) == receipt(job)
    assert len(workers) == 1 and workers[0].is_finished


@pytest.mark.parametrize("bad", [True, object()], ids=["boolean", "truthy-object"])
async def test_captured_run_refuses_truthy_objects_without_treating_them_as_delivery(switch_app, monkeypatch, bad):
    app = switch_app
    _job, cmd = prepare(app, monkeypatch)
    monkeypatch.setattr(app, "_fire_job", lambda *args, **kwargs: bad)
    with pytest.raises(TypeError, match="exact source worker"):
        await gui_rpc.async_dispatch(app, cmd)


async def test_direct_sync_dispatch_refuses_captured_run_before_scheduling(switch_app, monkeypatch):
    app = switch_app
    _job, cmd = prepare(app, monkeypatch)
    called = []
    monkeypatch.setattr(app, "_fire_job", lambda *args, **kwargs: called.append(True))
    with pytest.raises(ValueError, match="asynchronous dispatch"):
        gui_rpc.dispatch(app, cmd)
    assert not called


@pytest.mark.parametrize("delivered", [False, True])
async def test_inactive_bridge_keeps_legacy_synchronous_run_behavior(switch_app, monkeypatch, delivered):
    app = switch_app
    job, cmd = prepare(app, monkeypatch, captured=False)
    calls = []
    def fire(selected, *, manual):
        calls.append((selected.id, manual))
        return delivered
    monkeypatch.setattr(app, "_fire_job", fire)
    if delivered:
        assert await gui_rpc.async_dispatch(app, cmd) == {"delivered": True, "job_id": job.id, "run_count": 41}
    else:
        with pytest.raises(ValueError, match="not delivered"):
            await gui_rpc.async_dispatch(app, cmd)
    assert calls == [(job.id, True)]


@pytest.mark.parametrize("fault", ["boolean-result", "boolean-delivered", "foreign-job"])
async def test_worker_result_must_be_an_exact_structured_receipt_for_selected_job(switch_app, monkeypatch, fault):
    app = switch_app
    job, cmd = prepare(app, monkeypatch)
    result = receipt(job)
    if fault == "boolean-delivered":
        result["delivered"] = 1
    elif fault == "foreign-job":
        result["job_id"] = "another-job"
    async def invoke():
        return True if fault == "boolean-result" else result
    monkeypatch.setattr(app, "_fire_job", lambda *args, **kwargs:
                        app.run_worker(invoke(), group="captured-job-test", exit_on_error=False))
    with pytest.raises(ValueError, match="structured receipt"):
        await gui_rpc.async_dispatch(app, cmd)
    assert job.run_count == 41


@pytest.mark.parametrize("fault", ["busy", "authority", "unknown-job"])
async def test_existing_manual_run_guards_refuse_before_creating_worker(switch_app, monkeypatch, fault):
    app = switch_app
    _job, cmd = prepare(app, monkeypatch)
    called = []
    if fault == "busy":
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    elif fault == "authority":
        monkeypatch.setattr(seat_authority, "withheld", lambda *args: "Requested job profile is withheld")
    else:
        cmd["job_id"] = "missing-job"
    monkeypatch.setattr(app, "_fire_job", lambda *args, **kwargs: called.append(True))
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, cmd)
    assert not called
