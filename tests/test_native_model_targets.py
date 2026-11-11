"""Named targets retain exact source publication and owned identity transitions."""

from __future__ import annotations

import copy
from dataclasses import asdict

import pytest
from test_rust_model_lifecycle import app, armed, call, command, lm_runtime, switch_app

from litetui import background_input as work
from litetui import convo_settings, gui_rpc, llm_backend, rpc

__all__ = ["app", "armed", "switch_app"]
pytestmark = pytest.mark.asyncio


def runtime(owner, monkeypatch):
    state = lm_runtime(owner, monkeypatch)
    first = owner.model_id
    state.update(resident={first: None, "other-model": None})

    def rows():
        return [
            {
                "id": key,
                "type": "llm",
                "max_context_length": 8192,
                "loaded_context_length": ctx,
            }
            for key, ctx in state["resident"].items()
        ]

    class Sdk:
        def load(self, key, config=None):
            assert key in state["resident"]
            state["calls"].append(("load", key, config))
            state["resident"][key] = config["contextLength"] if config else 4096

        def unload(self, key):
            assert key in state["resident"]
            state["calls"].append(("unload", key))
            state["resident"][key] = None

    async def allow(key):
        assert key in state["resident"]
        state["gates"] += 1
        return True

    monkeypatch.setattr(owner.backend, "_native_models", rows)
    monkeypatch.setattr(owner.backend, "_sdk", Sdk)
    owner.backend.vram_gate = allow
    return state


async def select(
    owner, action="load", target="other-model", mode="inherit", value=None
):
    fields = {"target": target, "context_mode": mode, "context_value": value}
    snap = await call(owner, "snapshot", **fields)
    r = await call(owner, "review", revision=snap["revision"], action=action, **fields)
    return snap, r


async def raw_call(owner, cmd):
    result = await gui_rpc.async_dispatch(owner, cmd)
    rpc.publish_management_result(owner, cmd, result)
    return result


def recapture(owner):
    c = {
        "type": "gui.work.native_capture",
        "id": "new-capture",
        "version": 1,
        **work.identity(owner),
    }
    captured = work.capture(owner, c)
    work.activate(
        owner,
        {
            **c,
            "type": "gui.work.native_activate",
            "instance": captured["instance"],
            "activation_token": captured["activation_token"],
        },
    )
    return captured


@pytest.mark.parametrize(
    "mode,value,want",
    [("inherit", None, 3072), ("default", None, None), ("value", 2048, 2048)],
)
async def test_named_load_adopts_only_after_success_and_exact_readback(
    app, monkeypatch, mode, value, want
):
    state = runtime(app, monkeypatch)
    first, bridge = app.model_id, work.bridge(app)
    app.settings.default_context_length = 3072
    app.settings.llama_load_settings["other-model"] = {"ctx": 1024}
    before_history = app.convo_path.read_bytes()
    before_authority = app._agent_session.authority
    snap, r = await select(app, mode=mode, value=value)
    assert snap["load_context"] == want and snap["preset_context"] == 1024
    assert state["calls"] == [] and app.model_id == first and work.bridge(app) is bridge
    apply = command(app, "apply", token=r["token"])
    readback = command(app, "readback", token=r["token"], apply_id=apply["id"])
    result = await raw_call(app, apply)
    assert result["work_recapture_required"] and bridge.closing
    assert app.model_id == "other-model" and work.bridge(app) is None
    assert app._agent_session.authority.model == "other-model"
    assert (
        app._agent_session.authority.thinking_level == before_authority.thinking_level
    )
    assert app._cli_initial_model is None and app._user_model_choice == "other-model"
    assert app._native_model_lifecycle_hold
    app._validate_owned_identity()
    assert app._owned_effort_projection.authority is app._agent_session.authority
    settled = await raw_call(app, readback)
    assert settled["state"]["selected_model"] == "other-model"
    assert settled["state"]["reported_loaded"] is True
    assert app.convo_path.read_bytes() == before_history
    assert convo_settings.load(app.convo_dir).lmstudio_load == (
        {"ctx": want} if want else {}
    )
    assert state["calls"] == [
        ("load", "other-model", {"contextLength": want} if want else None)
    ]
    assert not app._native_model_lifecycle_hold
    captured = recapture(app)
    assert captured["instance"] != bridge.instance
    app._validate_owned_execution()
    with pytest.raises(ValueError):
        await raw_call(app, readback)


async def test_other_unload_preserves_selection_invocation_authority_and_settings(
    app, monkeypatch
):
    state = runtime(app, monkeypatch)
    state["resident"]["other-model"] = 4096
    before = (
        app.model_id,
        app._cli_initial_model,
        getattr(app, "_user_model_choice", None),
        app._agent_session.authority,
        work.bridge(app),
        asdict(convo_settings.load(app.convo_dir)),
    )
    monkeypatch.setattr(
        app, "connect", lambda: pytest.fail("Other-target unload reconnected selection")
    )
    _, r = await select(app, action="unload")
    a = await call(app, "apply", token=r["token"])
    result = await call(app, "readback", token=r["token"], apply_id=a["apply_id"])
    assert "work_shutdown" not in a and result["state"]["reported_loaded"] is False
    assert before == (
        app.model_id,
        app._cli_initial_model,
        getattr(app, "_user_model_choice", None),
        app._agent_session.authority,
        work.bridge(app),
        asdict(convo_settings.load(app.convo_dir)),
    )
    assert state["calls"] == [("unload", "other-model")]


