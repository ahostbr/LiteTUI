"""Owned scheduled transition negotiation uses actual response publication."""
# Tests intentionally use the canonical scheduler local wall clock.
# ruff: noqa: DTZ005
from __future__ import annotations

import asyncio
import copy

import pytest
from test_rust_background_input import app as _app
from test_rust_background_input import command
from test_rust_background_input import switch_app as _switch_app
from test_rust_conversation_transition import ENTER, LEAVE
from textual.worker import WorkerFailed

from litetui import background_input as work
from litetui import conversation_transition, rpc

app, switch_app = _app, _switch_app
pytestmark = pytest.mark.asyncio


def activation(app):
    app._launch_options.work_lifecycle_version = 1
    descriptor = work.capture(app, command(app))
    return command(app, type="gui.work.native_activate", instance=descriptor["instance"],
                   activation_token=descriptor["activation_token"], scheduled_transition_version=1)


async def test_scheduled_activation_remains_closed_until_exact_publication_ticket(app):
    cmd = activation(app)
    before = app.convo_path.read_bytes()
    result = work.activate(app, cmd)
    assert result["scheduled_transition_version"] == 1
    assert not work.enabled(app) and work.prompt_refusal(app)
    ticket = work.prepare_publication(app, cmd, result)
    assert not work.enabled(app) and app.convo_path.read_bytes() == before
    work.finish_publication(app, ticket)
    assert work.enabled(app) and work.prompt_refusal(app) is None
    with pytest.raises(ValueError, match="consumed|publication"):
        work.finish_publication(app, ticket)


@pytest.mark.parametrize("failure", [False, True], ids=["published", "failed-publication"])
async def test_actual_rpc_activation_emission_precedes_enable_and_failure_keeps_hold(app, monkeypatch, failure):
    cmd = activation(app)
    replies = []
    def emit(row):
        if row.get("id") == cmd["id"]:
            assert row["ok"] is True, row
            assert not work.enabled(app) and work.prompt_refusal(app)
            replies.append(copy.deepcopy(row))
            if failure:
                raise OSError("Controlled activation response failure")
    monkeypatch.setattr(rpc, "rpc_emit", emit)
    rpc._dispatch(app, cmd)
    results = await asyncio.wait_for(asyncio.gather(*(w.wait() for w in app.workers
        if w.group == "gui-management"), return_exceptions=True), 3)
    assert len(results) == len(replies) == 1
    if failure:
        assert isinstance(results[0], WorkerFailed)
        assert not work.enabled(app) and work.prompt_refusal(app)
    else:
        assert results == [None] and len(replies) == 1
        assert work.enabled(app) and work.prompt_refusal(app) is None
    assert not app._rewrite_test_requests


async def test_detached_review_keeps_human_cached_review_and_reserves_distinct_target(app):
    common = {"version": 1, **work.identity(app)}
    state = conversation_transition.snapshot(app, common)
    request = {**common, "snapshot": state["snapshot"], "action": "new"}
    human = conversation_transition.validate(app, request)
    cached = app._conversation_transition_review
    before = app.convo_path.read_bytes()
    review, scheduled = conversation_transition.build_review(app, request)
    assert app._conversation_transition_review is cached
    assert review is not cached and scheduled["target_session_id"] != human["target_session_id"]
    assert scheduled["snapshot"] == human["snapshot"] and app.convo_path.read_bytes() == before

