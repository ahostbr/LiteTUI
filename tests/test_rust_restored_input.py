"""Restored provider input stays held in the Rust-owned runtime."""
from __future__ import annotations

import copy
from types import SimpleNamespace as NS

import pytest
import test_rust_conversation_open

from litetui import cli, codex_steering, gui_rpc, hook_host
from litetui.app import LiteTUI
from litetui.claude_turn import queue_ready
from litetui.conversation import ConversationRepository

# Share the real owned-app fixture; no second ownership harness.
saved_app = test_rust_conversation_open.saved_app


def entry(state="queued"):
    return {"id": "saved-input", "threadId": "saved-thread", "turnId": "saved-turn",
            "state": state, "revision": 2,
            "item": {"content": "UNSENT SAVED INPUT", "source": "queued", "tool_profile": "strict"}}


def seed(app, state="queued"):
    value = entry(state)
    value["conversationId"] = app.convo_id
    app._append({"role": "assistant", "content": "Saved answer",
                 "provider_metadata": {"steering": [value]}})
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "admitting", "admitted", "next_turn", "sending", "uncertain", "accepted"])
async def test_owned_resume_holds_restored_entries_without_changing_journal(saved_app, state):
    app = saved_app
    app._hold_restored_input = True
    seed(app, state)
    saved = copy.deepcopy(ConversationRepository.read(app.convo_path)[1][-1]["provider_metadata"])
    assert app._resume(app.convo_path, startup=True)
    # Resume may refresh process/store metadata. Holding never rewrites the journal.
    before = app.convo_path.read_bytes()
    assert ConversationRepository.read(app.convo_path)[1][-1]["provider_metadata"] == saved
    codex_steering.restore_queue(app)  # Repeated discovery must not duplicate input.
    assert len(app._pending_input) == 1
    assert app._pending_input[0]["_restored_input_held"] is True
    assert app._pending_input[0]["_codex_entry"]["state"] == state
    assert app.convo_path.read_bytes() == before
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("litetui.gpu_gate.is_rtx_5090", lambda: False)
        state = gui_rpc.dispatch(app, {"type": "gui.state"})
    assert state["restored_input"] == {"policy": "hold", "held": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["idle", "steering", "recovery"])
async def test_all_native_consumers_leave_held_input_and_ledger_untouched(saved_app, monkeypatch, path):
    app = saved_app
    seed(app)
    assert app._resume(app.convo_path, startup=True)
    item = app._pending_input[0]
    item["_restored_input_held"] = True
    before = app.convo_path.read_bytes()
    ledger_before = copy.deepcopy(item["_codex_entry"])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Held input reached dispatch, admission, persistence or provider")

    async def forbidden_async(*_args, **_kwargs):
        forbidden()

    monkeypatch.setattr(hook_host, "start_prompt", forbidden)
    server = NS(start=forbidden_async, request=forbidden_async)
    if path == "idle":
        app._flush_pending_input()
    elif path == "recovery":
        assert not await codex_steering.recover_queue_head(app, server)
    else:
        steering = codex_steering.HostSteering(app, server, "saved-thread", "saved-turn",
                                             {"steering": [item["_codex_entry"]]}, forbidden)
        monkeypatch.setattr(steering, 'admit', forbidden_async)
        await steering.run()
    assert app._pending_input == [item]
    assert item["_codex_entry"] == ledger_before
    assert app.convo_path.read_bytes() == before


@pytest.mark.asyncio
async def test_automatic_policy_keeps_ordinary_python_queue_behavior(saved_app, monkeypatch):
    app = saved_app
    assert app._hold_restored_input is False
    seed(app)
    assert app._resume(app.convo_path, startup=True)
    calls = []
    monkeypatch.setattr(hook_host, "start_prompt", lambda _app, item: calls.append(item["content"]))
    app._flush_pending_input()
    assert calls == ["UNSENT SAVED INPUT"]
    assert app._pending_input == []


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["ready", "mid_turn"])
async def test_generic_consumers_cannot_bypass_a_restored_hold(consumer, saved_app, monkeypatch):
    item = {"content": "held", "_restored_input_held": True}
    app = saved_app
    app._pending_input = [item]
    app._stop_requested = False
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Held generic input reached admission")
    monkeypatch.setattr(hook_host, "accept_prompt", forbidden)
    if consumer == "ready":
        assert not queue_ready(app, item)
    else:
        assert not LiteTUI._deliver_queued_input(app)
    assert app._pending_input == [item]


