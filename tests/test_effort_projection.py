"""Owned current-model effort preserves base authority and exact publication."""
from __future__ import annotations

import copy

import pytest
from test_rust_context_reader import app, switch_app
from test_rust_model_preferences import call, local_owner, review

from litetui import settings_runtime

__all__ = ["app", "switch_app"]


@pytest.mark.asyncio
async def test_per_model_effort_preserves_base_across_reopen_and_model_switch(app, monkeypatch):
    from litetui import effort_projection

    owner = await local_owner(app, monkeypatch)
    owner.thinking_level = "medium"
    before = owner._agent_session.authority
    original_global = (owner._agent_session.store.data_root / "settings.json").read_bytes()
    r = await review(owner, "reasoning_effort", "off")
    a = await call(owner, "apply", token=r["token"])
    assert owner._native_model_preferences_hold
    await call(owner, "readback", token=r["token"], apply_id=a["apply_id"])
    owner._validate_owned_identity()
    assert owner._agent_session.authority == before
    assert owner.thinking_level == owner.settings.thinking_level == "medium"
    assert effort_projection.request_level(owner) == "off"
    assert (owner._agent_session.store.data_root / "settings.json").read_bytes() == original_global

    # Rehydration reads the durable selected model preference, never promoting it
    # into the agent's inherited thinking value.
    owner._adopt_convo_settings(False)
    owner._validate_owned_identity()
    assert owner.thinking_level == "medium"
    assert effort_projection.request_level(owner) == "off"
    first = owner.model_id
    owner.model_id = "different-model"
    owner._validate_owned_identity()
    assert effort_projection.request_level(owner) == "medium"
    owner.model_id = first
    owner._validate_owned_identity()
    assert effort_projection.request_level(owner) == "off"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("inherit", "medium"), ("server", None)])
async def test_absent_and_explicit_null_effort_are_distinct(app, monkeypatch, mode, expected):
    owner = await local_owner(app, monkeypatch)
    owner.thinking_level = "medium"
    r = await review(owner, "reasoning_effort", "off")
    a = await call(owner, "apply", token=r["token"])
    await call(owner, "readback", token=r["token"], apply_id=a["apply_id"])
    r = await review(owner, "reasoning_effort", None, mode)
    a = await call(owner, "apply", token=r["token"])
    final = await call(owner, "readback", token=r["token"], apply_id=a["apply_id"])
    owner._validate_owned_identity()
    row = final["preferences"]["fields"]["reasoning_effort"]
    assert row["next_request"] == expected
    assert row["present"] is (mode == "server")
    assert owner.thinking_level == "medium"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["live", "disk"])
async def test_effort_projection_refuses_unpublished_preference_drift(app, monkeypatch, change):
    from litetui import effort_projection

    owner = await local_owner(app, monkeypatch)
    owner.thinking_level = "medium"
    effort_projection.install(owner)
    if change == "live":
        owner.settings.model_infer_overrides.setdefault(owner.model_id, {})["reasoning_effort"] = "off"
    else:
        from litetui.settings_service import SettingChange
        service = settings_runtime.service_for(owner)
        before = service.snapshot(owner.convo_id, read_only=True)
        candidate = copy.deepcopy(before.saved.model_infer_overrides)
        candidate.setdefault(owner.model_id, {})["reasoning_effort"] = "off"
        result = service.save_patch(owner.convo_id,
            [SettingChange("model_infer_overrides", candidate, "conversation")], before.revisions)
        assert result.fully_saved
    with pytest.raises(RuntimeError, match="effort"):
        owner._validate_owned_identity()


@pytest.mark.asyncio
async def test_failed_effort_publication_never_reopens_admission(app, monkeypatch):
    from test_rust_model_preferences import command

    from litetui import gui_rpc, native_runtime_controls, rpc

    owner = await local_owner(app, monkeypatch)
    owner.thinking_level = "medium"
    r = await review(owner, "reasoning_effort", "off")
    cmd = command(owner, "apply", token=r["token"])
    result = await gui_rpc.async_dispatch(owner, cmd)

    def fail(_event):
        raise BrokenPipeError("controlled lost publication")

    monkeypatch.setattr(rpc, "rpc_emit", fail)
    with pytest.raises(BrokenPipeError):
        rpc.publish_management_result(owner, cmd, result)
    assert owner._native_model_preferences_hold
    assert native_runtime_controls.mutation_hold(owner)
    saved = settings_runtime.service_for(owner).snapshot(owner.convo_id, read_only=True)
    assert saved.saved.model_infer_overrides[owner.model_id]["reasoning_effort"] == "off"
    assert owner._agent_session.authority.thinking_level == "medium"


@pytest.mark.parametrize("explicit", [False, True])
def test_hydrated_authority_does_not_fabricate_invocation_pin(make_app, explicit):
    from litetui import settings
    from litetui.launch_options import LaunchOptions

    settings.save(settings.Settings(backend="custom", default_model="fixture",
                                    thinking_level="medium", mcp_enabled=False))
    owner = make_app(initial_backend="custom", initial_model="fixture", initial_thinking="medium",
                     launch_options=LaunchOptions(server_mode="connect", thinking_explicit=explicit))
    expected = "medium" if explicit else None
    assert owner._cli_thinking_level == expected
    assert owner._cli_effective_thinking == expected
    assert owner._owned_effort_invocation.level == expected
    assert owner._agent_session.authority.thinking_level == "medium"