@pytest.mark.parametrize("kind", ["cron", "loop"])
async def test_automatic_proposal_precedes_loop_advance_stamp_worker_and_identity(app, monkeypatch, kind):
    from dataclasses import asdict
    from datetime import datetime, timedelta

    from litetui import paths, scheduler
    from litetui.app import LiteTUI
    cmd = activation(app)
    descriptor = work.capture(app, command(app))
    wire = []
    monkeypatch.setattr(rpc, "rpc_emit", lambda row: wire.append(copy.deepcopy(row)))
    rpc._dispatch(app, cmd)
    await asyncio.wait_for(asyncio.gather(*(w.wait() for w in app.workers if w.group == "gui-management")), 3)
    active = next(row["result"] for row in wire if row.get("id") == cmd["id"])
    assert work.enabled(app)
    if kind == "loop":
        job = scheduler.Job.loop(prompt="Original scheduled payload", interval_minutes=1,
            owner_convo_id=app.convo_id, tool_profile="strict", now=datetime.now() - timedelta(minutes=2))
        job.new_conversation = True
    else:
        job = scheduler.Job(prompt="Original scheduled payload", schedule="* * * * *",
                            new_conversation=True, tool_profile="strict")
    app.jobs[:] = [job]
    scheduler.save(app.jobs, paths.data_root())
    common = {"version": 1, **work.identity(app)}
    snapshot = conversation_transition.snapshot(app, common)
    conversation_transition.validate(app, {**common, "snapshot": snapshot["snapshot"], "action": "new"})
    human = app._conversation_transition_review
    before = asdict(job), scheduler.jobs_path(paths.data_root()).read_bytes(), app.convo_id, app.convo_path.read_bytes()
    events = []
    def emit(event, *, origin=None):
        events.append((copy.deepcopy(event), origin))
        LiteTUI._rpc_emit(app, event, origin=origin)
    app._rpc = app._gui_rpc_enabled = True
    monkeypatch.setattr(app, "_rpc_emit", emit)
    def forbidden(*_args, **_kwargs):
        pytest.fail("Proposal reached canonical prepare_fire before native acceptance")
    monkeypatch.setattr(scheduler, "prepare_fire", forbidden)
    assert app._fire_job(job) is None
    proposals = [event for event, _origin in events if event["type"] == "work_transition_proposed"]
    assert len(proposals) == 1
    proposal = proposals[0]["proposal"]
    assert proposal["kind"] == kind and proposal["trigger"] == "automatic"
    assert proposal["job_snapshot"] == work.encoded(before[0]).decode("utf-8")
    assert proposal["job_sha256"] == work.digest(proposal["job_snapshot"].encode("utf-8"))
    assert app._conversation_transition_review is human
    assert before == (asdict(job), scheduler.jobs_path(paths.data_root()).read_bytes(), app.convo_id, app.convo_path.read_bytes())
    assert not work.bridge(app).reservations and not work.bridge(app).producers and not work.bridge(app).inputs
    assert not getattr(app, "_native_work_jobs", {})
    assert app._fire_job(job) is None and len(events) == 1

    import os
    from pathlib import Path
    if os.environ.get("LITETUI_SCHEDULE_FIXTURES"):
        root = Path(os.environ["LITETUI_SCHEDULE_FIXTURES"])
        root.mkdir(parents=True, exist_ok=True)
        (root / (kind + "-proposal.json")).write_bytes(work.encoded({"capture": descriptor, "active": active,
            "proposal": proposals[0], "wire": wire, "unchanged": True, "worker_started": False}))

async def published_rpc(app, monkeypatch, cmd, *, before_emit=None):
    replies = []
    def emit(row):
        if row.get("id") == cmd["id"]:
            if before_emit:
                before_emit(row)
            replies.append(copy.deepcopy(row))
    monkeypatch.setattr(rpc, "rpc_emit", emit)
    rpc._dispatch(app, cmd)
    workers = [w for w in app.workers if w.group == "gui-management" and not w.is_finished]
    await asyncio.wait_for(asyncio.gather(*(w.wait() for w in workers)), 5)
    assert len(replies) == 1 and replies[0]["ok"], replies
    return replies[0]["result"]


async def proposed_job(app, monkeypatch, kind="cron", *, extension=False):
    from datetime import datetime, timedelta

    from litetui import hook_host, paths, scheduler
    from litetui.app import LiteTUI
    monkeypatch.setattr(hook_host, "enter_conversation", ENTER)
    monkeypatch.setattr(hook_host, "leave_conversation", LEAVE)
    app._hook_conversation_id = app.convo_id
    from types import SimpleNamespace
    monkeypatch.setattr(app, "_user_bubble", lambda *_args, **_kwargs: SimpleNamespace(border_title="You"))
    await published_rpc(app, monkeypatch, activation(app))
    # Hold this unit-test firing slot across wall-clock minute boundaries.
    slot = scheduler.slot_of(datetime.now())
    monkeypatch.setattr(scheduler, "slot_of", lambda _now: slot)
    if kind == "loop":
        job = scheduler.Job.loop(prompt="Scheduled owned payload", interval_minutes=1,
            owner_convo_id=app.convo_id, tool_profile="strict", now=datetime.now() - timedelta(minutes=2))
        job.new_conversation = True
    else:
        job = scheduler.Job(prompt="Scheduled owned payload", schedule="* * * * *",
                            new_conversation=True, tool_profile="strict")
    app.jobs[:] = [job]
    scheduler.save(app.jobs, paths.data_root())
    if extension:
        path = scheduler.jobs_path(paths.data_root())
        rows = work.strict_json(path.read_bytes())
        rows[0]["future_extension"] = {"retained": "opaque"}
        path.write_bytes(work.encoded(rows))
    events = []
    def emit(event, *, origin=None):
        events.append(copy.deepcopy(event))
        LiteTUI._rpc_emit(app, event, origin=origin)
    app._rpc = app._gui_rpc_enabled = True
    monkeypatch.setattr(app, "_rpc_emit", emit)
    assert app._fire_job(job) is None
    proposal = next(row["proposal"] for row in events if row["type"] == "work_transition_proposed")
    cmd = command(app, type="gui.work.native_schedule_accept", id="accept-scheduled-1", schedule_version=1,
                  instance=work.bridge(app).instance, proposal_id=proposal["proposal_id"], proposal_token=proposal["proposal_token"])
    return job, proposal, cmd, events


