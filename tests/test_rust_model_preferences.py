"""Current-model preferences through registered source operations and owned storage."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace

import pytest
from test_rust_context_reader import app, armed, switch_app

from litetui import background_input as work
from litetui import gui_rpc, settings_runtime
from litetui import native_model_preferences as models
from litetui.settings_service import SettingsService

pytestmark = pytest.mark.asyncio
__all__ = ["app", "armed", "switch_app"]


def command(owner, action, **fields):
    return {
        "type": models.PREFIX + action,
        "id": "preferences-" + action,
        "version": 1,
        **work.identity(owner),
        "work_instance": work.bridge(owner).instance,
        **fields,
    }


async def call(owner, action, **fields):
    cmd = command(owner, action, **fields)
    result = await gui_rpc.async_dispatch(owner, cmd)
    if action in ("apply", "readback"):
        from litetui import rpc

        rpc.publish_management_result(owner, cmd, result)
    return result


async def review(owner, field="temperature", value=0.35, mode="value"):
    current = await call(owner, "snapshot")
    return await call(
        owner,
        "review",
        revision=current["revision"],
        field=field,
        mode=mode,
        value=value,
    )


@pytest.mark.parametrize(
    "field,value", [("temperature", 0.35), ("top_p", 0.8), ("max_tokens", 123)]
)
async def test_review_saves_only_current_conversation_and_drives_request(
    armed, field, value
):
    owner = armed
    original = owner.convo_path.read_bytes()
    global_path = owner._agent_session.store.data_root / "settings.json"
    global_before = global_path.read_bytes()
    before = copy.deepcopy(owner.settings.model_infer_overrides)
    preview = await review(owner, field, value)
    assert owner.convo_path.read_bytes() == original
    assert owner.settings.model_infer_overrides == before
    applied = await call(owner, "apply", token=preview["token"])
    with pytest.raises(ValueError, match="consumed"):
        await call(owner, "apply", token=preview["token"])
    result = await call(
        owner, "readback", token=preview["token"], apply_id=applied["apply_id"]
    )
    assert result["kind"] == "model_preferences_saved"
    row = result["preferences"]["fields"][field]
    assert (
        row["present"] and row["saved"] == row["active"] == row["next_request"] == value
    )
    saved = settings_runtime.service_for(owner).snapshot(owner.convo_id, read_only=True)
    assert saved.saved.model_infer_overrides[owner.model_id][field] == value
    assert global_path.read_bytes() == global_before
    assert owner.convo_path.read_bytes() == original
    assert owner._effective_request_overrides()[field] == value
    with pytest.raises(ValueError, match="again"):
        await call(
            owner, "readback", token=preview["token"], apply_id=applied["apply_id"]
        )


@pytest.mark.parametrize("prior", [None, 0.2])
async def test_inherit_removes_one_entry_and_preserves_other_models(armed, prior):
    owner = armed
    candidate = copy.deepcopy(owner.settings)
    candidate.temperature = 0.7
    candidate.model_infer_overrides = {
        owner.model_id: {"temperature": prior, "top_p": 0.85},
        "unselected": {"temperature": 0.6},
    }
    settings_runtime.persist_or_raise(owner, candidate)
    owner.settings = candidate
    owner.backend.set_settings(candidate)
    r = await review(owner, "temperature", None, "inherit")
    a = await call(owner, "apply", token=r["token"])
    saved = await call(owner, "readback", token=r["token"], apply_id=a["apply_id"])
    assert saved["preferences"]["fields"]["temperature"] == {
        "present": False,
        "saved": None,
        "active_present": False,
        "active": None,
        "next_request": 0.7,
        "inherited": 0.7,
        "editable": True,
        "overridden": False,
        "server_default": None,
    }
    assert owner.settings.model_infer_overrides == {
        owner.model_id: {"top_p": 0.85},
        "unselected": {"temperature": 0.6},
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_tokens", 0),
        ("max_tokens", True),
        ("temperature", float("nan")),
        ("top_p", "0.5"),
    ],
)
async def test_malformed_values_never_create_review(armed, field, value):
    before = copy.deepcopy(armed.settings.model_infer_overrides)
    with pytest.raises((ValueError, TypeError)):
        await review(armed, field, value)
    assert armed.settings.model_infer_overrides == before
    assert getattr(armed, "_native_model_preferences_review", None) is None


async def test_invocation_override_is_visible_and_uneditable(armed):
    armed._launch_options = replace(armed._launch_options, max_tokens=91)
    snap = await call(armed, "snapshot")
    field = snap["fields"]["max_tokens"]
    assert field["overridden"] and not field["editable"] and field["next_request"] == 91
    with pytest.raises(ValueError, match="changed"):
        await call(
            armed,
            "review",
            revision=snap["revision"],
            field="max_tokens",
            mode="value",
            value=92,
        )


async def test_changed_settings_consume_review_before_writing(armed):
    r = await review(armed)
    path = armed._agent_session.store.data_root / "settings.json"
    document = json.loads(path.read_bytes())
    document["theme_name"] = "external-change"
    path.write_text(json.dumps(document), encoding="utf8")
    original = copy.deepcopy(armed.settings.model_infer_overrides)
    with pytest.raises(ValueError, match="changed"):
        await call(armed, "apply", token=r["token"])
    assert armed.settings.model_infer_overrides == original
    with pytest.raises(ValueError, match="again"):
        await call(armed, "apply", token=r["token"])


async def test_partial_publication_closes_admission_and_cannot_replay(
    armed, monkeypatch
):
    original_save = SettingsService.save_patch

    def partial(service, *args, **kwargs):
        original_save(service, *args, **kwargs)
        raise OSError("controlled failure after persistence")

    r = await review(armed)
    monkeypatch.setattr(SettingsService, "save_patch", partial)
    with pytest.raises(ValueError, match="uncertain"):
        await call(armed, "apply", token=r["token"])
    assert armed._owned_launch_error
    disk = settings_runtime.service_for(armed).snapshot(armed.convo_id, read_only=True)
    assert disk.saved.model_infer_overrides[armed.model_id]["temperature"] == 0.35
    assert getattr(armed, "_native_model_preferences_review", None) is None


async def test_cancel_during_apply_inspection_consumes_attempt_without_write(
    armed, monkeypatch
):
    r = await review(armed)
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def held(_model):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    monkeypatch.setattr(armed.backend, "model_info", held)
    old = copy.deepcopy(armed.settings.model_infer_overrides)
    task = asyncio.create_task(call(armed, "apply", token=r["token"]))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set() and armed.settings.model_infer_overrides == old
    assert getattr(armed, "_native_model_preferences_review", None) is None


async def test_reported_residency_is_separate_from_selected_identity(
    armed, monkeypatch
):
    async def unloaded(_model):
        return 8192, "llm", False

    monkeypatch.setattr(armed.backend, "model_info", unloaded)
    snap = await call(armed, "snapshot")
    assert snap["model"] == armed.model_id and snap["reported_loaded"] is False
    assert (
        snap["context_length"] == 8192 and snap["context_source"] == "client_declared"
    )


async def test_wrong_work_binding_has_no_read_or_write(armed, monkeypatch):
    async def forbidden(_model):
        pytest.fail("Foreign identity reached the backend")

    monkeypatch.setattr(armed.backend, "model_info", forbidden)
    bad = command(armed, "snapshot")
    bad["work_instance"] = "retired"
    with pytest.raises(ValueError, match="another"):
        await gui_rpc.async_dispatch(armed, bad)


VALUES = {
    "temperature": 0.42,
    "top_p": 0.81,
    "max_tokens": 423,
    "top_k": 41,
    "min_p": 0.06,
    "repeat_penalty": 1.12,
    "presence_penalty": -0.4,
    "frequency_penalty": 0.25,
    "stop": ["END", "STOP"],
    "reasoning_effort": "off",
    "enable_thinking": False,
    "json_schema": '{"type":"object","properties":{"ok":{"type":"boolean"}}}',
    "ctx": 4096,
    "ngl": -1,
    "threads": 8,
    "batch": 512,
    "ubatch": 128,
    "parallel": 2,
    "flash_attn": "auto",
    "cache_k": "q8_0",
    "cache_v": "f16",
    "kv_offload": False,
    "kv_unified": True,
    "mlock": False,
    "mmap": True,
    "context_shift": False,
    "seed": -1,
    "rope_base": 10000.0,
    "rope_scale": 1.5,
    "spec_type": "none",
    "draft_model": "later-draft.gguf",
    "draft_max": 4,
    "draft_min": 0,
    "draft_p_min": 0.4,
    "mmproj": "none",
    "chat_template_file": "template.jinja",
}


async def local_owner(app, monkeypatch, backend="llamacpp"):
    from dataclasses import replace

    from litetui import llm_backend

    monkeypatch.setattr(app.backend, "name", backend)
    app._agent_session.update_execution(
        backend=backend, model=app.model_id, thinking_level="default"
    )
    monkeypatch.setattr(
        llm_backend,
        "configured_flags",
        lambda _settings: frozenset(llm_backend.FLAG_FOR.values()) | {"jinja"},
    )
    monkeypatch.setattr(
        llm_backend,
        "configured_spec_types",
        lambda _settings: frozenset(("none", "draft-simple", "draft-mtp")),
    )
    monkeypatch.setattr(llm_backend, "gguf_metadata", lambda _path: ({}, 2))
    app.model_rows[app.model_id] = replace(
        app.model_rows[app.model_id], path="controlled.gguf", nextn_predict_layers=2
    )
    app._cli_effective_thinking = None
    app._launch_options.work_lifecycle_version = 1
    capture = work.capture(app, {"version": 1, "id": "capture", **work.identity(app)})
    work.activate(
        app,
        {
            "version": 1,
            "id": "activate",
            **work.identity(app),
            "instance": capture["instance"],
            "activation_token": capture["activation_token"],
        },
    )
    return app


@pytest.mark.parametrize("field", [key for key in VALUES if key != "reasoning_effort"])
async def test_every_source_screen_field_saves_with_exact_active_and_raw_readback(
    app, monkeypatch, field
):
    from litetui import llm_backend
    from litetui.native_model_preference_catalog import INFERENCE, LOAD, wire_value

    owner = await local_owner(app, monkeypatch)
    for name in ("load", "unload", "apply_load_settings", "ensure_running"):

        def forbidden(*_args, **_kwargs):
            pytest.fail("Preference saving reached model lifecycle")

        monkeypatch.setattr(owner.backend, name, forbidden, raising=False)
    candidate = copy.deepcopy(owner.settings)
    candidate.model_infer_overrides = {
        owner.model_id: {"future-infer": {"keep": [1, None]}},
        "other": {"top_k": 99},
    }
    candidate.llama_load_settings = {
        owner.model_id: {"future-load": {"keep": [False, 2]}},
        "other": {"ctx": 8192},
    }
    settings_runtime.persist_or_raise(owner, candidate)
    owner.settings = candidate
    owner.backend.set_settings(candidate)
    initial = await call(owner, "snapshot")
    original = copy.deepcopy(owner.settings.llama_load_settings)
    r = await review(owner, field, VALUES[field])
    assert not owner._native_model_preferences_review.applied
    a = await call(owner, "apply", token=r["token"])
    assert owner._native_model_preferences_hold
    result = await call(owner, "readback", token=r["token"], apply_id=a["apply_id"])
    assert not owner._native_model_preferences_hold
    snap = result["preferences"]
    group = "inference" if field in INFERENCE else "load"
    assert snap["fields"][field]["saved"] == VALUES[field]
    assert snap["raw"][group]["saved"][field] == VALUES[field]
    assert snap["raw"]["inference"]["saved"]["future-infer"] == {"keep": [1, None]}
    assert snap["raw"]["load"]["saved"]["future-load"] == {"keep": [False, 2]}
    saved = (
        settings_runtime.service_for(owner)
        .snapshot(owner.convo_id, read_only=True)
        .saved
    )
    assert saved.model_infer_overrides["other"] == {"top_k": 99}
    assert saved.llama_load_settings["other"] == {"ctx": 8192}
    if field in LOAD:
        assert owner.settings.llama_load_settings == original
        assert snap["raw"]["load"]["active"] == initial["raw"]["load"]["active"]
        assert (
            snap["fields"][field]["next_request"]
            == initial["fields"][field]["next_request"]
        )
    else:
        request = models._request_values(owner, owner._effective_request_overrides())
        assert request[field] == wire_value(
            field, VALUES[field], snap["catalog"][field]
        )
        assert snap["fields"][field]["active"] == VALUES[field]
    assert set(INFERENCE) | set(LOAD) == set(VALUES) == set(snap["catalog"])
    assert set(llm_backend.FLAG_FOR).issuperset(LOAD)


async def test_pending_load_edits_compose_from_saved_values_and_inherit_preserves_active(
    app, monkeypatch
):
    owner = await local_owner(app, monkeypatch)
    for field, value in [("ctx", 4096), ("threads", 7), ("ctx", None)]:
        r = await review(owner, field, value, "inherit" if value is None else "value")
        a = await call(owner, "apply", token=r["token"])
        final = await call(owner, "readback", token=r["token"], apply_id=a["apply_id"])
    assert final["preferences"]["raw"]["load"]["saved"] == {"threads": 7}
    assert final["preferences"]["raw"]["load"]["active"] == {}
    assert owner.settings.llama_load_settings == {}


async def test_explicit_null_is_not_inherit_and_uses_constructor_defaults(armed):
    candidate = copy.deepcopy(armed.settings)
    candidate.temperature = 0.75
    settings_runtime.persist_or_raise(armed, candidate)
    armed.settings = candidate
    armed.backend.set_settings(candidate)
    r = await review(armed, "temperature", None, "server")
    a = await call(armed, "apply", token=r["token"])
    final = await call(armed, "readback", token=r["token"], apply_id=a["apply_id"])
    row = final["preferences"]["fields"]["temperature"]
    assert row["present"] and row["saved"] is None and row["next_request"] is None
    assert row["inherited"] == 0.75
    assert "temperature" not in armed._effective_request_overrides()


@pytest.mark.parametrize(
    "field,value",
    [
        ("ctx", 511),
        ("ctx", 1048577),
        ("ngl", True),
        ("kv_offload", 1),
        ("stop", "END"),
        ("reasoning_effort", "bogus"),
        ("json_schema", "{"),
        ("cache_k", "bogus"),
        ("flash_attn", "true"),
    ],
)
async def test_full_catalog_types_and_source_bounds_refuse_before_write(
    app, monkeypatch, field, value
):
    owner = await local_owner(app, monkeypatch)
    original = owner.convo_path.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        await review(owner, field, value)
    assert owner.convo_path.read_bytes() == original
    assert owner._native_model_preferences_review is None


async def test_lmstudio_only_exposes_context_load_and_custom_none(app, monkeypatch):
    owner = await local_owner(app, monkeypatch, "lmstudio")
    snap = await call(owner, "snapshot")
    assert [
        key for key in models.vocabulary.LOAD if snap["fields"][key]["editable"]
    ] == ["ctx"]
    assert not snap["fields"]["enable_thinking"]["editable"]
    with pytest.raises(ValueError, match="changed"):
        await review(owner, "threads", 3)


async def test_speculation_capability_drift_and_projector_disappearance_refuse(
    app, monkeypatch, tmp_path
):
    from litetui import llm_backend

    owner = await local_owner(app, monkeypatch)
    r = await review(owner, "spec_type", "draft-mtp")
    monkeypatch.setattr(
        llm_backend, "configured_spec_types", lambda _settings: frozenset(("none",))
    )
    with pytest.raises(ValueError, match="changed"):
        await call(owner, "apply", token=r["token"])
    path = tmp_path / "projector.gguf"
    path.write_bytes(b"fixture")
    r = await review(owner, "mmproj", str(path))
    path.unlink()
    with pytest.raises(ValueError, match="refused"):
        await call(owner, "apply", token=r["token"])
    assert not owner._native_model_preferences_hold


@pytest.mark.parametrize("phase", ["apply", "readback"])
@pytest.mark.parametrize("failure", [None, "write", "flush"])
async def test_preference_publication_exact_real_output_barrier(
    armed, monkeypatch, phase, failure
):
    from test_rpc_publication_sink import publish

    from litetui import native_runtime_controls

    r = await review(armed)
    cmd = command(armed, "apply", token=r["token"])
    result = await gui_rpc.async_dispatch(armed, cmd)
    if phase == "readback":
        publish(armed, cmd, result, monkeypatch, None)
        cmd = command(armed, "readback", token=r["token"], apply_id=result["apply_id"])
        result = await gui_rpc.async_dispatch(armed, cmd)
    publish(armed, cmd, result, monkeypatch, failure)
    assert bool(native_runtime_controls.mutation_hold(armed)) == (
        phase == "apply" or failure is not None
    )
    if failure:
        retained = armed._native_model_preferences_review
        assert retained.publication is not None and not retained.publication.used
        with pytest.raises(ValueError):
            models.prepare_publication(armed, cmd, result)
        assert armed._native_model_preferences_hold
        await work.settle_before_shutdown(armed)
        assert retained.publication is not None and not retained.publication.used
        assert armed._native_model_preferences_hold


async def test_readback_raw_unknown_key_drift_retains_admission(armed):
    r = await review(armed)
    a = await call(armed, "apply", token=r["token"])
    armed.settings.model_infer_overrides[armed.model_id]["unknown"] = 99
    with pytest.raises(ValueError, match="changed"):
        await call(armed, "readback", token=r["token"], apply_id=a["apply_id"])
    assert armed._native_model_preferences_hold and armed._owned_launch_error


async def test_invocation_thinking_override_cannot_be_silently_saved(armed):
    assert armed._cli_effective_thinking == "default"
    snap = await call(armed, "snapshot")
    assert snap["fields"]["reasoning_effort"]["overridden"]
    assert not snap["fields"]["reasoning_effort"]["editable"]
    with pytest.raises(ValueError, match="changed"):
        await review(armed, "reasoning_effort", "off")


async def test_per_model_effort_is_honestly_readonly_until_authority_transition_exists(
    app, monkeypatch
):
    owner = await local_owner(app, monkeypatch)
    before = owner._agent_session.authority
    snap = await call(owner, "snapshot")
    assert not snap["fields"]["reasoning_effort"]["editable"]
    assert "execution-authority" in snap["catalog"]["reasoning_effort"]["reason"]
    assert snap["catalog"]["reasoning_effort"]["choices"] == [
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    with pytest.raises(ValueError, match="changed"):
        await review(owner, "reasoning_effort", "off")
    assert owner._agent_session.authority == before


async def test_canonical_model_screen_effort_save_reproduces_authority_gap(
    app, monkeypatch
):
    from types import SimpleNamespace

    from litetui.plugins import model_switch

    app._cli_effective_thinking = None
    before = app._agent_session.authority
    widgets = {
        "#inf-reasoning_effort": SimpleNamespace(value="off"),
        "#mc-preset-apply": SimpleNamespace(value=""),
        "#ld-spec_type": SimpleNamespace(value=""),
        "#mc-json-schema": SimpleNamespace(value=""),
        "#mc-preset-name": SimpleNamespace(value=""),
    }
    body = SimpleNamespace(
        app=app,
        _key=app.model_id,
        query_one=lambda key, *_: widgets[key],
        _load_cfg=dict,
        _infer_cfg=dict,
        _say_error=lambda text: pytest.fail(text),
    )
    body._collect_group = lambda prefix, fields, base: (
        model_switch.ModelConfigBody._collect_group(body, prefix, fields, base)
    )
    monkeypatch.setattr(model_switch, "close_dialog", lambda *_: None)
    monkeypatch.setattr(app, "system_message", lambda *_: None)
    model_switch.ModelConfigBody.action_apply(body)
    assert app.settings.model_infer_overrides[app.model_id]["reasoning_effort"] == "off"
    assert app._agent_session.authority == before and before.thinking_level == "default"
    assert (
        models._request_values(app, app._effective_request_overrides())[
            "reasoning_effort"
        ]
        == "none"
    )
    with pytest.raises(RuntimeError, match="Effective request effort disagrees"):
        app._validate_owned_identity()


@pytest.mark.parametrize("change", ["history", "policy", "launch"])
async def test_review_pins_canonical_execution_context_before_save(armed, change):
    r = await review(armed)
    before = copy.deepcopy(armed.settings.model_infer_overrides)
    if change == "history":
        armed.conversation.append({"role": "assistant", "content": "changed"})
    elif change == "policy":
        armed._active_tool_profile = "interactive"
    else:
        armed._launch_options = replace(armed._launch_options, max_tokens=91)
    with pytest.raises(ValueError, match="changed"):
        await call(armed, "apply", token=r["token"])
    assert armed.settings.model_infer_overrides == before
