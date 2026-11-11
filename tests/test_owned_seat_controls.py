"""Observer controls stay scoped despite later pytest patch restoration."""
from __future__ import annotations

import sys
from functools import wraps
from types import SimpleNamespace

import _owned_seat as seats
import pytest

from litetui import llm_backend, thinking_probe, update_check


@pytest.fixture
def seams(monkeypatch):
    seats.release_all()
    calls = []

    async def listing(backend):
        calls.append(("model_list", backend))
        return ["original-list"]

    def loaded(backend):
        calls.append(("readiness", backend))
        return ["original-loaded"]

    def thinking(*args, **kwargs):
        calls.append(("thinking", (args, kwargs)))
        return "original-thinking"

    def update(app):
        calls.append(("updates", app))
        return "original-update"

    monkeypatch.setattr(llm_backend.LMStudioBackend, "list_models", listing)
    monkeypatch.setattr(llm_backend.LMStudioBackend, "loaded_models", loaded)
    monkeypatch.setattr(thinking_probe, "get_effective_levels", thinking)
    monkeypatch.setattr(update_check, "start_background_check", update)
    yield calls
    seats.release_all()


def restored_after_release(app=None):
    """Actual fixture order: helper releases before a later monkeypatch undo."""
    seats.control_external_effects(app)
    with pytest.MonkeyPatch.context() as later:
        for target, name in (
            (llm_backend.LMStudioBackend, "list_models"),
            (llm_backend.LMStudioBackend, "loaded_models"),
            (thinking_probe, "get_effective_levels"),
            (update_check, "start_background_check"),
        ):
            later.setattr(target, name, lambda *_args, **_kwargs: None)
        seats.release_all()
    assert seats._EFFECTS is None


@pytest.mark.asyncio
async def test_repeated_late_restore_does_not_build_a_delegation_chain(seams):
    # Broad collection failed after 943 retained wrappers. These cycles need no
    # app, provider, live model inventory, hardware or background updater.
    for _ in range(sys.getrecursionlimit() + 100):
        restored_after_release()
    backend = object()
    app = SimpleNamespace(backend=backend)
    assert await llm_backend.LMStudioBackend.list_models(backend) == ["original-list"]
    assert llm_backend.LMStudioBackend.loaded_models(backend) == ["original-loaded"]
    update_check.start_background_check(app)
    assert thinking_probe.get_effective_levels("model", backend="fixture") == "original-thinking"
    assert [name for name, _ in seams] == ["model_list", "readiness", "updates", "thinking"]


@pytest.mark.asyncio
async def test_retired_controls_do_not_override_a_later_real_effects_policy(seams):
    backend = object()
    app = SimpleNamespace(backend=backend, available_models=["fixture-model"], model_rows={})
    restored_after_release(app)
    seats.control_external_effects(app, real_effects=seats._EFFECT_NAMES)
    assert await llm_backend.LMStudioBackend.list_models(backend) == ["original-list"]
    assert llm_backend.LMStudioBackend.loaded_models(backend) == ["original-loaded"]
    update_check.start_background_check(app)
    assert thinking_probe.get_effective_levels("model") == "original-thinking"
    assert [name for name, _ in seams] == ["model_list", "readiness", "updates", "thinking"]


@pytest.mark.asyncio
async def test_active_controls_keep_owned_catalog_and_unrelated_backend_seam(seams):
    restored_after_release()
    backend = object()
    app = SimpleNamespace(backend=backend, available_models=["fixture-model"], model_rows={})
    seats.control_external_effects(app)
    rows = await llm_backend.LMStudioBackend.list_models(backend)
    assert [(row.key, row.loaded) for row in rows] == [("fixture-model", False)]
    assert llm_backend.LMStudioBackend.loaded_models(backend) == []
    update_check.start_background_check(app)
    assert thinking_probe.get_effective_levels("model") is None
    assert seams == []
    unrelated = object()
    assert await llm_backend.LMStudioBackend.list_models(unrelated) == ["original-list"]
    assert llm_backend.LMStudioBackend.loaded_models(unrelated) == ["original-loaded"]
    update_check.start_background_check(SimpleNamespace(backend=unrelated))
    assert [name for name, _ in seams] == ["model_list", "readiness", "updates"]
    with pytest.raises(ValueError, match="same external-effect policy"):
        seats.control_external_effects(app, real_effects={"model_list"})


@pytest.mark.asyncio
async def test_unrelated_wrapper_is_preserved_across_release_and_reinstallation(seams):
    restored_after_release()
    previous = llm_backend.LMStudioBackend.list_models
    calls = []

    @wraps(previous)
    async def foreign(backend):
        calls.append(backend)
        return await previous(backend)

    with pytest.MonkeyPatch.context() as external:
        external.setattr(llm_backend.LMStudioBackend, "list_models", foreign)
        backend = object()
        seats.control_external_effects()
        assert await llm_backend.LMStudioBackend.list_models(backend) == ["original-list"]
        seats.release_all()
        assert llm_backend.LMStudioBackend.list_models is foreign
        assert calls == [backend]
        assert seams == [("model_list", backend)]
