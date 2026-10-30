"""Explicit Claude recovery uses owned persistence and controlled worker boundaries."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest
import test_rust_claude_recovery

from litetui import gui_rpc, hook_host, restored_input, seat_authority
from litetui.claude_persistence import ClaudeLedger, LedgerError
from litetui.conversation import ConversationRepository

saved_app = test_rust_claude_recovery.saved_app
seed = test_rust_claude_recovery.seed
read = test_rust_claude_recovery.read


def user_message(entry):
    return {"role": "user", "content": entry["content"],
            "claude_delivery": {"id": entry["id"], "segment_id": entry["segment_id"], "state": "prepared"}}


def saved_entry(app, ident):
    segment = ClaudeLedger(app.convo_dir).selected
    assert segment is not None
    return next(entry for entry in segment["entries"] if entry["id"] == ident)


def setup(app, monkeypatch, *, materialized=False, source="harness", profile="strict", state="prepared"):
    entry = seed(app, state, source=source, profile=profile, operation="original-operation")
    if materialized:
        app._append(user_message(entry))
    assert app._resume(app.convo_path, startup=True)
    app._cli_tool_profile = "strict"
    monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: None)
    origins, events = [], []
    monkeypatch.setattr(app, "_rpc_emit", lambda event, **_: events.append(event))
    monkeypatch.setattr(app, "_stream", lambda *, origin: origins.append(origin))
    snapshot = read(app)
    command = {"type": "gui.restored_input.continue", "id": "continue-claude",
               "session_id": app.convo_id, "entry_id": entry["id"], "snapshot": snapshot["snapshot"]}
    return entry, command, origins, events


def request(app, command):
    return {"type": "gui.restored_input.settled", "id": "read-settled",
            "session_id": app.convo_id, "operation_id": command["id"], "entry_id": command["entry_id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("materialized", [False, True])
@pytest.mark.parametrize("source", ["harness", "queued", "goal", ""])
async def test_explicit_continuation_preserves_origin_and_reuses_original_user_record(
        saved_app, monkeypatch, materialized, source):
    app = saved_app
    entry, command, origins, events = setup(
        app, monkeypatch, materialized=materialized, source=source, profile="autonomous")
    before = len(app.conversation)
    def schedule(*, origin):
        current = saved_entry(app, entry["id"])
        assert current["state"] == "prepared"
        assert current["operation_id"] == "original-operation"
        assert current["continuation_operation_id"] == command["id"]
        assert current["continuation_outcome"] == "admitted"
        assert all(current[key] == entry[key] for key in ("content", "profile", "source", "segment_id"))
        assert app._active_tool_profile == "strict"
        assert app._claude_active_input["_restored_claude_continuation"] is True
        assert app._claude_active_input["operation_id"] == command["id"]
        assert len(app._pending_input) == 1 and restored_input.held(app._pending_input[0])
        app._flush_pending_input()
        assert not app._deliver_queued_input()
        origins.append(origin)
    monkeypatch.setattr(app, "_stream", schedule)
    result = await gui_rpc.async_dispatch(app, command)
    assert result == {"turn": "accepted", "entry_id": entry["id"], "session_id": app.convo_id, "held": 1}
    assert len(app.conversation) == before + (not materialized)
    messages = ConversationRepository.read(app.convo_path)[1]
    assert [message for message in messages if message.get("claude_delivery")] == [user_message(entry)]
    assert len(origins) == 1 and origins[0].operation_id == command["id"]
    assert origins[0].session_id == app.convo_id and origins[0].source == (source or "unlabelled")
    assert origins[0].execution_id is None
    assert events == []
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, command)


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["floor", "hook", "stop", "schedule"])
async def test_known_refusal_preserves_prepared_hold_without_worker_or_fake_terminal(
        saved_app, monkeypatch, refusal):
    app = saved_app
    entry, command, origins, events = setup(
        app, monkeypatch, source="scheduled" if refusal == "schedule" else "harness",
        profile="autonomous" if refusal == "schedule" else "strict")
    before = app.convo_path.read_bytes()
    if refusal == "floor":
        monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: "below floor")
    elif refusal in {"hook", "stop"}:
        monkeypatch.setattr(hook_host, "snapshot", lambda _: NS(hooks=[object()], error=None))
        async def dispatch(*_args, **_kwargs):
            app._stop_requested = refusal == "stop"
            return NS(allowed=refusal == "stop", reason="hook refused")
        monkeypatch.setattr(hook_host, "dispatch", dispatch)
    result = await gui_rpc.async_dispatch(app, command)
    assert result["turn"] == "refused" and result["reason"]
    assert result["held"] == 1 and origins == events == []
    assert app.convo_path.read_bytes() == before
    settled = await gui_rpc.async_dispatch(app, request(app, command))
    assert settled["continuation_provider"] == "claude"
    assert settled["continuation_state"] == "prepared" and settled["continuation_outcome"] == "refused"
    assert settled["entries"][0]["id"] == entry["id"]
    assert not getattr(app, "_restored_input_error", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stale", "uncertain", "later", "other-uncertain", "workspace",
                                  "profile", "busy", "maintenance", "duplicate-user", "changed-user"])
async def test_ineligible_or_conflicting_input_never_reaches_admission(saved_app, monkeypatch, fault):
    app = saved_app
    entry, command, origins, events = setup(
        app, monkeypatch, state="uncertain" if fault == "uncertain" else "prepared",
        profile="invented" if fault == "profile" else "strict")
    if fault == "stale":
        command["snapshot"] = "stale"
    elif fault == "later":
        app._pending_input.insert(0, {"content": "earlier input"})
    elif fault == "other-uncertain":
        seed(app, "uncertain", content="other uncertain")
        assert app._resume(app.convo_path, startup=True)
    elif fault == "workspace":
        monkeypatch.setattr("litetui.claude_turn.launch_workspace", lambda _: "elsewhere")
    elif fault == "busy":
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    elif fault == "maintenance":
        app._mcp_maintenance = True
    elif fault in {"duplicate-user", "changed-user"}:
        message = user_message(entry)
        app._append(message)
        if fault == "duplicate-user":
            app._append(message)
        else:
            app.conversation[-1]["content"] = "different live content"
    if fault not in {"stale", "busy"}:
        command["snapshot"] = read(app)["snapshot"]
    before = app._claude_ledger.file.read_bytes()
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, command)
    assert app._claude_ledger.file.read_bytes() == before
    assert origins == events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["queue", "conversation", "metadata", "owner"])
async def test_admission_rechecks_after_hooks_and_serializes_other_commands(saved_app, monkeypatch, fault):
    app = saved_app
    entry, command, origins, events = setup(app, monkeypatch)
    monkeypatch.setattr(hook_host, "snapshot", lambda _: NS(hooks=[object()], error=None))
    entered, release = asyncio.Event(), asyncio.Event()
    async def dispatch(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return NS(allowed=True, reason="")
    monkeypatch.setattr(hook_host, "dispatch", dispatch)
    pending = asyncio.create_task(gui_rpc.async_dispatch(app, command))
    await asyncio.wait_for(entered.wait(), 2)
    assert saved_entry(app, entry["id"])["continuation_outcome"] == "admitting"
    with pytest.raises(ValueError, match="management"):
        await gui_rpc.async_dispatch(app, command)
    owner = app._agent_session
    if fault == "queue":
        app._pending_input.clear()
    elif fault == "conversation":
        app.conversation = list(app.conversation)
    elif fault == "metadata":
        app._pending_input[0]["_claude_entry"]["content"] = "changed"
    else:
        app._agent_session = None
    release.set()
    try:
        with pytest.raises(ValueError):
            await pending
    finally:
        app._agent_session = owner
    assert origins == events == [] and app._restored_input_error
    assert not any(message.get("claude_delivery") for message in app.conversation)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["claim", "materialize", "admitted", "schedule"])
async def test_ambiguous_persistence_never_schedules_or_unlocks_replay(saved_app, monkeypatch, failure):
    app = saved_app
    entry, command, origins, _ = setup(app, monkeypatch)
    ledger = app._claude_ledger
    original_save = ledger._save
    calls: list[None] = []
    def save():
        calls.append(None)
        if failure == "claim" or failure == "admitted" and len(calls) == 2:
            return
        original_save()
    monkeypatch.setattr(ledger, "_save", save)
    if failure == "materialize":
        monkeypatch.setattr(app, "_append", lambda *_: None)
    elif failure == "schedule":
        def fail(**_kwargs):
            raise RuntimeError("worker scheduling failed")
        monkeypatch.setattr(app, "_stream", fail)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        await gui_rpc.async_dispatch(app, command)
    assert origins == [] and app._restored_input_error
    with pytest.raises(ValueError, match="durably saved"):
        read(app)
    assert saved_entry(app, entry["id"])["state"] == "prepared"


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_state", ["prepared", "uncertain", "terminal"])
async def test_worker_retirement_reholds_unresolved_delivery_and_preserves_queue_order(
        saved_app, monkeypatch, delivery_state):
    app = saved_app
    entry, command, origins, _ = setup(app, monkeypatch, materialized=True)
    other = seed(app, content="second held input")
    assert app._resume(app.convo_path, startup=True)
    command["snapshot"] = read(app)["snapshot"]
    entered, release = asyncio.Event(), asyncio.Event()
    class Worker:
        async def wait(self):
            entered.set()
            await release.wait()
    monkeypatch.setattr(app, "_stream", lambda **_: Worker())
    await gui_rpc.async_dispatch(app, command)
    monkeypatch.setattr(app, "_chat_running", lambda: not release.is_set())
    pending = asyncio.create_task(gui_rpc.async_dispatch(app, request(app, command)))
    await asyncio.wait_for(entered.wait(), 2)
    assert not pending.done() and app._gui_management_busy
    if delivery_state != "prepared":
        app._claude_ledger.update_delivery(entry["id"], "submitted")
        app._claude_ledger.update_delivery(entry["id"], delivery_state)
    # The retired-worker callback may flush before the read coroutine resumes.
    # The held barrier must block both ordinary delivery consumers at that point.
    release.set()
    app._flush_pending_input()
    assert not app._deliver_queued_input()
    result = await asyncio.wait_for(pending, 2)
    assert result["continuation_outcome"] == "accepted"
    assert result["continuation_provider"] == "claude"
    assert result["continuation_state"] == delivery_state
    expected = ([entry["id"]] if delivery_state != "terminal" else []) + [other["id"]]
    assert [row["id"] for row in result["entries"]] == expected
    assert all(restored_input.held(item) for item in app._pending_input)
    assert [message for message in app.conversation if message.get("claude_delivery")] == [user_message(entry)]
    again = await gui_rpc.async_dispatch(app, request(app, command))
    assert again["snapshot"] == result["snapshot"]
    assert origins == []


@pytest.mark.asyncio
async def test_prepared_after_retirement_can_be_explicitly_retried_with_same_native_delivery(
        saved_app, monkeypatch):
    app = saved_app
    entry, command, origins, _ = setup(app, monkeypatch, materialized=True)
    class Worker:
        async def wait(self):
            pass
    def schedule(*, origin):
        origins.append(origin)
        return Worker()
    monkeypatch.setattr(app, "_stream", schedule)
    await gui_rpc.async_dispatch(app, command)
    settled = await gui_rpc.async_dispatch(app, request(app, command))
    assert settled["entries"][0]["can_continue"]
    command["snapshot"] = settled["snapshot"]
    with pytest.raises(LedgerError, match="fresh operation"):
        await gui_rpc.async_dispatch(app, command)
    # The invalid reuse is fail-closed in this connection. Reopen to retry
    # explicitly; the prepared journal still proves no provider submission.
    app._restored_input_error = False
    assert app._resume(app.convo_path, startup=True)
    command.update(id="fresh-explicit-retry", snapshot=read(app)["snapshot"])
    await gui_rpc.async_dispatch(app, command)
    assert [origin.operation_id for origin in origins] == ["continue-claude", "fresh-explicit-retry"]
    assert [message for message in app.conversation if message.get("claude_delivery")] == [user_message(entry)]



@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["queue", "payload", "segment", "disk", "transcript", "inflight"])
async def test_settlement_rejects_changed_or_unverified_delivery_evidence(saved_app, monkeypatch, fault):
    app = saved_app
    entry, command, _, _ = setup(app, monkeypatch)
    class Worker:
        async def wait(self):
            pass
    monkeypatch.setattr(app, "_stream", lambda **_: Worker())
    await gui_rpc.async_dispatch(app, command)
    ledger = app._claude_ledger
    if fault == "queue":
        app._pending_input.clear()
    elif fault == "payload":
        ledger.update_delivery(entry["id"], "terminal", content="replaced")
    elif fault == "segment":
        ledger.select_segment("foreign workspace", new=True)
    elif fault == "disk":
        external = ClaudeLedger(app.convo_dir)
        external.update_delivery(entry["id"], "terminal")
    elif fault == "transcript":
        app._append(user_message(entry))
    else:
        ledger.update_delivery(entry["id"], "submitted")
    with pytest.raises((ValueError, OSError)):
        await gui_rpc.async_dispatch(app, request(app, command))
    assert app._restored_input_error


@pytest.mark.asyncio
async def test_cancelled_admission_latches_without_materialization_or_scheduling(saved_app, monkeypatch):
    app = saved_app
    entry, command, origins, events = setup(app, monkeypatch)
    monkeypatch.setattr(hook_host, "snapshot", lambda _: NS(hooks=[object()], error=None))
    entered = asyncio.Event()
    async def dispatch(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(hook_host, "dispatch", dispatch)
    pending = asyncio.create_task(gui_rpc.async_dispatch(app, command))
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert app._restored_input_error and not app._gui_management_busy
    assert saved_entry(app, entry["id"])["state"] == "prepared"
    assert origins == events == []
    assert not any(message.get("claude_delivery") for message in app.conversation)
    assert restored_input.held(app._pending_input[0])