async def test_named_load_refusal_keeps_old_epoch_and_has_no_effect(app, monkeypatch):
    state = runtime(app, monkeypatch)
    before = (app.model_id, work.bridge(app), app._agent_session.authority)

    async def refuse(_key):
        return False

    app.backend.vram_gate = refuse
    _, r = await select(app)
    with pytest.raises(llm_backend.VramRefused):
        await call(app, "apply", token=r["token"])
    assert before == (app.model_id, work.bridge(app), app._agent_session.authority)
    assert not before[1].closing and state["calls"] == []
    assert not app._native_model_lifecycle_hold


@pytest.mark.parametrize("change", ["token", "instance", "operation", "identity"])
async def test_retained_origin_is_exact_and_never_general_read_authority(
    app, monkeypatch, change
):
    runtime(app, monkeypatch)
    _, r = await select(app)
    a = command(app, "apply", token=r["token"])
    readback = command(app, "readback", token=r["token"], apply_id=a["id"])
    await raw_call(app, a)
    bad = copy.deepcopy(readback)
    if change == "token":
        bad["token"] = "f" * 64
    elif change == "instance":
        bad["work_instance"] = "f" * 64
    elif change == "operation":
        bad["type"] = "gui.models.native_lifecycle_snapshot"
    else:
        bad["model"] = app.model_id
    with pytest.raises(ValueError):
        await raw_call(app, bad)
    assert app._native_model_lifecycle_hold


@pytest.mark.parametrize(
    "fields",
    [
        {"target": "missing"},
        {"context_mode": "value", "context_value": True},
        {"context_mode": "default", "context_value": 4096},
        {"context_mode": "invalid"},
    ],
)
async def test_invalid_or_missing_target_never_mutates(app, monkeypatch, fields):
    state = runtime(app, monkeypatch)
    choices = {"target": "other-model", "mode": "inherit", "value": None}
    choices.update(
        {
            {"context_mode": "mode", "context_value": "value"}.get(k, k): v
            for k, v in fields.items()
        }
    )
    with pytest.raises(ValueError):
        await select(app, **choices)
    assert state["calls"] == []


async def test_changed_inherited_context_consumes_review_without_loading(
    app, monkeypatch
):
    state = runtime(app, monkeypatch)
    _, r = await select(app)
    app.settings.default_context_length = 3072
    with pytest.raises(ValueError, match="changed after review"):
        await call(app, "apply", token=r["token"])
    assert state["calls"] == [] and not work.bridge(app).closing


async def test_other_load_partial_effect_is_uncertain_without_false_adoption(
    app, monkeypatch
):
    state = runtime(app, monkeypatch)
    original, bridge = app.model_id, work.bridge(app)

    class Broken:
        def load(self, key, config=None):
            state["resident"][key] = 4096
            state["calls"].append(("load", key))
            raise OSError("Controlled lost load reply")

    monkeypatch.setattr(app.backend, "_sdk", Broken)
    _, r = await select(app)
    with pytest.raises(ValueError, match="uncertain"):
        await call(app, "apply", token=r["token"])
    assert app.model_id == original and work.bridge(app) is bridge
    assert state["resident"]["other-model"] == 4096 and len(state["calls"]) == 1
    assert app._native_model_lifecycle_hold and app._backend_transition_error


async def test_named_load_exact_readback_publication_holds_admission(app, monkeypatch):
    runtime(app, monkeypatch)
    _, r = await select(app)
    apply = command(app, "apply", token=r["token"])
    readback = command(app, "readback", token=r["token"], apply_id=apply["id"])
    await raw_call(app, apply)
    settled = await gui_rpc.async_dispatch(app, readback)
    assert app._native_model_lifecycle_hold
    with pytest.raises(ValueError, match="foreign"):
        work.prepare_publication(app, readback, copy.deepcopy(settled))
    assert app._native_model_lifecycle_hold and app._backend_transition_error


async def test_late_typed_refusal_cannot_release_a_completed_load(app, monkeypatch):
    state = runtime(app, monkeypatch)

    def refused_connection():
        raise llm_backend.VramRefused("Controlled failure after model load")

    monkeypatch.setattr(app, "connect", refused_connection)
    _, r = await select(app)
    with pytest.raises(ValueError, match="uncertain"):
        await call(app, "apply", token=r["token"])
    assert state["resident"]["other-model"] == 4096
    assert app._native_model_lifecycle_hold and app._backend_transition_error