def test_hold_flag_requires_rpc_before_any_owned_startup(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["litetui", "--hold-restored-input"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "--hold-restored-input requires --rpc" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_hold_policy_does_not_block_new_explicit_queue_input(saved_app, monkeypatch):
    app = saved_app
    app._hold_restored_input = True
    app._pending_input = [{"content": "new explicit input", "source": "rpc"}]
    calls = []
    monkeypatch.setattr(hook_host, "start_prompt", lambda _app, item: calls.append(item["content"]))
    app._flush_pending_input()
    assert calls == ["new explicit input"]
    assert app._pending_input == []


def command(app, action="list", **values):
    return gui_rpc.dispatch(app, {"type": f"gui.restored_input.{action}",
                                 "session_id": app.convo_id, **values})


def resume_held(app, state="queued"):
    app._hold_restored_input = True
    seed(app, state)
    assert app._resume(app.convo_path, startup=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "next_turn"])
async def test_cancel_persists_denial_and_does_not_restore_on_next_open(saved_app, state):
    app = saved_app
    resume_held(app, state)
    before = app.convo_path.read_bytes()
    view = command(app)
    assert app.convo_path.read_bytes() == before
    assert view["held"] == 1
    assert view["entries"][0]["can_cancel"] is True
    assert command(app, "cancel", snapshot=view["snapshot"], entry_id="saved-input") == {
        "cancelled": "saved-input", "session_id": app.convo_id}
    assert command(app)["held"] == 0
    saved = ConversationRepository.read(app.convo_path)[1][-1]["provider_metadata"]["steering"][0]
    assert saved["state"] == "denied"
    assert saved["revision"] == 3
    assert saved["admission"] == {"reason": "Cancelled by user"}
    assert app._resume(app.convo_path, startup=True)
    assert app._pending_input == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["admitting", "admitted", "sending", "uncertain", "accepted"])
async def test_provider_evidence_cannot_be_discarded_by_cancel(saved_app, state):
    app = saved_app
    resume_held(app, state)
    view = command(app)
    assert view["entries"][0]["can_cancel"] is False
    before = app.convo_path.read_bytes()
    with pytest.raises(ValueError, match="Only queued"):
        command(app, "cancel", snapshot=view["snapshot"], entry_id="saved-input")
    assert app.convo_path.read_bytes() == before
    assert len(app._pending_input) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["snapshot", "session", "revision", "foreign", "duplicate", "stale_record", "ledger", "materialized", "busy", "persist_error"])
async def test_cancel_revalidates_identity_authority_and_saved_evidence(saved_app, monkeypatch, fault):
    app = saved_app
    resume_held(app)
    view = command(app)
    request = {"type": "gui.restored_input.cancel", "session_id": app.convo_id,
               "snapshot": view["snapshot"], "entry_id": "saved-input"}
    item = app._pending_input[0]
    if fault == "snapshot":
        request["snapshot"] = "stale"
    elif fault == "session":
        request["session_id"] = "another-conversation"
    elif fault == "revision":
        item["_codex_entry"]["revision"] += 1
    elif fault == "foreign":
        item["_codex_entry"]["conversationId"] = "another-conversation"
    elif fault == "duplicate":
        app._pending_input.append(item)
    elif fault == "stale_record":
        app.conversation[-1]["provider_metadata"]["steering"] = [copy.deepcopy(item["_codex_entry"])]
    elif fault == "ledger":
        item["_codex_ledger"].entries = [copy.deepcopy(item["_codex_entry"])]
    elif fault == "materialized":
        app.conversation.append({"role": "user", "content": "Already sent", "codex_delivery": {"id": "saved-input"}})
    elif fault == "busy":
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    else:
        app.store.persist_error = "disk unavailable"
    before = app.convo_path.read_bytes()
    with pytest.raises(ValueError):
        gui_rpc.dispatch(app, request)
    assert app.convo_path.read_bytes() == before
    assert app._pending_input[0] is item


@pytest.mark.asyncio
async def test_failed_save_retains_hold_and_refuses_memory_only_readback(saved_app, monkeypatch):
    app = saved_app
    resume_held(app)
    view = command(app)
    before = app.convo_path.read_bytes()
    def fail(*_args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(codex_steering, "save_at", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        command(app, "cancel", snapshot=view["snapshot"], entry_id="saved-input")
    assert len(app._pending_input) == 1
    assert app._pending_input[0]["_restored_input_held"]
    assert app.convo_path.read_bytes() == before
    with pytest.raises(ValueError, match="durably saved"):
        command(app)


@pytest.mark.asyncio
async def test_cancel_uses_current_metadata_position_not_restored_callback(saved_app, monkeypatch):
    app = saved_app
    resume_held(app)
    def forbidden():
        raise AssertionError("Stale callback used")
    monkeypatch.setattr(app._pending_input[0]["_codex_ledger"], "save", forbidden)
    view = command(app)
    command(app, "cancel", snapshot=view["snapshot"], entry_id="saved-input")
    assert command(app)["held"] == 0


@pytest.mark.asyncio
async def test_memory_only_save_is_not_acknowledged_as_cancelled(saved_app, monkeypatch):
    app = saved_app
    resume_held(app)
    view = command(app)
    monkeypatch.setattr(codex_steering, "save_at", lambda *_args: None)
    with pytest.raises(OSError, match="verified in the saved journal"):
        command(app, "cancel", snapshot=view["snapshot"], entry_id="saved-input")
    assert len(app._pending_input) == 1
    with pytest.raises(ValueError, match="durably saved"):
        command(app)


@pytest.mark.asyncio
async def test_changed_hidden_journal_fields_invalidate_snapshot(saved_app):
    app = saved_app
    resume_held(app)
    view = command(app)
    app._pending_input[0]["_codex_entry"]["item"]["operation_id"] = "new-operation"
    codex_steering.save_at(app, len(app.conversation) - 1)
    with pytest.raises(ValueError, match="changed"):
        command(app, "cancel", snapshot=view["snapshot"], entry_id="saved-input")
