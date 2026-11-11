"""Owned defaults and presets share model preference publication and admission."""

from __future__ import annotations

import copy
import json

import pytest
from test_rust_model_preferences import app, armed, call, local_owner, switch_app

from litetui import effort_projection, settings_runtime

pytestmark = pytest.mark.asyncio
__all__ = ["app", "armed", "switch_app"]

DEFAULTS = {
    "temperature": 0.31,
    "top_p": 0.82,
    "top_k": 29,
    "min_p": 0.04,
    "repeat_penalty": 1.13,
    "presence_penalty": 0.2,
    "frequency_penalty": -0.3,
    "seed": 173,
    "stop": ["one,two", "line\nnext", ""],
}


async def change(owner, section, field, value=None, mode="value"):
    snap = await call(owner, "snapshot", section=section)
    review = await call(
        owner,
        "review",
        section=section,
        revision=snap["revision"],
        field=field,
        value=value,
        mode=mode,
    )
    applied = await call(owner, "apply", section=section, token=review["token"])
    assert owner._native_model_preferences_hold
    result = await call(
        owner,
        "readback",
        section=section,
        token=review["token"],
        apply_id=applied["apply_id"],
    )
    assert not owner._native_model_preferences_hold
    return snap, review, result["preferences"]


def seed(owner, **values):
    candidate = copy.deepcopy(owner.settings)
    for key, value in values.items():
        setattr(candidate, key, value)
    settings_runtime.persist_or_raise(owner, candidate)
    owner.settings = candidate
    owner.backend.set_settings(candidate)
    effort_projection.install(owner)


@pytest.mark.parametrize("field,value", list(DEFAULTS.items()))
async def test_defaults_exact_destination_and_next_request(armed, field, value):
    owner = armed
    global_path = owner._agent_session.store.data_root / "settings.json"
    global_before = global_path.read_bytes()
    authority = owner._agent_session.authority
    _, review, after = await change(owner, "defaults", field, value)
    assert review["transaction"]["scope"] == "conversation"
    assert review["transaction"]["destination"] == str(
        owner.convo_dir / "settings.json"
    )
    assert after["fields"][field]["saved"] == value
    assert after["fields"][field]["active"] == value
    assert after["fields"][field]["next_request"] == value
    assert global_path.read_bytes() == global_before
    assert owner._agent_session.authority is authority
    owner._validate_owned_identity()
    assert owner._effective_request_overrides()[field] == value


@pytest.mark.parametrize("override", [0.9, None])
async def test_default_edit_reports_model_mask_and_preserves_override(armed, override):
    seed(armed, model_infer_overrides={armed.model_id: {"temperature": override}})
    before, _, after = await change(armed, "defaults", "temperature", 0.23)
    assert before["fields"]["temperature"]["overridden"]
    assert after["fields"]["temperature"]["next_request"] == override
    assert (
        armed.settings.model_infer_overrides[armed.model_id]["temperature"] == override
    )


