"""Native Work holds unsupported scheduled identity changes before any effect."""
from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from test_rust_background_input import app as _owned_app
from test_rust_background_input import armed as _armed
from test_rust_background_input import command, settle_inputs
from test_rust_background_input import switch_app as _switch_app
from textual.worker import WorkerCancelled, WorkerFailed

from litetui import background_input as work
from litetui import cron, gui_rpc, hook_host, native_work, paths, scheduler

app = _owned_app
armed = _armed
switch_app = _switch_app


def seed(armed, *, new_conversation=True):
    armed._launch_options.work_lifecycle_version = 1
    job = scheduler.Job(prompt="Owned scheduled payload", schedule="* * * * *", tool_profile="strict", new_conversation=new_conversation)
    armed.jobs[:] = [job]
    scheduler.save(armed.jobs, paths.data_root())
    return job


def protected(armed, job):
    value = work.bridge(armed)
    return (asdict(job), scheduler.jobs_path(paths.data_root()).read_bytes(), armed.convo_id,
            armed.convo_path.read_bytes(), value.instance, value.raw,
            tuple(value.reservations), tuple(value.producers), tuple(value.inputs))


@pytest.mark.asyncio
async def test_automatic_new_conversation_job_is_held_before_stamp_reservation_or_identity_change(armed, monkeypatch):
    job = seed(armed)
    notices: list[str] = []
    monkeypatch.setattr(armed, "_system", notices.append)
    before = protected(armed, job)
    result = armed._fire_job(job)
    if result is not None:
        result.cancel()
        with suppress(WorkerCancelled):
            await result.wait()
    assert result is None, "native scheduler accepted an identity change without a host transition handshake"
    for _ in range(5):
        assert armed._fire_job(job) is None
    assert protected(armed, job) == before
    assert len(notices) == 1 and "new conversation" in notices[0] and len(notices[0].encode("utf-8")) <= 512
    assert not armed._rewrite_test_requests and not getattr(armed, "_native_work_deferred", None)
    assert not getattr(armed, "_native_work_recapture_required", None)
    assert not getattr(armed, "_native_work_jobs", {})
    assert native_work.snapshot(armed, command(armed, type="gui.work.native_snapshot"))["scheduler_notice"] == notices[0]


@pytest.mark.asyncio
async def test_manual_run_refuses_with_actionable_reason_and_keeps_saved_job(armed):
    job = seed(armed)
    before = protected(armed, job)
    with pytest.raises(ValueError, match="new conversation"):
        armed._fire_job(job, manual=True)
    with pytest.raises(ValueError, match="new conversation"):
        await gui_rpc.async_dispatch(armed, {"type":"gui.jobs.run", "id":"native-job-run", "job_id":job.id})
    assert protected(armed, job) == before
    assert not armed._rewrite_test_requests and not getattr(armed, "_native_work_jobs", {})


@pytest.mark.asyncio
async def test_same_conversation_job_still_admits_once_under_real_launch_contract(armed):
    job = seed(armed, new_conversation=False)
    session = armed.convo_id
    result = await asyncio.wait_for(armed._fire_job(job, manual=True).wait(), 5)
    assert result["delivered"] and result["run_count"] == 1
    await settle_inputs(armed, 1)
    assert armed.convo_id == session and job.run_count == 1
    assert len(armed._rewrite_test_requests) == 1
    assert not getattr(armed, "_native_work_scheduled_transition_hold", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["uncertain", "readback", "closing"])
async def test_actual_monitor_survives_refused_ticks_and_resumes_after_gate_resolution(armed, monkeypatch, gate):
    job = seed(armed, new_conversation=False)
    value = work.bridge(armed)
    notices: list[str] = []
    monkeypatch.setattr(armed, "_system", notices.append)
    if gate == "uncertain":
        value.error = "Canonical Work admission is uncertain; never replay automatically"
    elif gate == "readback":
        value.pending_readbacks["controlled-pending-proof"] = {}
    else:
        value.closing = True
    before = protected(armed, job)
    sleeps: asyncio.Queue[bool] = asyncio.Queue()
    ticks: asyncio.Queue[bool] = asyncio.Queue()
    async def tick_sleep(_seconds):
        sleeps.put_nowait(True)
        await ticks.get()
    monkeypatch.setattr(cron, "asyncio", SimpleNamespace(sleep=tick_sleep))
    # The real monitor body runs in its real Textual worker group. Keep a failed
    # regression local instead of allowing Textual's default fatal-error policy
    # to terminate the whole owned fixture and mask the monitor's exception.
    worker = armed.run_worker(inspect.unwrap(cron.monitor)(armed), group="cron", exclusive=True, exit_on_error=False)
    retired = asyncio.create_task(worker.wait())
    async def next_sleep():
        waiting = asyncio.create_task(sleeps.get())
        try:
            done, _ = await asyncio.wait((waiting, retired), timeout=5, return_when=asyncio.FIRST_COMPLETED)
            assert done, "canonical cron tick did not return to its wait"
            if retired in done:
                await retired
                pytest.fail("cron monitor stopped while jobs were merely held")
            await waiting
        finally:
            if not waiting.done():
                waiting.cancel()
                with suppress(asyncio.CancelledError):
                    await waiting
    try:
        await next_sleep()
        for _ in range(2):
            ticks.put_nowait(True)
            await next_sleep()
        assert not worker.is_finished and protected(armed, job) == before
        assert len(notices) == 1
        assert native_work.snapshot(armed, command(armed, type="gui.work.native_snapshot"))["scheduler_notice"] == notices[0]
        with pytest.raises(ValueError):
            armed._fire_job(job, manual=True)
        if gate != "closing":
            value.error = None
            value.pending_readbacks.clear()
            ticks.put_nowait(True)
            await next_sleep()
            await settle_inputs(armed, 1)
            assert job.run_count == 1 and len(armed._rewrite_test_requests) == 1
            assert native_work.snapshot(armed, command(armed, type="gui.work.native_snapshot"))["scheduler_notice"] is None
    finally:
        worker.cancel()
        with suppress(WorkerCancelled, WorkerFailed):
            await retired
        value.error = None
        value.pending_readbacks.clear()
        value.closing = False


