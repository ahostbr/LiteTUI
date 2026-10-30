"""Prompt profile echoes retain launch authority through real owned RPC admission."""
from __future__ import annotations

import io
import json
import subprocess

import httpx
import pytest
import pytest_asyncio

from litetui import (
    gui_rpc,
    hook_host,
    rpc,
    seat_authority,
    settings,
    settings_runtime,
    tool_policy,
)
from litetui.conversation import ConversationRepository
from litetui.settings_service import SettingChange


def forbidden(*_args, **_kwargs):
    raise AssertionError("Profile echo tests must not invoke a provider or process")


@pytest_asyncio.fixture
async def echo_app(make_app, monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "controlled-host")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(hook_host, "enter_conversation", lambda *_: None)
    monkeypatch.setattr(hook_host, "leave_conversation", lambda *_: None)
    settings.save(settings.Settings(backend="custom", default_model="fixture-model",
                                    thinking_level="default", tools_enabled=False, mcp_enabled=False,
                                    skills_enabled=False, tool_policy_profile="interactive"))
    app = make_app(rpc=True, initial_backend="custom", initial_model="fixture-model",
                   initial_thinking="default", tool_profile="strict")
    app._system = lambda *_: None
    app._refresh_ctx_label = lambda: None
    app._sync_fleet_identity = lambda: None
    app._chat_running = lambda: False
    app._user_bubble = lambda *_args, **_kwargs: None
    app.watch_pending_image = lambda _: None
    app.pending_image = None
    app._scroll_down = lambda **_kwargs: None
    app._oversize_refusal = lambda _content: None
    monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: None)
    app._materialise_convo()
    assert app._agent_session.authority.thinking_level == "default"
    assert app._cli_tool_profile == app._active_tool_profile == "strict"
    wire, origins = io.StringIO(), []
    monkeypatch.setattr(rpc, "_real_stdout", wire)
    app._stream = lambda *, origin: origins.append(origin)
    try:
        yield app, wire, origins
    finally:
        await app.client.close()


def saved_settings(app):
    return {str(path): path.read_bytes()
            for path in app._agent_session.store.data_root.rglob("settings.json")}


def prompt(app, profile="strict"):
    rpc._dispatch(app, {"type": "prompt", "id": "echo-owned", "message": "Controlled prompt",
                        "tool_profile": profile})


def response(wire):
    return next(row for row in reversed([json.loads(line) for line in wire.getvalue().splitlines()])
                if row["type"] == "response")


@pytest.mark.asyncio
async def test_active_profile_echo_retains_launch_and_settings_but_still_admits_prompt(echo_app):
    app, wire, origins = echo_app
    before = saved_settings(app)
    prompt(app)
    assert response(wire) == {"type": "response", "id": "echo-owned", "ok": True,
                              "result": {"turn": "accepted"}}
    assert app._cli_tool_profile == app._active_tool_profile == "strict"
    assert gui_rpc._tool_policy(app)["tool_profile_requested"] == "strict"
    assert saved_settings(app) == before
    assert len(origins) == 1 and origins[0].source == "rpc"
    assert origins[0].operation_id == "echo-owned" and origins[0].session_id == app.convo_id
    messages = ConversationRepository.read(app.convo_path)[1]
    assert sum(message.get("content") == "Controlled prompt" for message in messages) == 1


@pytest.mark.asyncio
async def test_active_profile_echo_still_reaches_admission_refusal(echo_app, monkeypatch):
    app, wire, origins = echo_app
    before = saved_settings(app)
    transcript = app.convo_path.read_bytes()
    monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: "controlled floor refusal")
    prompt(app)
    assert response(wire)["ok"]  # Existing wire acknowledgment is not provider admission.
    assert origins == [] and app.convo_path.read_bytes() == transcript
    assert saved_settings(app) == before and app._cli_tool_profile == "strict"
    assert any(row.get("type") == "turn_refused" or row.get("stopReason") == "fleet_floor"
               for row in map(json.loads, wire.getvalue().splitlines()))


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_active", [False, True])
async def test_unknown_profile_including_corrupt_active_echo_uses_existing_refusal(echo_app, monkeypatch, corrupt_active):
    app, wire, origins = echo_app
    if corrupt_active:
        app._active_tool_profile = "unknown-profile"
    calls = []
    setter = app.set_tool_profile
    def record(profile, **kwargs):
        calls.append(profile)
        return setter(profile, **kwargs)
    monkeypatch.setattr(app, "set_tool_profile", record)
    before = saved_settings(app)
    prompt(app, "unknown-profile")
    assert not response(wire)["ok"] and "unknown" in response(wire)["error"]
    assert calls == ["unknown-profile"] and origins == []
    assert saved_settings(app) == before and app._cli_tool_profile == "strict"


@pytest.mark.asyncio
async def test_changed_profile_under_default_thinking_uses_setter_and_persists(echo_app):
    app, wire, origins = echo_app
    prompt(app, "autonomous")
    assert response(wire)["ok"] and len(origins) == 1
    assert app._cli_tool_profile is None and app._active_tool_profile == "autonomous"
    assert app.chosen_tool_profile == "autonomous"
    saved = settings_runtime.service_for(app).snapshot(app.convo_id).saved
    assert saved.tool_policy_profile == "autonomous" and saved.thinking_level == "default"
    assert app._agent_session.authority.thinking_level == "default"