async def test_preset_save_then_atomic_apply_preserves_authority_and_pending_load(
    app, monkeypatch
):
    owner = await local_owner(app, monkeypatch)
    for name in ("load", "unload", "apply_load_settings", "ensure_running"):
        monkeypatch.setattr(
            owner.backend,
            name,
            lambda *_a, **_k: pytest.fail("Preset reached model lifecycle"),
            raising=False,
        )
    seed(
        owner,
        model_infer_overrides={
            owner.model_id: {
                "temperature": 0.4,
                "reasoning_effort": "high",
                "future": {"keep": True},
            },
            "other": {"top_k": 99},
        },
        llama_load_settings={owner.model_id: {"ctx": 4096}, "other": {"ctx": 8192}},
    )
    authority = owner._agent_session.authority
    before_conversation = (owner.convo_dir / "settings.json").read_bytes()
    _, save_review, saved = await change(owner, "presets", "Night, exact", mode="save")
    assert save_review["transaction"]["scope"] == "defaults"
    assert (owner.convo_dir / "settings.json").read_bytes() == before_conversation
    assert saved["presets"]["Night, exact"] == {
        "inference": {
            "temperature": 0.4,
            "reasoning_effort": "high",
            "future": {"keep": True},
        },
        "load": {"ctx": 4096},
    }
    seed(
        owner,
        model_infer_overrides={owner.model_id: {"top_p": 0.8}, "other": {"top_k": 99}},
        llama_load_settings={owner.model_id: {"ctx": 2048}, "other": {"ctx": 8192}},
    )
    global_before = (
        owner._agent_session.store.data_root / "settings.json"
    ).read_bytes()
    _, applied_review, after = await change(
        owner, "presets", "Night, exact", mode="apply"
    )
    assert applied_review["transaction"]["scope"] == "conversation"
    assert set(applied_review["transaction"]["changes"]) == {
        "model_infer_overrides",
        "llama_load_settings",
    }
    assert after["raw"]["inference"]["saved"] == {
        "top_p": 0.8,
        "temperature": 0.4,
        "reasoning_effort": "high",
        "future": {"keep": True},
    }
    assert after["raw"]["load"]["saved"]["ctx"] == 4096
    assert after["raw"]["load"]["active"]["ctx"] == 2048
    assert after["fields"]["reasoning_effort"]["next_request"] == "high"
    assert owner.settings.model_infer_overrides["other"] == {"top_k": 99}
    assert owner.settings.llama_load_settings["other"] == {"ctx": 8192}
    assert (
        owner._agent_session.store.data_root / "settings.json"
    ).read_bytes() == global_before
    assert owner._agent_session.authority is authority
    owner._validate_owned_identity()
    doc = json.loads((owner.convo_dir / "settings.json").read_bytes())
    assert (
        doc["execution"]["model_infer_overrides"][owner.model_id]["reasoning_effort"]
        == "high"
    )
    assert doc["execution"]["llama_load_settings"][owner.model_id]["ctx"] == 4096


@pytest.mark.parametrize("field", DEFAULTS)
async def test_default_clear_uses_server_omission_without_model_override(armed, field):
    seed(armed, **{field: DEFAULTS[field]})
    _, _, after = await change(armed, "defaults", field, mode="server")
    assert after["fields"][field]["saved"] == ([] if field == "stop" else None)
    assert after["fields"][field]["next_request"] is None
    assert field not in armed._effective_request_overrides()


@pytest.mark.parametrize(
    "field,value",
    [
        ("temperature", True),
        ("temperature", float("inf")),
        ("seed", 1.5),
        ("seed", 2**63),
        ("stop", "a,b"),
        ("stop", [1]),
        ("stop", ["x" * 4097]),
        ("stop", ["x"] * 257),
    ],
)
async def test_invalid_defaults_never_publish(armed, field, value):
    snap = await call(armed, "snapshot", section="defaults")
    before = (armed.convo_dir / "settings.json").read_bytes()
    with pytest.raises((ValueError, TypeError)):
        await call(
            armed,
            "review",
            section="defaults",
            revision=snap["revision"],
            field=field,
            value=value,
            mode="value",
        )
    assert (armed.convo_dir / "settings.json").read_bytes() == before


async def test_preset_overwrite_review_is_exact_and_cancel_is_pure(armed):
    seed(armed, llama_presets={"name": {"load": {}, "inference": {"top_p": 0.7}}})
    before = (armed._agent_session.store.data_root / "settings.json").read_bytes()
    snap = await call(armed, "snapshot", section="presets")
    review = await call(
        armed,
        "review",
        section="presets",
        revision=snap["revision"],
        field="name",
        value=None,
        mode="save",
    )
    assert review["transaction"]["affected"] == [
        {
            "group": "preset",
            "field": "name",
            "before": {"load": {}, "inference": {"top_p": 0.7}},
            "after": {"load": {}, "inference": {}},
        }
    ]
    await call(armed, "snapshot")
    assert (
        armed._agent_session.store.data_root / "settings.json"
    ).read_bytes() == before
    with pytest.raises(ValueError, match="again"):
        await call(armed, "apply", section="presets", token=review["token"])


