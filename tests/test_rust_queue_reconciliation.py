"""Owned history reconciliation records evidence without executing saved input."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace as NS

import pytest
import test_rust_queue_continuation

from litetui import codex_steering, gui_rpc, hook_host, restored_input
from litetui.conversation import ConversationRepository
from litetui.model_transport import ProviderError

saved_app = test_rust_queue_continuation.saved_app


def setup(app, monkeypatch, state="uncertain", *, paginated=False, fault=None):
    command, events, origins = test_rust_queue_continuation.setup(app, monkeypatch, state)
    calls = []
    turn: dict = {"id": "saved-turn", "items": [{"type": "userMessage", "clientId": "saved-input"}]}
    if fault == "absent":
        turn["items"] = []
    elif fault == "turn":
        turn["id"] = "other-turn"
    elif fault == "client":
        turn["items"][0]["clientId"] = "other-input"
    elif fault == "type":
        turn["items"][0]["type"] = "agentMessage"
    elif fault == "duplicate":
        turn["items"] *= 2
    thread = {"id": "saved-thread", "turns": [turn]}
    if fault == "thread":
        thread["id"] = "other-thread"
    if paginated:
        thread.update(historyMode="paginated", turns=[])
    async def start():
        calls.append(("start", None))
    async def request(method, params):
        calls.append((method, params))
        assert method in ("thread/read", "thread/turns/list"), "A send/resume reached the provider"
        if fault == "network":
            raise OSError("controlled unavailable history")
        if fault == "budget":
            return {"padding": "x" * (8 * 1024 * 1024 + 1)}
        if method == "thread/turns/list":
            return {"data": [turn], "nextCursor": None}
        return {"thread": thread}
    monkeypatch.setattr(app.backend, "app_server", NS(start=start, request=request))
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Reconciliation attempted admission or execution")
    monkeypatch.setattr(app, "_stream", forbidden)
    monkeypatch.setattr(hook_host, "accept_prompt", forbidden)
    monkeypatch.setattr(hook_host, "admit_prompt", forbidden)
    command["type"] = "gui.restored_input.reconcile"
    return command, calls, events, origins


def saved(app):
    return ConversationRepository.read(app.convo_path)[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["admitting", "admitted", "sending", "uncertain", "accepted"])
@pytest.mark.parametrize("paginated", [False, True])
async def test_positive_exact_history_is_saved_once_without_admission(saved_app, monkeypatch, state, paginated):
    app = saved_app
    command, calls, events, origins = setup(app, monkeypatch, state, paginated=paginated)
    profile, hook, operation = app._active_tool_profile, app._hook_turn_id, getattr(app, "_gui_operation_id", None)
    original = copy.deepcopy(app._pending_input[0]["_codex_entry"]["item"])
    result = await gui_rpc.async_dispatch(app, command)
    assert result == {"session_id": app.convo_id, "entry_id": "saved-input", "reconciled": True}
    assert not app._pending_input
    assert events == origins == []
    assert (app._active_tool_profile, app._hook_turn_id, getattr(app, "_gui_operation_id", None)) == (profile, hook, operation)
    rows = [m for m in saved(app) if m.get("codex_delivery", {}).get("id") == "saved-input"]
    assert len(rows) == 1 and rows[0]["content"] == original["content"]
    assert rows[0]["codex_delivery"]["state"] == "accepted"
    assert test_rust_queue_continuation.saved_entry(app)["item"] == original
    assert {c[0] for c in calls} == ({"start", "thread/read", "thread/turns/list"} if paginated else {"start", "thread/read"})
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, command)
    assert app._resume(app.convo_path, startup=True)
    assert not app._pending_input


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["absent", "turn", "client", "type", "duplicate"])
async def test_missing_positive_proof_never_clears_or_rewrites_hold(saved_app, monkeypatch, fault):
    app = saved_app
    command, _, events, _ = setup(app, monkeypatch, fault=fault)
    before = app.convo_path.read_bytes()
    result = await gui_rpc.async_dispatch(app, command)
    assert result["reconciled"] is False and events == []
    assert len(app._pending_input) == 1 and restored_input.held(app._pending_input[0])
    assert app.convo_path.read_bytes() == before
    assert restored_input.inspect(app, app.convo_id)["snapshot"] == command["snapshot"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["thread", "network", "budget"])
async def test_failed_history_read_is_not_acceptance(saved_app, monkeypatch, fault):
    app = saved_app
    command, _, events, _ = setup(app, monkeypatch, fault=fault)
    before = app.convo_path.read_bytes()
    with pytest.raises((ValueError, OSError, ProviderError)):
        await gui_rpc.async_dispatch(app, command)
    assert app.convo_path.read_bytes() == before
    assert restored_input.status(app)["held"] == 1 and not events
    assert not getattr(app, "_restored_input_error", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stale", "queued", "next_turn", "busy", "maintenance", "session"])
async def test_ineligible_or_stale_request_never_queries_provider(saved_app, monkeypatch, fault):
    app = saved_app
    command, calls, _, _ = setup(app, monkeypatch, state=fault if fault in ("queued", "next_turn") else "uncertain")
    if fault == "stale":
        command["snapshot"] = "old"
    elif fault == "session":
        command["session_id"] = "foreign"
    elif fault == "busy":
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    elif fault == "maintenance":
        app._mcp_maintenance = True
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, command)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["queue", "conversation", "model", "cancel"])
async def test_suspended_history_read_revalidates_and_blocks_competing_commands(saved_app, monkeypatch, fault):
    app = saved_app
    command, _, _, _ = setup(app, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.backend.app_server.request
    async def request(method, params):
        entered.set()
        await release.wait()
        return await original(method, params)
    monkeypatch.setattr(app.backend.app_server, "request", request)
    pending = asyncio.create_task(gui_rpc.async_dispatch(app, command))
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(ValueError, match="management"):
        await gui_rpc.async_dispatch(app, command)
    before = app.convo_path.read_bytes()
    if fault == "cancel":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        if fault == "queue":
            app._pending_input[0]["_codex_entry"]["item"]["content"] = "changed"
        elif fault == "conversation":
            app.conversation = list(app.conversation)
        else:
            app.model_id = "changed"
        release.set()
        with pytest.raises(ValueError):
            await pending
    assert app.convo_path.read_bytes() == before
    assert not app._gui_management_busy


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["claim", "append"])
async def test_failed_durable_recording_retains_uncertainty(saved_app, monkeypatch, phase):
    app = saved_app
    command, _, _, _ = setup(app, monkeypatch)
    if phase == "claim":
        monkeypatch.setattr(codex_steering, "save_at", lambda *_: None)
    else:
        monkeypatch.setattr(app, "_append", lambda *_: None)
    with pytest.raises(OSError):
        await gui_rpc.async_dispatch(app, command)
    assert app._restored_input_error and len(app._pending_input) == 1
    with pytest.raises(ValueError):
        restored_input.inspect(app, app.convo_id)


@pytest.mark.asyncio
async def test_history_request_budget_and_method_guard():
    calls = []
    async def request(method, params):
        calls.append(method)
        return {}
    reader = restored_input._HistoryReader(NS(request=request))
    with pytest.raises(ValueError):
        await reader.request("turn/start", {})
    assert calls == []
    for _ in range(100):
        await reader.request("thread/read", {})
    with pytest.raises(ValueError):
        await reader.request("thread/read", {})
    assert len(calls) == 100