@pytest.mark.asyncio
async def test_explicit_equal_profile_management_remains_a_persisted_user_choice(echo_app):
    app, wire, origins = echo_app
    rpc._dispatch(app, {"type": "set", "id": "profile-choice", "profile": "strict"})
    assert response(wire)["ok"] and response(wire)["result"]["changed"] == {"profile": "strict"}
    assert app._cli_tool_profile is None and app.chosen_tool_profile == "strict"
    assert origins == []
    assert settings_runtime.service_for(app).snapshot(app.convo_id).saved.tool_policy_profile == "strict"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["default", "off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
async def test_owned_settings_service_roundtrips_default_and_existing_thinking_levels(echo_app, level):
    app, _, _ = echo_app
    service = settings_runtime.service_for(app)
    before = service.snapshot(app.convo_id)
    result = service.save_patch(app.convo_id, (SettingChange("thinking_level", level, "conversation"),),
                                before.revisions)
    assert result.fully_saved
    after = service.snapshot(app.convo_id)
    assert after.saved.thinking_level == after.effective.thinking_level == level
    assert app._agent_session.authority.thinking_level == level
    raw = json.loads((app.convo_dir / "settings.json").read_text(encoding="utf-8"))
    assert raw["thinking_level"] == raw["execution"]["thinking_level"] == level


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["none", "banana"])
@pytest.mark.parametrize("field", ["thinking_level", "compact_thinking_level"])
async def test_owned_settings_service_still_rejects_invalid_thinking_without_writes(echo_app, level, field):
    app, _, _ = echo_app
    service = settings_runtime.service_for(app)
    snapshot = service.snapshot(app.convo_id)
    before = saved_settings(app)
    with pytest.raises(ValueError, match="expected one of"):
        service.save_patch(app.convo_id, (SettingChange(field, level, "conversation"),), snapshot.revisions)
    assert saved_settings(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", tuple(tool_policy.PROFILES))
async def test_every_canonical_active_echo_preserves_launch_choice(echo_app, monkeypatch, profile):
    app, wire, origins = echo_app
    app._active_tool_profile = app._cli_tool_profile = profile
    monkeypatch.setattr(app, "set_tool_profile", forbidden)
    before = saved_settings(app)
    prompt(app, profile)
    assert response(wire)["ok"] and len(origins) == 1
    assert saved_settings(app) == before and app._cli_tool_profile == profile


@pytest.mark.asyncio
@pytest.mark.parametrize("chosen,active,source", [
    ("autonomous", "strict", "harness"),
    ("strict", "autonomous", "scheduled"),
])
@pytest.mark.parametrize("refused", [False, True])
async def test_transient_profile_echo_must_select_requested_next_turn_authority(
        echo_app, monkeypatch, chosen, active, source, refused):
    app, wire, origins = echo_app
    # Real explicit choice and real producer admission recreate the mismatch:
    # narrowing mail or scheduled work changes the active execution profile,
    # while the next human/RPC turn still inherits the conversation's choice.
    assert app.set_tool_profile(chosen, announce=False)
    previous = hook_host.accept_prompt_origin(app, {
        "content": "Earlier producer input", "source": source, "tool_profile": active,
    })
    assert previous is not None and app._cli_tool_profile is None
    assert app._active_tool_profile == active and seat_authority.seat_profile(app) == chosen
    transcript = app.convo_path.read_bytes()
    calls = []
    setter = app.set_tool_profile
    def record(profile, **kwargs):
        calls.append(profile)
        return setter(profile, **kwargs)
    monkeypatch.setattr(app, "set_tool_profile", record)
    if refused:
        monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: "controlled floor refusal")
    prompt(app, active)
    assert response(wire)["ok"]
    assert calls == [active]
    assert app._active_tool_profile == app.chosen_tool_profile == seat_authority.seat_profile(app) == active
    assert settings_runtime.service_for(app).snapshot(app.convo_id).saved.tool_policy_profile == active
    if refused:
        assert origins == [] and app.convo_path.read_bytes() == transcript
    else:
        assert len(origins) == 1 and origins[0].source == "rpc"
        messages = ConversationRepository.read(app.convo_path)[1]
        assert sum(message.get("content") == "Controlled prompt" for message in messages) == 1


@pytest.mark.asyncio
async def test_aligned_active_and_chosen_profile_without_launch_flag_is_a_pure_echo(echo_app, monkeypatch):
    app, wire, origins = echo_app
    assert app.set_tool_profile("strict", announce=False)
    assert app._cli_tool_profile is None
    assert app._active_tool_profile == seat_authority.seat_profile(app) == "strict"
    before = saved_settings(app)
    monkeypatch.setattr(app, "set_tool_profile", forbidden)
    prompt(app)
    assert response(wire)["ok"] and len(origins) == 1
    assert saved_settings(app) == before and app._cli_tool_profile is None