async def test_preset_apply_one_write_and_lost_publication_holds_admission(
    app, monkeypatch
):
    from test_rust_model_preferences import command

    from litetui import gui_rpc
    from litetui.settings_service import SettingsService

    owner = await local_owner(app, monkeypatch)
    seed(
        owner,
        llama_presets={
            "both": {"load": {"ctx": 4096}, "inference": {"reasoning_effort": "high"}}
        },
    )
    snap = await call(owner, "snapshot", section="presets")
    r = await call(
        owner,
        "review",
        section="presets",
        revision=snap["revision"],
        field="both",
        value=None,
        mode="apply",
    )
    original = SettingsService.save_patch
    changes_seen = []

    def save(service, session, changes, *args, **kwargs):
        changes_seen.append(tuple(row.key for row in changes))
        return original(service, session, changes, *args, **kwargs)

    monkeypatch.setattr(SettingsService, "save_patch", save)
    cmd = command(owner, "apply", section="presets", token=r["token"])
    result = await gui_rpc.async_dispatch(owner, cmd)
    assert set(changes_seen[0]) == {"model_infer_overrides", "llama_load_settings"}
    assert len(changes_seen) == 1
    assert owner._native_model_preferences_hold
    with pytest.raises(ValueError):
        await call(
            owner,
            "readback",
            section="presets",
            token=r["token"],
            apply_id=result["apply_id"],
        )
    assert owner._native_model_preferences_hold


async def test_preset_changed_after_review_is_refused_before_any_write(armed):
    seed(armed, llama_presets={"p": {"inference": {"top_p": 0.7}}})
    snap = await call(armed, "snapshot", section="presets")
    r = await call(
        armed,
        "review",
        section="presets",
        revision=snap["revision"],
        field="p",
        value=None,
        mode="apply",
    )
    path = armed._agent_session.store.data_root / "settings.json"
    raw = json.loads(path.read_bytes())
    raw["llama_presets"]["p"]["inference"]["top_p"] = 0.9
    path.write_text(json.dumps(raw), encoding="utf8")
    before = (armed.convo_dir / "settings.json").read_bytes()
    with pytest.raises(ValueError, match="changed"):
        await call(armed, "apply", section="presets", token=r["token"])
    assert (armed.convo_dir / "settings.json").read_bytes() == before


async def test_detached_configure_installs_projection_from_durable_owner(
    app, monkeypatch
):
    from litetui import gui_rpc

    owner = await local_owner(app, monkeypatch)
    authority = owner._agent_session.authority
    result = await gui_rpc.async_dispatch(
        owner,
        {
            "type": "gui.models.configure",
            "id": "source-config",
            "slug": owner.model_id,
            "load": {},
            "inference": {"reasoning_effort": "high"},
        },
    )
    assert result["saved"]
    assert owner._agent_session.authority is authority
    owner._validate_owned_identity()
    assert owner._effective_request_overrides()["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "failure", ["write_one", "wrong_receipt", "after_write", "runtime_second"]
)
async def test_atomic_preset_uncertainty_closes_admission_without_retry(
    app, monkeypatch, failure
):
    from dataclasses import replace

    from litetui.settings_service import SettingsService

    owner = await local_owner(app, monkeypatch)
    seed(
        owner,
        llama_presets={"p": {"inference": {"temperature": 0.8}, "load": {"ctx": 4096}}},
    )
    snap = await call(owner, "snapshot", section="presets")
    review = await call(
        owner,
        "review",
        section="presets",
        revision=snap["revision"],
        field="p",
        value=None,
        mode="apply",
    )
    original = SettingsService.save_patch

    def broken(service, session, changes, *args, **kwargs):
        outcome = original(
            service,
            session,
            changes[:1] if failure == "write_one" else changes,
            *args,
            **kwargs,
        )
        if failure == "wrong_receipt":
            outcome = replace(
                outcome,
                persistence=(
                    replace(outcome.persistence[0], fields=(changes[0].key,)),
                ),
            )
        if failure == "after_write":
            raise OSError("controlled post-write exception")
        return outcome

    if failure == "runtime_second":
        apply = settings_runtime.apply_saved_result

        def misreport(app, requested, result):
            outcome = apply(app, requested, result)
            return replace(outcome, runtime=outcome.runtime[:1])

        monkeypatch.setattr(settings_runtime, "apply_saved_result", misreport)
    else:
        monkeypatch.setattr(SettingsService, "save_patch", broken)
    with pytest.raises(ValueError, match="uncertain"):
        await call(owner, "apply", section="presets", token=review["token"])
    assert owner._owned_launch_error and owner._native_model_preferences_hold
    with pytest.raises(ValueError):
        await call(owner, "apply", section="presets", token=review["token"])