@pytest.mark.parametrize("kind", ["cron", "loop"])
async def test_accepted_automatic_new_stamps_once_then_waits_for_readback_and_activation_publication(app, monkeypatch, kind):
    from litetui import native_schedule, paths, scheduler
    job, proposal, cmd, events = await proposed_job(app, monkeypatch, kind)
    original = app.convo_path
    history = original.read_bytes()
    result = await published_rpc(app, monkeypatch, cmd)
    assert result["status"] == "staged", repr(result)
    assert job.run_count == 1 and job.last_fired_slot == proposal["due_slot"]
    assert result["stamp"]["job_snapshot"] == work.encoded(__import__("dataclasses").asdict(job)).decode()
    assert app.convo_id == proposal["transition"]["target_session_id"]
    assert original.read_bytes() == history and not app._rewrite_test_requests
    assert app._native_work_deferred.handle is None and work.prompt_refusal(app)
    with pytest.raises(ValueError, match="readback"):
        work.capture(app, command(app))
    read = command(app, type="gui.work.native_schedule_readback", id="read-scheduled-1", schedule_version=1,
        old_instance=result["old_instance"], proposal_id=result["proposal_id"], proposal_token=result["proposal_token"],
        accepted_operation_id=result["operation_id"], result_sha256=work.digest(work.encoded(result)))
    def before_read(row):
        assert row["ok"], row
        with pytest.raises(ValueError, match="readback"):
            work.capture(app, command(app))
        assert app._native_work_deferred.handle is None
    readback = await published_rpc(app, monkeypatch, read, before_emit=before_read)
    assert readback["deferred_readback"]["record"] == result["deferred"]
    capture = work.capture(app, command(app))
    assert capture["deferred_jobs"] == [result["deferred"]]
    activate = command(app, type="gui.work.native_activate", id="activate-scheduled-1", instance=capture["instance"],
                       activation_token=capture["activation_token"], scheduled_transition_version=1)
    def before_active(row):
        assert row["ok"], row
        assert app._native_work_deferred.handle is None and not work.enabled(app)
    await published_rpc(app, monkeypatch, activate, before_emit=before_active)
    for _ in range(100):
        if app._native_work_deferred is None:
            break
        await asyncio.sleep(.01)
    if app._native_work_deferred is not None and app._native_work_deferred.worker is not None:
        await app._native_work_deferred.worker.wait()
    assert app._native_work_deferred is None
    assert len(work.bridge(app).inputs) == 1
    await asyncio.wait_for(asyncio.gather(*(entry.observer for entry in work.bridge(app).inputs.values() if entry.observer)), 5)
    assert job.run_count == 1 and native_schedule.enabled(app)
    assert scheduler.jobs_path(paths.data_root()).exists()
    import os
    from pathlib import Path
    if directory := os.environ.get("LITETUI_SCHEDULE_FIXTURES"):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        (root / (kind + "-full-handoff.json")).write_bytes(work.encoded({
            "source_identity": {key: cmd[key] for key in ("session_id", "agent_id", "backend", "model")},
            "proposal": next(event for event in events if event["type"] == "work_transition_proposed"),
            "accept_command": cmd, "result": result, "readback_command": read, "readback": readback,
            "capture": capture, "activation_command": activate, "events": events,
            "job_snapshot": work.encoded(__import__("dataclasses").asdict(job)).decode(),
            "old_history": history.decode(), "target_history": app.convo_path.read_text(encoding="utf-8"),
            "final_work_document": work.bridge(app).document(), "provider_requests": len(app._rewrite_test_requests),
        }))


def readback_command(app, result):
    return command(app, type="gui.work.native_schedule_readback", id="read-scheduled-failure", schedule_version=1,
        old_instance=result["old_instance"], proposal_id=result["proposal_id"], proposal_token=result["proposal_token"],
        accepted_operation_id=result["operation_id"], result_sha256=work.digest(work.encoded(result)))