@pytest.mark.asyncio
@pytest.mark.parametrize("notice", [17, "x" * 2049], ids=["wrong-type", "oversized"])
async def test_snapshot_rejects_malformed_scheduler_notice(armed, notice):
    armed._native_work_scheduled_transition_hold = notice
    with pytest.raises((ValueError, TypeError)):
        native_work.snapshot(armed, command(armed, type="gui.work.native_snapshot"))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disk", "mutable-job"])
async def test_canonical_job_change_after_worker_scheduling_is_rechecked_before_stamp(armed, monkeypatch, change):
    job = seed(armed, new_conversation=False)
    monkeypatch.setattr(hook_host, "enter_conversation", lambda app, _event: setattr(app, "_hook_conversation_id", app.convo_id))
    notices: list[str] = []
    monkeypatch.setattr(armed, "_system", notices.append)
    value = work.bridge(armed)
    before = armed.convo_id, armed.convo_path.read_bytes(), value.instance, value.raw
    worker = armed._fire_job(job, manual=change == "mutable-job")
    assert worker is not None and not job.new_conversation
    # A separate canonical saved row wins before the invocation acquires its
    # scheduler lease. The real _fire_job refresh must remain authoritative.
    if change == "disk":
        scheduler.save([replace(job, new_conversation=True)], paths.data_root())
    else:
        job.new_conversation = True
        scheduler.save(armed.jobs, paths.data_root())
    saved = scheduler.jobs_path(paths.data_root()).read_bytes()
    try:
        if change == "disk":
            result = await asyncio.wait_for(worker.wait(), 5)
            assert result is None, "canonical refresh bypassed the native transition hold"
        else:
            with pytest.raises(WorkerFailed, match="new conversation"):
                await asyncio.wait_for(worker.wait(), 5)
        assert job.new_conversation and job.run_count == 0
        assert saved == scheduler.jobs_path(paths.data_root()).read_bytes()
        assert (armed.convo_id, armed.convo_path.read_bytes(), value.instance, value.raw) == before
        assert work.bridge(armed) is value and not value.closing and not value.reservations
        assert not value.producers and not value.inputs and not armed._rewrite_test_requests
        if change == "disk":
            assert len(notices) == 1 and "new conversation" in notices[0]
            assert native_work.snapshot(armed, command(armed, type="gui.work.native_snapshot"))["scheduler_notice"] == notices[0]
        else:
            assert not notices
    finally:
        if work.bridge(armed) is None:
            # Red-only cleanup: restore a captured, inactive object for the
            # owned fixture after exposing the unintended transition. Do not
            # activate the staged invocation or perform its model request.
            work.capture(armed, command(armed))


@pytest.mark.asyncio
async def test_other_job_success_does_not_clear_the_exact_held_job_notice(armed, monkeypatch):
    held = seed(armed)
    other = scheduler.Job(prompt="Other current-session job", schedule="* * * * *", tool_profile="strict")
    armed.jobs.append(other)
    scheduler.save(armed.jobs, paths.data_root())
    notices: list[str] = []
    monkeypatch.setattr(armed, "_system", notices.append)
    assert armed._fire_job(held) is None
    reason = armed._native_work_scheduled_transition_hold
    result = await asyncio.wait_for(armed._fire_job(other, manual=True).wait(), 5)
    assert result["delivered"]
    await settle_inputs(armed, 1)
    assert armed._native_work_scheduled_transition_hold == reason
    assert armed._native_work_scheduled_transition_hold_job == held.id
    assert armed._fire_job(held) is None and notices == [reason]
    held.new_conversation = False
    scheduler.save(armed.jobs, paths.data_root())
    result = await asyncio.wait_for(armed._fire_job(held, manual=True).wait(), 5)
    assert result["delivered"]
    await settle_inputs(armed, 2)
    assert armed._native_work_scheduled_transition_hold is None
    assert armed._native_work_scheduled_transition_hold_job is None
    assert len(armed._rewrite_test_requests) == 2
