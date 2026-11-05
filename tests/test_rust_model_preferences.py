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
    return await gui_rpc.async_dispatch(owner, command(owner, action, **fields))


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