async def failed_publication(app, monkeypatch, cmd):
    rows = []
    def emit(row):
        if row.get("id") == cmd["id"]:
            assert row["ok"], row
            rows.append(copy.deepcopy(row))
            raise OSError("Controlled exact response publication failure")
    monkeypatch.setattr(rpc, "rpc_emit", emit)
    rpc._dispatch(app, cmd)
    outcomes = await asyncio.wait_for(asyncio.gather(*(worker.wait() for worker in app.workers
        if worker.group == "gui-management" and not worker.is_finished), return_exceptions=True), 5)
    assert len(rows) == 1 and len(outcomes) == 1 and isinstance(outcomes[0], WorkerFailed)
    return rows[0]["result"]


@pytest.mark.parametrize("phase", ["result", "readback", "activation"])
async def test_exact_response_failure_never_schedules_deferred_continuation(app, monkeypatch, phase):
    from litetui import native_schedule
    job, _proposal, accept, _events = await proposed_job(app, monkeypatch)
    result = await (failed_publication(app, monkeypatch, accept) if phase == "result"
                    else published_rpc(app, monkeypatch, accept))
    assert result["status"] == "staged"
    read = readback_command(app, result)
    if phase == "result":
        with pytest.raises(ValueError, match="published decision"):
            native_schedule.readback(app, read)
    elif phase == "readback":
        await failed_publication(app, monkeypatch, read)
    else:
        await published_rpc(app, monkeypatch, read)
        capture = work.capture(app, command(app))
        activate = command(app, type="gui.work.native_activate", id="activate-failure",
            instance=capture["instance"], activation_token=capture["activation_token"], scheduled_transition_version=1)
        await failed_publication(app, monkeypatch, activate)
        assert not work.enabled(app)
    if phase != "activation":
        with pytest.raises(ValueError, match="readback"):
            work.capture(app, command(app))
    deferred = app._native_work_deferred
    assert deferred.handle is None and deferred.worker is None and not deferred.claimed
    assert job.run_count == 1 and not app._rewrite_test_requests and work.prompt_refusal(app)