async def test_preset_effort_cannot_overwrite_real_invocation_pin(armed):
    assert armed._cli_effective_thinking == "default"
    seed(armed, llama_presets={"p": {"inference": {"reasoning_effort": "high"}}})
    snap = await call(armed, "snapshot", section="presets")
    before = (armed.convo_dir / "settings.json").read_bytes()
    with pytest.raises(ValueError, match="invocation"):
        await call(
            armed,
            "review",
            section="presets",
            revision=snap["revision"],
            field="p",
            value=None,
            mode="apply",
        )
    assert (armed.convo_dir / "settings.json").read_bytes() == before


async def test_default_readback_requires_exact_section(armed):
    snap = await call(armed, "snapshot", section="defaults")
    review = await call(
        armed,
        "review",
        section="defaults",
        revision=snap["revision"],
        field="temperature",
        value=0.3,
        mode="value",
    )
    result = await call(armed, "apply", section="defaults", token=review["token"])
    with pytest.raises(ValueError):
        await call(
            armed,
            "readback",
            section="presets",
            token=review["token"],
            apply_id=result["apply_id"],
        )
    assert armed._native_model_preferences_hold


async def test_presets_preserve_unknown_fields_and_inference_seed(app, monkeypatch):
    owner = await local_owner(app, monkeypatch)
    seed(
        owner,
        llama_presets={
            "future-build": {"load": {"cache_k": "a-future-cache"}},
            "opaque": {
                "inference": {"seed": 177, "future-infer": {"keep": [False, None]}},
                "load": {"future-load": {"keep": [1, "x"]}},
            },
        },
    )
    snap = await call(owner, "snapshot", section="presets")
    assert snap["presets"]["future-build"]["load"]["cache_k"] == "a-future-cache"
    with pytest.raises(ValueError):
        await call(
            owner,
            "review",
            section="presets",
            revision=snap["revision"],
            field="future-build",
            value=None,
            mode="apply",
        )
    _, _, after = await change(owner, "presets", "opaque", mode="apply")
    assert after["request"]["seed"] == 177
    assert after["raw"]["inference"]["saved"]["future-infer"] == {"keep": [False, None]}
    _, _, saved = await change(owner, "presets", "copy", mode="save")
    assert saved["presets"]["copy"]["inference"]["seed"] == 177
    assert saved["presets"]["copy"]["load"]["future-load"] == {"keep": [1, "x"]}


async def test_named_save_refuses_unrelated_active_catalog_reconciliation(armed):
    path = armed._agent_session.store.data_root / "settings.json"
    raw = json.loads(path.read_bytes())
    raw["llama_presets"] = {"external": {"inference": {"temperature": 0.7}}}
    path.write_text(json.dumps(raw), encoding="utf8")
    snap = await call(armed, "snapshot", section="presets")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Saved presets differ"):
        await call(
            armed,
            "review",
            section="presets",
            revision=snap["revision"],
            field="new",
            value=None,
            mode="save",
        )
    assert path.read_bytes() == before


async def test_detached_configure_preserves_real_invocation_pin(armed):
    from litetui import gui_rpc

    pin = armed._cli_effective_thinking
    authority = armed._agent_session.authority
    await gui_rpc.async_dispatch(
        armed,
        {
            "type": "gui.models.configure",
            "id": "source-config",
            "slug": armed.model_id,
            "load": {},
            "inference": {"reasoning_effort": "high"},
        },
    )
    assert armed._cli_effective_thinking == pin == "default"
    assert armed._agent_session.authority is authority
    armed._validate_owned_identity()
    assert "reasoning_effort" not in armed._effective_request_overrides()
