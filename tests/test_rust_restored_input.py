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