@pytest.mark.parametrize("change", ["disk", "slot", "input", "policy", "mutation"])
async def test_accept_revalidates_canonical_pre_and_quiet_context_before_stamp(app, monkeypatch, change):
    from dataclasses import asdict
    from datetime import datetime, timedelta

    from litetui import native_schedule, paths, scheduler
    job, _proposal, cmd, _events = await proposed_job(app, monkeypatch)
    old_identity, old_history = app.convo_id, app.convo_path.read_bytes()
    if change == "disk":
        edited = scheduler.Job(**asdict(job))
        edited.prompt = "Externally edited payload"
        scheduler.save([edited], paths.data_root())
    elif change == "slot":
        expired = (datetime.fromisoformat(_proposal["due_slot"]) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
        monkeypatch.setattr(scheduler, "slot_of", lambda _now: expired)
    elif change == "input":
        app._pending_input.append({"content": "Local pending command", "source": "rpc", "tool_profile": "strict"})
    elif change == "policy":
        app._hold_restored_input = False
    else:
        app._gui_management_busy = True
        app._gui_management_owner = object()
    before_jobs = scheduler.jobs_path(paths.data_root()).read_bytes()
    result = await published_rpc(app, monkeypatch, cmd)
    assert result["status"] == "refused", result
    assert result["stamp"] is result["transition"] is result["deferred"] is None
    assert result["close_required"] is False and job.run_count == 0
    assert scheduler.jobs_path(paths.data_root()).read_bytes() == before_jobs
    assert app.convo_id == old_identity and app.convo_path.read_bytes() == old_history
    assert not app._rewrite_test_requests and getattr(app, "_native_work_deferred", None) is None
    with pytest.raises(ValueError, match="consumed"):
        await native_schedule.accept(app, cmd)


@pytest.mark.parametrize("failure", ["save", "readback"])
async def test_attempted_stamp_failure_retains_generation_and_never_invents_verified_stamp(app, monkeypatch, failure):
    from litetui import paths, scheduler
    job, _proposal, cmd, _events = await proposed_job(app, monkeypatch, "loop")
    old_next, old_identity, history = job.next_run_at, app.convo_id, app.convo_path.read_bytes()
    if failure == "save":
        save = scheduler.save
        def fail_after_save(*args, **kwargs):
            save(*args, **kwargs)
            raise OSError("Controlled ambiguous stamp save")
        monkeypatch.setattr(scheduler, "save", fail_after_save)
    else:
        def fail_readback(*_args):
            raise OSError("Controlled missing POST stamp readback")
        monkeypatch.setattr(work, "verify_job_stamp", fail_readback)
    result = await published_rpc(app, monkeypatch, cmd)
    assert result["status"] == "uncertain" and result["close_required"] is True, result
    assert result["stamp"] is result["transition"] is result["deferred"] is None
    assert job.run_count == 1 and job.next_run_at != old_next
    assert work.strict_json(scheduler.jobs_path(paths.data_root()).read_bytes())[0]["run_count"] == 1
    assert app.convo_id == old_identity and app.convo_path.read_bytes() == history
    assert work.bridge(app).closing and work.prompt_refusal(app) and not app._rewrite_test_requests


async def test_scheduled_recapture_requires_negotiated_activation_and_exact_readback(app, monkeypatch):
    from litetui import native_schedule
    _job, _proposal, cmd, _events = await proposed_job(app, monkeypatch)
    result = await published_rpc(app, monkeypatch, cmd)
    await published_rpc(app, monkeypatch, readback_command(app, result))
    capture = work.capture(app, command(app))
    legacy = command(app, type="gui.work.native_activate", id="legacy-scheduled-activate",
        instance=capture["instance"], activation_token=capture["activation_token"])
    with pytest.raises(ValueError, match="negotiated response publication"):
        work.activate(app, legacy)
    assert not work.enabled(app) and app._native_work_deferred.handle is None
    request = {**legacy, "scheduled_transition_version": 1}
    active = work.activate(app, request)
    with pytest.raises(ValueError, match="exact unconsumed owner"):
        work.prepare_publication(app, {**request, "id": "foreign-id"}, active)
    ticket = work.prepare_publication(app, request, active)
    app._gui_quitting = True
    with pytest.raises(ValueError, match="Quit"):
        native_schedule.finish_publication(app, ticket)
    assert not work.enabled(app) and work.bridge(app).closing and app._native_work_deferred.handle is None


async def test_decline_publication_keeps_single_decision_and_allows_next_canonical_candidate(app, monkeypatch):
    from dataclasses import asdict

    from litetui import native_schedule, paths, scheduler
    job, _proposal, cmd, events = await proposed_job(app, monkeypatch)
    other = scheduler.Job(prompt="Second canonical candidate", schedule="* * * * *", new_conversation=True, tool_profile="strict")
    app.jobs.append(other)
    scheduler.save(app.jobs, paths.data_root())
    before = scheduler.jobs_path(paths.data_root()).read_bytes(), asdict(job), asdict(other)
    assert app._fire_job(other) is None and len(events) == 1
    decline = {**cmd, "type": "gui.work.native_schedule_decline", "reason": "Native editor owns the decision"}
    result = await published_rpc(app, monkeypatch, decline)
    assert result["status"] == "declined" and not result["close_required"]
    await published_rpc(app, monkeypatch, readback_command(app, result))
    assert app._native_schedule_pending is None
    assert app._fire_job(job) is None and len(events) == 1
    assert app._fire_job(other) is None and len(events) == 2
    assert events[-1]["proposal"]["job_id"] == other.id
    assert before == (scheduler.jobs_path(paths.data_root()).read_bytes(), asdict(job), asdict(other))
    with pytest.raises(ValueError, match="Unknown"):
        native_schedule.decline(app, decline)


async def test_readback_ticket_rejects_mutated_bytes_and_foreign_current_identity(app, monkeypatch):
    from litetui import native_schedule
    _job, _proposal, cmd, _events = await proposed_job(app, monkeypatch)
    result = await published_rpc(app, monkeypatch, cmd)
    request = readback_command(app, result)
    original = app.model_id
    app.model_id = "foreign-current-model"
    with pytest.raises(ValueError, match="exact expected runtime"):
        native_schedule.readback(app, {**request, "model": app.model_id})
    app.model_id = original
    readback = native_schedule.readback(app, request)
    readback["verified"] = False
    with pytest.raises(ValueError, match="exact result"):
        work.prepare_publication(app, request, readback)
    assert app._native_work_deferred.handle is None


@pytest.mark.parametrize("flag", ["_gui_quitting", "_conversation_transition_shutdown_requested", "_conversation_rewrite_closing"])
async def test_quit_before_accept_returns_correlated_safe_refusal_without_starting_worker(app, monkeypatch, flag):
    from litetui import paths, scheduler
    job, _proposal, cmd, _events = await proposed_job(app, monkeypatch)
    before = scheduler.jobs_path(paths.data_root()).read_bytes(), app.convo_id, app.convo_path.read_bytes()
    setattr(app, flag, True)
    result = await published_rpc(app, monkeypatch, cmd)
    assert result["status"] == "refused" and result["operation_id"] == cmd["id"]
    assert result["stamp"] is result["transition"] is result["deferred"] is None
    assert not result["close_required"] and not getattr(app, "_native_work_jobs", {})
    readback = await published_rpc(app, monkeypatch, readback_command(app, result))
    assert readback["verified"] and readback["result"] == result
    assert before == (scheduler.jobs_path(paths.data_root()).read_bytes(), app.convo_id, app.convo_path.read_bytes())
    assert job.run_count == 0 and not app._rewrite_test_requests


async def test_proposal_and_staged_handoff_allow_inspection_but_staged_rejects_other_mutations(app, monkeypatch):
    from litetui import gui_rpc, native_schedule
    job, _proposal, cmd, _events = await proposed_job(app, monkeypatch)
    for operation in ("gui.state", "gui.conversations.native_snapshot"):
        result = await published_rpc(app, monkeypatch, command(app, type=operation))
        assert result and job.run_count == 0
    hello = gui_rpc.dispatch(app, {"type": "gui.hello", "id": "hello-six", "protocol_version": 1})
    assert all(operation in hello["operations"] for operation in native_schedule.OPERATIONS)
    await published_rpc(app, monkeypatch, cmd)
    for operation in ("gui.state", "gui.conversations.native_snapshot"):
        assert await published_rpc(app, monkeypatch, command(app, type=operation))
    with pytest.raises(ValueError, match="scheduled conversation transition"):
        await gui_rpc.async_dispatch(app, {"type": "gui.jobs.pause", "id": "foreign-mutation", "job_id": job.id})
    replies = []
    monkeypatch.setattr(rpc, "rpc_emit", replies.append)
    rpc._dispatch(app, {"type": "set", "id": "foreign-raw-mutation", "profile": "autonomous"})
    assert len(replies) == 1 and replies[0]["ok"] is False
    assert app._native_work_deferred.handle is None


@pytest.mark.parametrize("version", [True, False, 0, 2, "1", None])
async def test_scheduled_activation_version_requires_exact_integer(app, version):
    cmd = activation(app)
    with pytest.raises(ValueError, match="exact version1"):
        work.activate(app, {**cmd, "scheduled_transition_version": version})
    assert not work.enabled(app)


@pytest.mark.parametrize("kind", ["cron", "loop"])
async def test_accepted_canonical_job_preserves_unknown_store_fields(app, monkeypatch, kind):
    from litetui import paths, scheduler
    job, _proposal, cmd, _events = await proposed_job(app, monkeypatch, kind, extension=True)
    result = await published_rpc(app, monkeypatch, cmd)
    assert result["status"] == "staged", result
    rows = work.strict_json(scheduler.jobs_path(paths.data_root()).read_bytes())
    assert rows[0]["future_extension"] == {"retained": "opaque"} and job.run_count == 1
    readback = await published_rpc(app, monkeypatch, readback_command(app, result))
    assert readback["job_readback"]["job_snapshot"] == result["stamp"]["job_snapshot"]
    assert "future_extension" not in work.strict_json(result["stamp"]["job_snapshot"])
    assert not app._rewrite_test_requests


@pytest.mark.parametrize("action", ["new", "model"])
@pytest.mark.parametrize("outcome", ["success", "post-close-failure"])
@pytest.mark.parametrize("decision", ["decline", "accept"])
async def test_human_identity_transition_drains_old_scheduled_proposal(app, monkeypatch, action, outcome, decision):
    from test_rust_conversation_transition import reviewed

    from litetui import gui_rpc, native_schedule, paths, scheduler

    job, proposal, old_accept, events = await proposed_job(app, monkeypatch)
    old_identity = work.identity(app)
    old_bridge = work.bridge(app)
    pending = app._native_schedule_pending
    original_jobs = scheduler.jobs_path(paths.data_root()).read_bytes()
    if action == "new":
        human = {**reviewed(app, "new"), "type": "gui.conversations.native_apply", "id": "human-new"}
    else:
        app.available_models.append("new-model")
        monkeypatch.setattr(app, "system_message", lambda *_: None)
        monkeypatch.setattr(app, "apply_context_length", lambda: None)
        human = {"type": "gui.models.select", "id": "human-model", "slug": "new-model"}
    entered, release = asyncio.Event(), asyncio.Event()
    before = work.before_identity_transition

    async def pause_after_retirement(instance):
        proof = await before(instance)
        entered.set()
        await release.wait()
        return proof

    def fail_after_close(*_args):
        raise ValueError("Controlled human identity effect failure")

    monkeypatch.setattr(work, "before_identity_transition", pause_after_retirement)
    if outcome == "post-close-failure":
        monkeypatch.setattr(app, "_load_system_prompt" if action == "new" else "apply_context_length", fail_after_close)
    task = asyncio.create_task(gui_rpc.async_dispatch(app, human))
    await asyncio.wait_for(entered.wait(), 5)
    assert not task.done() and old_bridge.closing and pending.retired
    # The actual human control owns the quiet boundary; no new scheduled effect.
    assert app._native_schedule_pending is pending
    release.set()
    if outcome == "post-close-failure" and action == "model":
        with pytest.raises(ValueError, match="Controlled human identity effect failure"):
            await task
    else:
        result = await task
        if action == "new":
            assert result["status"] == ("created" if outcome == "success" else "failed"), result
    assert work.identity(app) != old_identity
    assert scheduler.jobs_path(paths.data_root()).read_bytes() == original_jobs
    assert pending.retired and pending.worker is None and pending.task is None
    if outcome == "success":
        await published_rpc(app, monkeypatch, activation(app))
        job.prompt += " changed after human control"
        scheduler.save(app.jobs, paths.data_root())
        assert native_schedule.offer(app, job)
        assert app._native_schedule_pending is pending
    decision_cmd = {**old_accept, "type": "gui.work.native_schedule_" + decision, "id": "late-" + decision,
                    **({"reason": "Human identity control already owns the lane"} if decision == "decline" else {})}
    receipt = await published_rpc(app, monkeypatch, decision_cmd)
    assert receipt["status"] == ("declined" if decision == "decline" else "refused")
    assert pending.worker is None and pending.task is None
    assert all(receipt[field] is None for field in ("stamp", "transition", "deferred", "deferred_journal_sha256"))
    read_cmd = {**readback_command(app, receipt), **old_identity}
    read = await published_rpc(app, monkeypatch, read_cmd)
    assert read["identity"] == old_identity and read["verified"]
    assert read["job_readback"] is None and read["deferred_readback"] is None
    assert app._native_schedule_pending is None
    assert not app._rewrite_test_requests
    assert not any(row["type"] == "work_input" for row in events)
    if outcome == "success":
        # A fresh canonical candidate may now be offered; the retired token cannot suppress it.
        assert native_schedule.offer(app, job)
        assert app._native_schedule_pending.proposal["proposal_id"] != proposal["proposal_id"]


async def retired_human_new(app, monkeypatch):
    from test_rust_conversation_transition import reviewed

    from litetui import gui_rpc
    job, proposal, old_accept, events = await proposed_job(app, monkeypatch)
    identity = work.identity(app)
    result = await gui_rpc.async_dispatch(app, {**reviewed(app, "new"), "type": "gui.conversations.native_apply", "id": "human-new"})
    assert result["status"] == "created"
    await published_rpc(app, monkeypatch, activation(app))
    return job, proposal, old_accept, events, identity


async def test_retired_proposal_still_requires_exact_old_token_agent_and_no_effect_state(app, monkeypatch):
    from litetui import native_schedule
    _job, _proposal, old_accept, _events, identity = await retired_human_new(app, monkeypatch)
    pending = app._native_schedule_pending
    decline = {**old_accept, "type": "gui.work.native_schedule_decline", "reason": "Human New won"}
    for changes in ({"proposal_token": "0" * 64}, {"proposal_id": "changed"},
                    {"instance": "0" * 64}, work.identity(app), {"schedule_version": True}, {"extra": None}):
        with pytest.raises(ValueError):
            native_schedule.decline(app, {**decline, **changes})
        assert pending.state == "proposed" and pending.result is None
    with monkeypatch.context() as changed:
        changed.setattr(app, "_agent_session", object())
        with pytest.raises(ValueError, match="exact no-effect owner"):
            native_schedule.decline(app, decline)
    for field, value in (("stamp_attempted", True), ("stamp", {}), ("transition", {})):
        with monkeypatch.context() as changed:
            changed.setattr(pending, field, value)
            with pytest.raises(ValueError, match="exact no-effect owner"):
                native_schedule.decline(app, decline)
    result = await published_rpc(app, monkeypatch, decline)
    read_cmd = {**readback_command(app, result), **identity}
    for changes in ({"result_sha256": "0" * 64}, {"accepted_operation_id": "foreign"}, work.identity(app)):
        with pytest.raises(ValueError):
            native_schedule.readback(app, {**read_cmd, **changes})
        assert pending.readback_result is None
    await published_rpc(app, monkeypatch, read_cmd)
    with pytest.raises(ValueError):
        await native_schedule.accept(app, old_accept)
    assert not app._rewrite_test_requests


@pytest.mark.parametrize("phase", ["result", "readback"])
async def test_retired_proposal_publication_failure_preserves_only_bounded_tombstone(app, monkeypatch, phase):
    from litetui import native_schedule
    job, _proposal, old_accept, _events, identity = await retired_human_new(app, monkeypatch)
    pending = app._native_schedule_pending
    decline = {**old_accept, "type": "gui.work.native_schedule_decline", "reason": "Human New won"}
    if phase == "result":
        await failed_publication(app, monkeypatch, decline)
        assert not pending.result_published
    else:
        result = await published_rpc(app, monkeypatch, decline)
        await failed_publication(app, monkeypatch, {**readback_command(app, result), **identity})
        assert pending.result_published and not pending.readback_published
    assert app._native_schedule_pending is pending
    assert native_schedule.offer(app, job)
    assert app._native_schedule_pending is pending and pending.retired
    with pytest.raises(ValueError):
        await native_schedule.accept(app, old_accept)
    assert pending.worker is None and not app._rewrite_test_requests


@pytest.mark.parametrize("action", ["new", "model"])
async def test_human_preflight_refusal_leaves_proposal_live_and_declineable(app, monkeypatch, action):
    from test_rust_conversation_transition import reviewed

    from litetui import gui_rpc
    _job, _proposal, old_accept, _events = await proposed_job(app, monkeypatch)
    pending = app._native_schedule_pending
    if action == "new":
        human = {**reviewed(app, "new"), "snapshot": "stale", "type": "gui.conversations.native_apply", "id": "stale-new"}
        result = await gui_rpc.async_dispatch(app, human)
        assert result["status"] == "refused" and not result["mutated"]
    else:
        with pytest.raises(ValueError, match="Model is not available"):
            await gui_rpc.async_dispatch(app, {"type": "gui.models.select", "id": "stale-model", "slug": "no-longer-available"})
    assert not pending.retired and not work.bridge(app).closing
    decline = {**old_accept, "type": "gui.work.native_schedule_decline", "reason": "Human control refused"}
    result = await published_rpc(app, monkeypatch, decline)
    await published_rpc(app, monkeypatch, readback_command(app, result))
    assert app._native_schedule_pending is None and not app._rewrite_test_requests


@pytest.mark.parametrize("decision", ["accept", "decline"])
async def test_historical_decision_can_finish_while_actual_human_new_still_owns_transition(app, monkeypatch, decision):
    from test_rust_conversation_transition import reviewed

    from litetui import gui_rpc, native_schedule, paths, scheduler
    job, _proposal, old_accept, _events = await proposed_job(app, monkeypatch)
    pending, old_bridge = app._native_schedule_pending, work.bridge(app)
    original_jobs = scheduler.jobs_path(paths.data_root()).read_bytes()
    human = {**reviewed(app, "new"), "type": "gui.conversations.native_apply", "id": "human-new"}
    before = work.before_identity_transition
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(instance):
        proof = await before(instance)
        entered.set()
        await release.wait()
        return proof

    monkeypatch.setattr(work, "before_identity_transition", pause)
    task = asyncio.create_task(gui_rpc.async_dispatch(app, human))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert pending.retired and app._conversation_transition_pending
        cmd = {**old_accept, "type": "gui.work.native_schedule_" + decision,
               **({"reason": "Human New already owns the lane"} if decision == "decline" else {})}
        result = await published_rpc(app, monkeypatch, cmd)
        assert result["status"] == ("refused" if decision == "accept" else "declined")
        await published_rpc(app, monkeypatch, readback_command(app, result))
        assert app._native_schedule_pending is None
        assert app._conversation_transition_pending and not task.done()
        assert work.bridge(app) is old_bridge and old_bridge.closing
        assert pending.worker is None and pending.task is None
        assert scheduler.jobs_path(paths.data_root()).read_bytes() == original_jobs
        assert native_schedule.offer(app, job)
        assert app._native_schedule_pending is None
    finally:
        release.set()
        result = await task
    assert result["status"] == "created"
    assert result["work_shutdown"] == old_bridge.transition_proof
    assert not app._rewrite_test_requests


async def test_retired_readback_publication_keeps_hold_if_original_agent_changes_after_emit(app, monkeypatch):
    from litetui import native_schedule
    _job, _proposal, old_accept, _events, identity = await retired_human_new(app, monkeypatch)
    pending = app._native_schedule_pending
    cmd = {**old_accept, "type": "gui.work.native_schedule_decline", "reason": "Human New won"}
    result = await published_rpc(app, monkeypatch, cmd)
    read_cmd = {**readback_command(app, result), **identity}
    read = native_schedule.readback(app, read_cmd)
    ticket = native_schedule.prepare_publication(app, read_cmd, read)
    with monkeypatch.context() as changed:
        changed.setattr(app, "_agent_session", object())
        with pytest.raises(ValueError, match="exact no-effect owner"):
            native_schedule.finish_publication(app, ticket)
    assert app._native_schedule_pending is pending and not pending.readback_published
