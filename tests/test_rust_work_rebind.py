"""Actual owned identity changes retire captured work before fresh admission."""
import copy
import inspect

import pytest
from test_rust_backend_transition import command as backend_command
from test_rust_background_input import app as _owned_app
from test_rust_background_input import armed as _armed
from test_rust_background_input import command
from test_rust_background_input import switch_app as _switch_app
from test_rust_conversation_transition import ENTER, LEAVE, reviewed

from litetui import background_input as work
from litetui import conversation_page, gui_rpc, hook_host, native_history
from litetui.plugins import model_switch

app = _owned_app
armed = _armed
switch_app = _switch_app

pytestmark = pytest.mark.asyncio


def recapture(app, old, receipt):
    assert receipt["work_recapture_required"] is True
    proof = receipt["work_shutdown"]
    assert proof == old.transition_proof
    assert proof["instance"] == old.instance and proof["worker_settled"] and proof["callback_settled"]
    assert work.bridge(app) is None
    assert work.prompt_refusal(app)
    descriptor = work.capture(app, command(app))
    assert descriptor["instance"] != old.instance
    assert work.prompt_refusal(app)
    active = work.activate(app, command(app, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    assert active["identity"] == work.identity(app)
    assert work.prompt_refusal(app) is None
    return descriptor


def model_effects(app, monkeypatch):
    app.available_models = ["old-model", "new-model"]
    effects = []
    monkeypatch.setattr(app, "system_message", lambda *_: None)
    for name in ("update_header", "fetch_context_window", "apply_context_length"):
        monkeypatch.setattr(app, name, lambda name=name: effects.append(name))
    return effects


async def test_model_switch_retains_effects_and_blocks_admission_until_recapture(armed, monkeypatch):
    effects = model_effects(armed, monkeypatch)
    old = work.bridge(armed)
    history = copy.deepcopy(armed.conversation)
    result = await gui_rpc.async_dispatch(armed, {"type":"gui.models.select", "id":"model-change", "slug":"new-model"})
    assert armed.model_id == armed._agent_session.authority.model == "new-model"
    assert effects == ["update_header", "fetch_context_window", "apply_context_length"]
    assert armed.conversation == history
    recapture(armed, old, result)


async def test_model_reselection_has_no_epoch_retirement(armed, monkeypatch):
    effects = model_effects(armed, monkeypatch)
    old = work.bridge(armed)
    result = await gui_rpc.async_dispatch(armed, {"type":"gui.models.select", "slug":"old-model"})
    assert work.bridge(armed) is old and not old.closing and not effects
    assert "work_shutdown" not in result


@pytest.mark.parametrize("fault", ["unknown", "pending"])
async def test_model_preflight_refuses_before_epoch_close(armed, monkeypatch, fault):
    effects = model_effects(armed, monkeypatch)
    old = work.bridge(armed)
    if fault == "pending":
        armed._pending_input.append({"content":"held", "_restored_input_held":True})
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(armed, {"type":"gui.models.select", "slug":"missing" if fault == "unknown" else "new-model"})
    assert work.bridge(armed) is old and not old.closing and not effects
    assert armed.model_id == "old-model"
    armed._pending_input.clear()


async def test_sync_model_entry_cannot_bypass_awaited_epoch_retirement(armed, monkeypatch):
    effects = model_effects(armed, monkeypatch)
    with pytest.raises(ValueError, match="awaited identity"):
        model_switch.switch_model(armed, "new-model")
    assert armed.model_id == "old-model" and not effects and not work.bridge(armed).closing


async def test_backend_switch_retires_old_epoch_after_quiet_validation(armed):
    old = work.bridge(armed)
    result = await gui_rpc.async_dispatch(armed, backend_command(armed))
    assert result["status"] == "connected", result
    assert armed.backend.name == "lmstudio" and armed.model_id == "new-model"
    recapture(armed, old, result)


@pytest.mark.parametrize("action", ["new", "rename"])
async def test_reviewed_conversation_identity_changes_only_retire_new(armed, monkeypatch, action):
    monkeypatch.setattr(hook_host, "enter_conversation", ENTER)
    monkeypatch.setattr(hook_host, "leave_conversation", LEAVE)
    armed._hook_conversation_id = armed.convo_id
    old = work.bridge(armed)
    review = reviewed(armed, action)
    result = await gui_rpc.async_dispatch(armed, {**review, "type":"gui.conversations.native_apply"})
    assert result["status"] == ("created" if action == "new" else "renamed"), result
    if action == "new":
        recapture(armed, old, result)
    else:
        assert work.bridge(armed) is old and not old.closing and "work_shutdown" not in result


async def test_native_open_joins_real_connection_worker_before_recapture(armed, monkeypatch):
    from litetui.app import LiteTUI
    monkeypatch.setattr(hook_host, "enter_conversation", ENTER)
    monkeypatch.setattr(hook_host, "leave_conversation", LEAVE)
    armed._hook_conversation_id = armed.convo_id
    target = armed.convo_id
    old = work.bridge(armed)
    review = reviewed(armed, "new")
    result = await gui_rpc.async_dispatch(armed, {**review, "type":"gui.conversations.native_apply"})
    assert result["status"] == "created", result
    recapture(armed, old, result)
    old = work.bridge(armed)
    catalog = conversation_page._catalog(armed, armed.convo_id)[4]
    request = {"version":1, "from_session_id":armed.convo_id, "session_id":target,
        "agent_id":armed._agent_session.authority.agent_id, "backend":armed.backend.name,
        "model":armed.model_id, "scope":"preview", "catalog_snapshot":catalog}
    descriptor = native_history.snapshot(armed, request)
    async def context_read():
        return None
    monkeypatch.setattr(armed, "_fetch_ctx_window", lambda: armed.run_worker(context_read(), group="ctxload"))
    monkeypatch.setattr(armed, "connect", lambda: armed.run_worker(inspect.unwrap(LiteTUI.connect)(armed), group="init"))
    result = await gui_rpc.async_dispatch(armed, {**request, "type":"gui.conversations.native_open",
        "history_snapshot":descriptor["snapshot"], "action_id":descriptor["open_action_id"]})
    assert result["status"] == "connected" and result["worker_settled"], result
    assert armed.convo_id == target
    recapture(armed, old, result)


async def test_model_effect_failure_keeps_admission_closed_without_recapture(armed, monkeypatch):
    model_effects(armed, monkeypatch)
    old = work.bridge(armed)
    def fail():
        raise OSError("controlled explicit model effect failure")
    monkeypatch.setattr(armed, "apply_context_length", fail)
    with pytest.raises(OSError, match="controlled explicit"):
        await gui_rpc.async_dispatch(armed, {"type":"gui.models.select", "slug":"new-model"})
    assert work.bridge(armed) is old and old.closing
    assert armed.model_id == "new-model" and armed._backend_transition_error
    assert work.prompt_refusal(armed)
    with pytest.raises(ValueError):
        work.capture(armed, command(armed))


async def test_legacy_open_preserves_joined_readback_and_recapture(armed, monkeypatch):
    from litetui.app import LiteTUI
    monkeypatch.setattr(hook_host, "enter_conversation", ENTER)
    monkeypatch.setattr(hook_host, "leave_conversation", LEAVE)
    armed._hook_conversation_id = armed.convo_id
    target = armed.convo_id
    old = work.bridge(armed)
    review = reviewed(armed, "new")
    created = await gui_rpc.async_dispatch(armed, {**review, "type":"gui.conversations.native_apply"})
    assert created["status"] == "created", created
    recapture(armed, old, created)
    old = work.bridge(armed)
    monkeypatch.setattr(armed, "_render_resumed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(armed, "connect", lambda: armed.run_worker(inspect.unwrap(LiteTUI.connect)(armed), group="init"))
    request = {"type":"gui.conversations.open", "session_id":target}
    with pytest.raises(ValueError, match="awaited identity"):
        gui_rpc.dispatch(armed, request)
    assert not old.closing
    result = await gui_rpc.async_dispatch(armed, request)
    assert result["connected"] is True and armed.convo_id == target
    assert result["messages"] == armed.conversation
    recapture(armed, old, result)