@pytest.mark.parametrize("flag", [None, "--thinking-level", "--reasoning-effort"])
def test_real_cli_preserves_thinking_flag_provenance(tmp_path, monkeypatch, flag):
    import sys
    from types import SimpleNamespace

    from litetui import cli, paths, settings, shared_state

    seen = {}
    class App:
        def __init__(self, **kwargs):
            seen.update(kwargs)
        def run(self, **kwargs):
            pass

    settings.save(settings.Settings(backend="custom", default_model="fixture",
                                    thinking_level="medium", mcp_enabled=False))
    monkeypatch.setitem(sys.modules, "litetui.app", SimpleNamespace(
        LiteTUI=App, wants_ansi_fallback=lambda: False))
    monkeypatch.setitem(sys.modules, "litetui.image_viewer", SimpleNamespace(
        init_image_backend=lambda: None))
    monkeypatch.setattr(shared_state, "check_data_version", lambda root: None)
    from litetui.agent_launch_context import ordinary
    with ordinary(paths.data_root(), settings.load()):
        pass
    monkeypatch.setattr(sys, "argv", ["litetui", "--agent", "LiteTUI", *([flag, "medium"] if flag else [])])
    cli.main()
    assert seen["launch_options"].thinking_explicit is (flag is not None)
    assert seen["initial_thinking"] == "medium"


@pytest.mark.asyncio
async def test_settings_save_cannot_adopt_changed_invocation_pin(app, monkeypatch):
    from litetui import effort_projection

    assert app._cli_effective_thinking == "default"
    app._cli_effective_thinking = None
    with pytest.raises(RuntimeError, match="Invocation effort"):
        effort_projection.install(app)
    with pytest.raises(RuntimeError, match="Invocation effort"):
        app._validate_owned_identity()


@pytest.mark.parametrize("backend,level,expected", [
    ("custom", "high", "high"), ("llamacpp", "off", "none"),
    ("ninfer", "high", "xhigh"), ("strata", "xhigh", "high"),
    ("lmstudio", "high", None),
])
def test_request_projection_uses_normal_backend_wire_mapping(backend, level, expected):
    from types import SimpleNamespace

    from litetui import effort_projection
    from litetui.app import LiteTUI
    from litetui.llm_backend import _merged_overrides
    from litetui.native_model_preferences import _request_values
    from litetui.settings import Settings

    cfg = Settings(thinking_level="medium",
                   model_infer_overrides={"fixture": {"reasoning_effort": level}})
    owner = SimpleNamespace(settings=cfg, model_id="fixture", thinking_level="medium",
        tools_enabled=False, backend=SimpleNamespace(name=backend,
            request_overrides=lambda model: _merged_overrides(cfg, model)))
    assert effort_projection.request_level(owner) == level
    assert _request_values(owner, LiteTUI._effective_request_overrides(owner))["reasoning_effort"] == expected


@pytest.mark.parametrize("choice,present,value", [
    ("", False, None), ("__server_default__", True, None), ("off", True, "off")
])
def test_source_model_screen_collects_distinct_effort_choices(choice, present, value):
    from types import SimpleNamespace

    from textual.widgets import Select

    from litetui.plugins.model_switch import ModelConfigBody

    widget = Select([("inherit", ""), ("server", "__server_default__"), ("off", "off")],
                    value=choice, allow_blank=False)
    widget.set_reactive(Select.value, choice)
    body = SimpleNamespace(query_one=lambda *_: widget)
    result = ModelConfigBody._collect_group(
        body, "inf", [("reasoning_effort", "", "select")], {"reasoning_effort": "high"})
    assert ("reasoning_effort" in result) is present
    assert result.get("reasoning_effort") == value


@pytest.mark.asyncio
@pytest.mark.parametrize("level,present,wire", [("off", True, "none"), ("default", False, None)])
async def test_canonical_thinking_command_persists_its_per_model_update(app, monkeypatch, level, present, wire):
    from litetui.thinking_capabilities import set_thinking

    owner = await local_owner(app, monkeypatch)
    r = await review(owner, "reasoning_effort", "high")
    a = await call(owner, "apply", token=r["token"])
    await call(owner, "readback", token=r["token"], apply_id=a["apply_id"])
    set_thinking(owner, level)
    owner._validate_owned_identity()
    saved = settings_runtime.service_for(owner).snapshot(owner.convo_id, read_only=True)
    assert ("reasoning_effort" in saved.saved.model_infer_overrides[owner.model_id]) is present
    owner._adopt_convo_settings(False)
    owner._validate_owned_identity()
    snap = await call(owner, "snapshot")
    assert snap["fields"]["reasoning_effort"]["next_request"] == wire
