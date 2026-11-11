"""Historical no-effect replies retain authority without reopening failed execution."""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_rust_conversation_transition import reviewed
from test_rust_scheduled_publication import app as _app
from test_rust_scheduled_publication import (
    proposed_job,
    published_rpc,
    readback_command,
)
from test_rust_scheduled_publication import switch_app as _switch_app

from litetui import background_input as work
from litetui import effort_projection, gui_rpc, native_schedule, paths, scheduler
from litetui.agent_ownership import OwnershipError
from litetui.llm_backend import BackendError

app, switch_app = _app, _switch_app
pytestmark = pytest.mark.asyncio


async def failed_new(app, monkeypatch):
    _job, _proposal, old_accept, events = await proposed_job(app, monkeypatch)
    identity = work.identity(app)
    projection = app._owned_effort_projection
    jobs = scheduler.jobs_path(paths.data_root()).read_bytes()
    human = {**reviewed(app, "new"), "type": "gui.conversations.native_apply", "id": "failed-new"}

    def fail_after_stage():
        raise ValueError("Controlled target prompt failure")

    with monkeypatch.context() as changed:
        changed.setattr(app, "_load_system_prompt", fail_after_stage)
        receipt = await gui_rpc.async_dispatch(app, human)
    assert receipt["status"] == "failed" and receipt["close_required"]
    assert work.identity(app) != identity and app.store.pending
    assert app._owned_effort_projection is projection
    pending = app._native_schedule_pending
    assert pending.retired and pending.worker is None and pending.task is None
    assert scheduler.jobs_path(paths.data_root()).read_bytes() == jobs
    return old_accept, identity, pending, projection, events, jobs


@pytest.mark.parametrize("request_drift", ["none", "per-model", "coordinated-invocation"])
async def test_retired_cleanup_cannot_reopen_failed_new_execution_or_adopt_its_projection(app, monkeypatch, request_drift):
    old, identity, pending, projection, events, jobs = await failed_new(app, monkeypatch)
    if request_drift == "per-model":
        app.settings.model_infer_overrides = {app.model_id: {"reasoning_effort": "high"}}
    elif request_drift == "coordinated-invocation":
        # Both trusted capture and actual are replaced together: this is outside
        # the cleanup helper's request-only boundary, but cannot grant execution.
        app._owned_effort_invocation = effort_projection.Invocation("high")
        app._cli_effective_thinking = "high"
    with pytest.raises(BackendError, match="owned projection"):
        app._validate_owned_identity()
    with pytest.raises(ValueError, match="uncertain"):
        app._validate_owned_execution()
    # Even without the transition's outer close hold, the request boundary
    # independently refuses the stale projection rather than installing it.
    with monkeypatch.context() as changed:
        changed.setattr(app, "_conversation_transition_error", None)
        with pytest.raises(BackendError, match="owned projection"):
            app._validate_owned_execution()
        capture = {"version": 1, "id": "new-capture", **work.identity(app)}
        with pytest.raises(BackendError, match="owned projection"):
            work.capture(app, capture)
    with pytest.raises(ValueError, match="cannot grant execution"):
        work._command(app, old, "schedule_accept", ("schedule_version", "instance", "proposal_id", "proposal_token"),
                      expected_identity=identity)
    decline = {**old, "type": "gui.work.native_schedule_decline", "id": "late-decline", "reason": "Failed New owns the lane"}
    receipt = await published_rpc(app, monkeypatch, decline)
    read = await published_rpc(app, monkeypatch, {**readback_command(app, receipt), **identity})
    assert read["verified"] and read["identity"] == identity
    assert app._native_schedule_pending is None
    assert app._owned_effort_projection is projection
    assert pending.worker is None and pending.task is None
    assert scheduler.jobs_path(paths.data_root()).read_bytes() == jobs
    assert not app._rewrite_test_requests
    assert not any(row["type"] == "work_input" for row in events)
    with pytest.raises(BackendError, match="owned projection"):
        app._validate_owned_identity()


