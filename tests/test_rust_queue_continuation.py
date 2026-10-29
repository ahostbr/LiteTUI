"""Explicit queue continuation uses actual owned journals; no provider calls."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest
import test_rust_conversation_open
from test_rust_restored_input import seed

from litetui import codex_steering, gui_rpc, hook_host, restored_input, seat_authority
from litetui.conversation import ConversationRepository

saved_app = test_rust_conversation_open.saved_app


def setup(app, monkeypatch, state="queued", source="queued", profile="strict"):
    app._hold_restored_input = True
    app._cli_tool_profile = "strict"
    value = seed(app, state)
    value["item"].update(source=source, tool_profile=profile, operation_id="old-operation")
    codex_steering.save_at(app, len(app.conversation) - 1)
    assert app._resume(app.convo_path, startup=True)
    monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: None)
    events, origins = [], []
    monkeypatch.setattr(app, "_rpc_emit", lambda event, **_kw: events.append(event))
    monkeypatch.setattr(app, "_stream", lambda *, origin: origins.append(origin))
    snapshot = gui_rpc.dispatch(app, {"type": "gui.restored_input.list", "session_id": app.convo_id})
    command = {"type": "gui.restored_input.continue", "id": "continue-owned",
               "session_id": app.convo_id, "snapshot": snapshot["snapshot"], "entry_id": value["id"]}
    return command, events, origins


def saved_entry(app):
    return ConversationRepository.read(app.convo_path)[1][2]["provider_metadata"]["steering"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "next_turn"])
@pytest.mark.parametrize("source", ["queued", "harness", "goal", ""])
async def test_continuation_keeps_source_authority_and_persists_before_schedule(saved_app, monkeypatch, state, source):
    app = saved_app
    command, events, origins = setup(app, monkeypatch, state, source, "autonomous")
    original = saved_entry(app)["item"].copy()
    def schedule(*, origin):
        persisted = saved_entry(app)
        assert persisted["state"] == "admitted"
        assert persisted["continuation_operation_id"] == command["id"]
        assert persisted["item"] == original
        messages = ConversationRepository.read(app.convo_path)[1]
        assert sum(m.get("codex_delivery", {}).get("id") == "saved-input" for m in messages) == 1
        assert app._active_tool_profile == "strict"
        assert app._pending_input == []
        origins.append(origin)
    monkeypatch.setattr(app, "_stream", schedule)
    result = await gui_rpc.async_dispatch(app, command)
    assert result == {"turn": "accepted", "entry_id": "saved-input", "session_id": app.convo_id, "held": 0}
    assert len(origins) == 1
    assert origins[0].operation_id == "continue-owned"
    assert origins[0].source == (source or "unlabelled")
    assert origins[0].session_id == app.convo_id
    assert origins[0].execution_id is None  # _stream assigns the invocation identity.
    assert events == []
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, command)
    assert len(origins) == 1
    assert app._resume(app.convo_path, startup=True)
    assert app._pending_input == []


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["floor", "hook", "stop", "schedule"])
async def test_known_admission_refusal_retains_hold_without_fake_turn_end(saved_app, monkeypatch, refusal):
    app = saved_app
    command, events, origins = setup(app, monkeypatch, source="scheduled" if refusal == "schedule" else "queued",
                                    profile="autonomous" if refusal == "schedule" else "strict")
    if refusal == "floor":
        monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: "below floor")
    elif refusal in ("hook", "stop"):
        monkeypatch.setattr(hook_host, "snapshot", lambda _app: NS(hooks=[object()], error=None))
        async def dispatch(*_args, **_kwargs):
            if refusal == "stop":
                app._stop_requested = True
            return NS(allowed=refusal == "stop", reason="" if refusal == "stop" else "hook refused")
        monkeypatch.setattr(hook_host, "dispatch", dispatch)
    result = await gui_rpc.async_dispatch(app, command)
    assert result["turn"] == "refused" and result["reason"]
    assert result["held"] == 1
    assert origins == events == []
    assert len(app._pending_input) == 1 and restored_input.held(app._pending_input[0])
    assert saved_entry(app)["state"] == "queued"
    assert not getattr(app, "_restored_input_error", False)
    assert not any(m.get("codex_delivery") for m in ConversationRepository.read(app.convo_path)[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stale", "unsafe", "later", "profile", "busy", "maintenance"])
async def test_invalid_continuation_never_claims_or_schedules(saved_app, monkeypatch, fault):
    app = saved_app
    command, events, origins = setup(app, monkeypatch, state="sending" if fault == "unsafe" else "queued",
                                    profile="unknown" if fault == "profile" else "strict")
    if fault == "stale":
        command["snapshot"] = "stale"
    elif fault == "later":
        app._pending_input.insert(0, {"content": "earlier input"})
        command["snapshot"] = restored_input.inspect(app, app.convo_id)["snapshot"]
    elif fault == "busy":
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    elif fault == "maintenance":
        app._mcp_maintenance = True
    before = app.convo_path.read_bytes()
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, command)
    assert app.convo_path.read_bytes() == before
    assert events == origins == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["queue", "conversation", "metadata"])
async def test_admission_suspension_revalidates_identity_and_serializes_commands(saved_app, monkeypatch, fault):
    app = saved_app
    command, events, origins = setup(app, monkeypatch)
    monkeypatch.setattr(hook_host, "snapshot", lambda _app: NS(hooks=[object()], error=None))
    entered, release = asyncio.Event(), asyncio.Event()
    async def dispatch(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return NS(allowed=True, reason="")
    monkeypatch.setattr(hook_host, "dispatch", dispatch)
    operation = asyncio.create_task(gui_rpc.async_dispatch(app, command))
    await asyncio.wait_for(entered.wait(), 2)
    assert app._gui_management_busy
    assert saved_entry(app)["state"] == "admitting"
    with pytest.raises(ValueError, match="management operation"):
        await gui_rpc.async_dispatch(app, command)
    with pytest.raises(ValueError, match="management"):
        gui_rpc.dispatch(app, {"type": "gui.prompt.submit", "message": "must not send"})
    if fault == "queue":
        app._pending_input.clear()
    elif fault == "conversation":
        app.conversation = list(app.conversation)
    else:
        app._pending_input[0]["_codex_entry"]["item"]["content"] = "changed"
    release.set()
    with pytest.raises(ValueError):
        await operation
    assert not app._gui_management_busy
    assert events == origins == []
    assert app._restored_input_error
    assert not any(m.get("codex_delivery") for m in ConversationRepository.read(app.convo_path)[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["claim", "materialize", "admitted", "schedule"])
async def test_ambiguous_persistence_or_schedule_never_reports_started_success(saved_app, monkeypatch, failure):
    app = saved_app
    command, events, origins = setup(app, monkeypatch)
    actual_save = codex_steering.save_at
    calls = []
    def save(host, index):
        calls.append(index)
        if failure == "claim" or failure == "admitted" and len(calls) == 2:
            return  # Deliberately lie about saving; readback must detect this.
        actual_save(host, index)
    monkeypatch.setattr(codex_steering, "save_at", save)
    if failure == "materialize":
        monkeypatch.setattr(app, "_append", lambda *_: None)
    if failure == "schedule":
        def fail(*, origin):
            raise RuntimeError("worker could not be scheduled")
        monkeypatch.setattr(app, "_stream", fail)
    with pytest.raises((OSError, RuntimeError)):
        await gui_rpc.async_dispatch(app, command)
    assert origins == events == []
    assert app._restored_input_error
    with pytest.raises(ValueError, match="durably saved"):
        restored_input.inspect(app, app.convo_id)
    if failure != "schedule":
        assert restored_input.held(app._pending_input[0])


@pytest.mark.asyncio
async def test_cancelled_admission_stays_claimed_and_is_not_automatically_retried(saved_app, monkeypatch):
    app = saved_app
    command, events, origins = setup(app, monkeypatch)
    monkeypatch.setattr(hook_host, "snapshot", lambda _app: NS(hooks=[object()], error=None))
    entered = asyncio.Event()
    async def dispatch(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(hook_host, "dispatch", dispatch)
    operation = asyncio.create_task(gui_rpc.async_dispatch(app, command))
    await asyncio.wait_for(entered.wait(), 2)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert app._restored_input_error
    assert saved_entry(app)["state"] == "admitting"
    assert restored_input.held(app._pending_input[0])
    app._flush_pending_input()
    assert origins == events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_actual_rpc_dispatch_returns_correlated_admission_outcome(saved_app, monkeypatch, refused):
    from litetui import rpc

    app = saved_app
    command, events, origins = setup(app, monkeypatch)
    if refused:
        monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: "below floor")
    records, workers = [], []
    monkeypatch.setattr(rpc, "rpc_emit", lambda value: records.append(value))
    def run_worker(work, **_kwargs):
        task = asyncio.create_task(work)
        workers.append(task)
        return task
    monkeypatch.setattr(app, "run_worker", run_worker)
    rpc._dispatch(app, command)
    assert len(workers) == 1
    await workers[0]
    assert len(records) == 1
    response = records[0]
    assert response["type"] == "response" and response["id"] == command["id"]
    assert response["ok"] is True
    assert response["result"]["turn"] == ("refused" if refused else "accepted")
    assert len(origins) == (0 if refused else 1)
    assert events == []
    assert not app._gui_management_busy


@pytest.mark.asyncio
async def test_refused_attempt_requires_a_fresh_snapshot_before_explicit_retry(saved_app, monkeypatch):
    app = saved_app
    command, events, origins = setup(app, monkeypatch)
    monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: "below floor")
    assert (await gui_rpc.async_dispatch(app, command))["turn"] == "refused"
    with pytest.raises(ValueError, match="changed"):
        await gui_rpc.async_dispatch(app, command)
    monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: None)
    fresh = restored_input.inspect(app, app.convo_id)
    command.update(id="second-explicit-attempt", snapshot=fresh["snapshot"])
    assert (await gui_rpc.async_dispatch(app, command))["turn"] == "accepted"
    assert len(origins) == 1 and origins[0].operation_id == "second-explicit-attempt"
    assert events == []


@pytest.mark.asyncio
async def test_command_cannot_relabel_unattended_input_or_widen_its_profile(saved_app, monkeypatch):
    app = saved_app
    command, events, origins = setup(app, monkeypatch, source="harness", profile="strict")
    app._cli_tool_profile = None
    assert app.set_tool_profile("autonomous", announce=False)
    assert app.chosen_tool_profile == "autonomous"
    command.update(source="rpc", tool_profile="autonomous", message="replacement")
    assert (await gui_rpc.async_dispatch(app, command))["turn"] == "accepted"
    assert app._active_tool_profile == "strict"
    assert origins[0].source == "harness"
    assert app.conversation[-1]["content"] == "UNSENT SAVED INPUT"
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "   ", 17, {}])
async def test_invalid_saved_content_is_not_offered_for_continuation(saved_app, monkeypatch, value):
    app = saved_app
    command, events, origins = setup(app, monkeypatch)
    app._pending_input[0]["_codex_entry"]["item"]["content"] = value
    codex_steering.save_at(app, len(app.conversation) - 1)
    fresh = restored_input.inspect(app, app.convo_id)
    assert fresh["entries"][0]["can_continue"] is False
    command["snapshot"] = fresh["snapshot"]
    before = app.convo_path.read_bytes()
    with pytest.raises(ValueError, match="eligible"):
        await gui_rpc.async_dispatch(app, command)
    assert app.convo_path.read_bytes() == before
    assert events == origins == []
