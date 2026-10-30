"""Owned output-budget edits with process/network/hardware tripwires."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from test_rust_backend_transition import disk, forbidden
from test_rust_backend_transition import switch_app as _controlled_app

from litetui import gui_rpc, native_settings, settings_runtime, settings_service
from litetui.settings_apply import RuntimeSettingStatus, SettingsSaveResult
from litetui.turn_engine import TurnEngine

switch_app = _controlled_app


def request(app, field="max_tokens_chat", value=1234):
    state = native_settings.snapshot(app, {"version": 1, "session_id": app.convo_id})
    return {"version": 1, "session_id": app.convo_id, "agent_id": state["agent_id"],
            "snapshot": state["snapshot"], "revisions": state["revisions"], "field": field, "value": value}


def reviewed(app, **kwargs):
    cmd = request(app, **kwargs)
    return {**cmd, "review": native_settings.validate(app, cmd)["review"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("field,tools", [("max_tokens_chat", False), ("max_tokens_tools", True)])
async def test_review_save_changes_next_request_and_reopens_saved_budget_only(switch_app, field, tools):
    app = switch_app
    before = disk(app)
    history = copy.deepcopy(app.conversation)
    policy = gui_rpc._tool_policy(app)
    authority = app._agent_session.authority
    cmd = reviewed(app, field=field)
    assert disk(app) == before
    result = await gui_rpc.async_dispatch(app, {"type": "gui.settings.native_save", **cmd})
    assert result["status"] == "saved" and result["mutated"] and not result["close_required"], result
    assert result["saved"] == result["effective"] == 1234
    assert result["runtime"][0]["status"] == "applied"
    assert result["persistence"][0]["scope"] == "conversation"
    kwargs = TurnEngine.chat_request(model_id=app.model_id, messages=app.conversation, tools_enabled=tools,
                max_tokens_tools=app.settings.max_tokens_tools, max_tokens_chat=app.settings.max_tokens_chat,
                request_overrides=app._effective_request_overrides(), thinking_level=None)
    assert kwargs["max_tokens"] == 1234
    reopened = settings_service.SettingsService(app._agent_session.store.data_root,
                                                agent_session=app._agent_session).snapshot(app.convo_id, read_only=True)
    assert getattr(reopened.saved, field) == 1234
    assert result["revisions"] == reopened.revisions
    assert app._agent_session.authority == authority and app.conversation == history
    assert gui_rpc._tool_policy(app) == policy
    changed = {name for name, content in disk(app).items() if before.get(name) != content}
    assert changed == {str(app.convo_dir / "settings.json")}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", native_settings.FIELDS)
@pytest.mark.parametrize("value", [0, -1, True, False, None, "1234", 1.5, 2147483648])
async def test_invalid_drafts_never_write(switch_app, field, value):
    app = switch_app
    cmd = request(app, field, value)
    before = disk(app)
    with pytest.raises(ValueError, match="Output budget"):
        native_settings.validate(app, cmd)
    result = native_settings.save(app, {**cmd, "review": "invalid"})
    assert result["status"] == "refused" and not result["mutated"] and not result["close_required"]
    assert disk(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["version", "agent", "session", "snapshot", "revisions", "field", "review"])
async def test_guarded_save_rejects_stale_or_foreign_review(switch_app, fault):
    app = switch_app
    cmd = reviewed(app)
    cmd[fault] = {"version": True, "agent": "0" * 32, "session": "0" * 32,
                  "snapshot": "0" * 64, "revisions": {}, "field": "tools_enabled", "review": "0" * 64}[fault]
    if fault in {"agent", "session"}:
        cmd[fault + "_id"] = cmd.pop(fault)
    before = disk(app)
    result = native_settings.save(app, cmd)
    assert result["status"] == "refused" and not result["close_required"], result
    assert disk(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["launch", "model", "environment", "claude", "codex"])
async def test_higher_precedence_or_engine_owned_budgets_are_disabled(switch_app, monkeypatch, owner):
    app = switch_app
    if owner == "launch":
        from litetui.launch_options import LaunchOptions
        app._launch_options = LaunchOptions(max_tokens=777)
    elif owner == "model":
        app.settings.model_infer_overrides[app.model_id] = {"max_tokens": 777}
    elif owner == "environment":
        monkeypatch.setenv("LITETUI_MAX_TOKENS", "777")
    elif owner == "claude":
        app.backend.owns_native_turns = True
    else:
        app.backend.app_server = SimpleNamespace(runtime_activity=SimpleNamespace(
            uncertain=False, commands={}, requests={}, connected=True), process=None)
    before = disk(app)
    state = native_settings.snapshot(app, {"version": 1, "session_id": app.convo_id})
    fields = ("max_tokens_tools",) if owner == "environment" else native_settings.FIELDS
    for field in fields:
        assert not state["fields"][field]["editable"] and state["fields"][field]["reason"]
        with pytest.raises(ValueError):
            native_settings.validate(app, request(app, field))
    assert disk(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["history", "policy", "disk", "held", "busy"])
async def test_state_changes_invalidate_review_without_applying(switch_app, monkeypatch, fault):
    app = switch_app
    cmd = reviewed(app)
    if fault == "history":
        app.conversation.append({"role": "user", "content": "New input"})
    elif fault == "policy":
        app.tools_enabled = not app.tools_enabled
    elif fault == "disk":
        path = app.convo_dir / "settings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["unrelated_revision"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
    elif fault == "held":
        app._pending_input.append({"content": "Keep held"})
    else:
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    before = disk(app)
    result = native_settings.save(app, cmd)
    assert result["status"] == "refused" and not result["close_required"], result
    assert disk(app) == before


@pytest.mark.asyncio
async def test_idle_owned_engine_does_not_disable_budget_edit(switch_app):
    app = switch_app
    app.backend._owned = object()
    result = native_settings.save(app, reviewed(app))
    assert result["status"] == "saved", result


@pytest.mark.asyncio
async def test_known_persistence_refusal_preserves_old_runtime_and_is_retryable(switch_app, monkeypatch):
    app = switch_app
    cmd = reviewed(app)
    before = disk(app)
    monkeypatch.setattr(settings_service, "_write", lambda *_: (_ for _ in ()).throw(OSError("Controlled write refused")))
    result = native_settings.save(app, cmd)
    assert result["status"] == "refused" and not result["mutated"] and not result["close_required"], result
    assert result["persistence"][0]["saved"] is False
    assert disk(app) == before


@pytest.mark.asyncio
async def test_write_then_raise_latches_closed_without_claiming_rollback(switch_app, monkeypatch):
    app = switch_app
    cmd = reviewed(app)
    write = settings_service._write

    def write_then_raise(*args):
        write(*args)
        raise OSError("Controlled post-replace failure")

    monkeypatch.setattr(settings_service, "_write", write_then_raise)
    result = native_settings.save(app, cmd)
    assert result["status"] == "failed" and result["mutated"] and result["close_required"], result
    with pytest.raises(ValueError, match="close this runtime"):
        app._validate_owned_execution()


@pytest.mark.asyncio
async def test_known_runtime_failure_reports_saved_and_effective_separately(switch_app, monkeypatch):
    app = switch_app
    cmd = reviewed(app)
    old = app.settings.max_tokens_chat

    def fail_apply(_app, candidate, result):
        return SettingsSaveResult(result.persistence, (RuntimeSettingStatus("max_tokens_chat", "conversation",
                                  "failed", "retry", candidate.max_tokens_chat, old, "Controlled application failure"),))

    monkeypatch.setattr(settings_runtime, "apply_saved_result", fail_apply)
    result = native_settings.save(app, cmd)
    assert result["status"] == "partial" and not result["close_required"], result
    assert result["saved"] == 1234 and result["effective"] == old
    state = native_settings.snapshot(app, {"version": 1, "session_id": app.convo_id})
    assert state["fields"]["max_tokens_chat"]["saved"] == 1234
    assert state["fields"]["max_tokens_chat"]["effective"] == old


@pytest.mark.asyncio
async def test_unexpected_apply_exception_is_fatal(switch_app, monkeypatch):
    app = switch_app
    cmd = reviewed(app)
    monkeypatch.setattr(settings_runtime, "apply_saved_result", forbidden)
    result = native_settings.save(app, cmd)
    assert result["status"] == "failed" and result["close_required"], result
    assert result["persistence"][0]["saved"] is True


@pytest.mark.asyncio
async def test_read_only_review_and_backend_switch_never_migrate_legacy_defaults(switch_app):
    app = switch_app
    from litetui import backend_catalog, backend_transition
    catalog = backend_catalog.catalog(app, {"session_id": app.convo_id})
    path = app._agent_session.store.data_root / "settings.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["codex_subagent_model"] = "legacy-model"
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = disk(app)
    with pytest.raises(ValueError, match="Legacy defaults"):
        native_settings.snapshot(app, {"version": 1, "session_id": app.convo_id})
    catalog = backend_catalog.catalog(app, {"session_id": app.convo_id})
    result = await backend_transition.transition(app, {"version": 1, "session_id": app.convo_id,
             "agent_id": catalog["agent_id"], "snapshot": catalog["snapshot"], "name": "lmstudio", "model": "new-model"})
    assert result["status"] == "refused" and "Legacy defaults" in result["error"], result
    assert disk(app) == before
    # Existing ordinary service callers retain migration behavior.
    settings_runtime.service_for(app).snapshot(app.convo_id)
    assert "codex_subagent_model" not in json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
@pytest.mark.parametrize("materialization", ["absent", "legacy"])
async def test_post_review_legacy_defaults_cannot_migrate_during_conversation_save(switch_app, monkeypatch, materialization):
    app = switch_app
    conversation_path = app.convo_dir / "settings.json"
    if materialization == "absent":
        conversation_path.unlink()
    else:
        raw = json.loads(conversation_path.read_text(encoding="utf-8"))
        raw["execution"].pop("temperature")
        conversation_path.write_text(json.dumps(raw), encoding="utf-8")
    cmd = reviewed(app)
    original = settings_service.SettingsService.save_patch
    global_path = app._agent_session.store.data_root / "settings.json"
    injected = None

    def race(service, *args, **kwargs):
        nonlocal injected
        raw = json.loads(global_path.read_text(encoding="utf-8"))
        raw["codex_subagent_model"] = "concurrent-legacy"
        injected = json.dumps(raw).encode("utf-8")
        global_path.write_bytes(injected)
        return original(service, *args, **kwargs)

    before_conversation = conversation_path.read_bytes() if conversation_path.exists() else None
    monkeypatch.setattr(settings_service.SettingsService, "save_patch", race)
    result = native_settings.save(app, cmd)
    assert result["status"] == "failed" and result["close_required"], result
    assert global_path.read_bytes() == injected
    assert (conversation_path.read_bytes() if conversation_path.exists() else None) == before_conversation


@pytest.mark.asyncio
async def test_budget_review_with_missing_settings_does_not_materialize(switch_app):
    app = switch_app
    path = app.convo_dir / "settings.json"
    path.unlink()
    before = disk(app)
    cmd = reviewed(app)
    assert disk(app) == before and not path.exists()
    result = native_settings.save(app, cmd)
    assert result["status"] == "saved", result
    assert path.is_file()


@pytest.mark.asyncio
async def test_budget_save_before_first_prompt_keeps_pending_history_empty(switch_app, make_app):
    app = make_app(initial_backend="custom", initial_model="old-model", hold_restored_input=True)
    app._model_id = "old-model"
    app._chat_running = lambda: False
    app._rpc = True
    app._gui_rpc_enabled = True
    try:
        assert app.store.pending and not app.store.convo_path.exists()
        history = copy.deepcopy(app.conversation)
        before = disk(app)
        cmd = reviewed(app)
        assert disk(app) == before
        result = native_settings.save(app, cmd)
        assert result["status"] == "saved", result
        assert app.store.pending and not app.store.convo_path.exists()
        assert app.conversation == history and app.settings.max_tokens_chat == 1234
    finally:
        await app.client.close()