@contextmanager
def changed_owner(app, monkeypatch, kind):
    session = app._agent_session
    authority_file = session.store.agent_directory(session.authority.name) / "settings.json"
    original = authority_file.read_bytes()
    with monkeypatch.context() as changed:
        if kind == "session":
            changed.setattr(app, "_agent_session", object())
        elif kind == "backend":
            changed.setattr(app, "_backend", SimpleNamespace(name="foreign"))
        elif kind == "model":
            changed.setattr(app, "_model_id", "foreign-model")
        elif kind == "base":
            changed.setattr(app, "_thinking_level", "high")
        elif kind == "settings-base":
            changed.setattr(app.settings, "thinking_level", "high")
        elif kind == "invocation":
            changed.setattr(app, "_cli_effective_thinking", "high")
        elif kind == "missing-invocation":
            changed.setattr(app, "_owned_effort_invocation", None)
        elif kind == "invalid-invocation":
            changed.setattr(app, "_owned_effort_invocation", SimpleNamespace(level=app._cli_effective_thinking))
        elif kind == "changed-invocation":
            changed.setattr(app, "_owned_effort_invocation", effort_projection.Invocation("high"))
        elif kind == "store-object":
            changed.setattr(app, "store", SimpleNamespace(
                agent_session=session, convo_id=app.convo_id, convo_dir=app.convo_dir,
                convo_path=app.convo_path, pending=app.store.pending))
        elif kind == "store-session":
            changed.setattr(app.store, "agent_session", object())
        elif kind == "store-path":
            changed.setattr(app.store, "convo_path", app.convo_dir / "foreign.jsonl")
        elif kind == "lease":
            changed.setattr(session._lease, "pid", -1)
        elif kind == "disk-authority":
            data = json.loads(original)
            data["execution"]["model"] = "foreign-model"
            authority_file.write_text(json.dumps(data), encoding="utf-8")
        else:
            raise AssertionError(kind)
        try:
            yield
        finally:
            if kind == "disk-authority":
                authority_file.write_bytes(original)


@pytest.mark.parametrize("phase", ["decision", "readback-publication"])
@pytest.mark.parametrize("kind", [
    "session", "backend", "model", "base", "settings-base", "invocation",
    "missing-invocation", "invalid-invocation", "changed-invocation",
    "store-object", "store-session", "store-path", "lease", "disk-authority",
])
async def test_retired_cleanup_rejects_changed_current_authority_at_decision_and_publication(app, monkeypatch, kind, phase):
    old, identity, pending, projection, events, jobs = await failed_new(app, monkeypatch)
    decline = {**old, "type": "gui.work.native_schedule_decline", "id": "late-decline", "reason": "Failed New owns the lane"}
    ticket = None
    if phase == "readback-publication":
        result = await published_rpc(app, monkeypatch, decline)
        read_cmd = {**readback_command(app, result), **identity}
        readback = native_schedule.readback(app, read_cmd)
        ticket = native_schedule.prepare_publication(app, read_cmd, readback)
        assert ticket is not None and not pending.readback_published
    with changed_owner(app, monkeypatch, kind), pytest.raises((ValueError, BackendError, OwnershipError)):
        if ticket is None:
            native_schedule.decline(app, decline)
        else:
            native_schedule.finish_publication(app, ticket)
    assert app._native_schedule_pending is pending and not pending.readback_published
    assert app._owned_effort_projection is projection
    assert pending.worker is None and pending.task is None
    assert scheduler.jobs_path(paths.data_root()).read_bytes() == jobs
    assert not app._rewrite_test_requests
    assert not any(row["type"] == "work_input" for row in events)
    if ticket is not None:
        # Restoring the same owner permits only this retained exact ticket.
        native_schedule.finish_publication(app, ticket)
        assert app._native_schedule_pending is None
    else:
        assert pending.state == "proposed" and pending.result is None
        result = await published_rpc(app, monkeypatch, decline)
        read = await published_rpc(app, monkeypatch, {**readback_command(app, result), **identity})
        assert read["verified"] and app._native_schedule_pending is None
