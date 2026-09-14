import json
from types import SimpleNamespace as NS

import pytest

from litetui import oauth_backend
from litetui.llm_backend import BackendError


def native(key="gpt-6-astra", **extra):
    return {
        "model": key,
        "defaultReasoningEffort": "medium",
        "hidden": False,
        "supportedReasoningEfforts": [
            {"reasoningEffort": level, "description": level}
            for level in ("low", "medium", "high", "xhigh", "max", "ultra")
        ],
        "inputModalities": ["text", "image"],
        **extra,
    }


def backend(monkeypatch, tmp_path, pages, cached=None):
    monkeypatch.setattr(
        oauth_backend, "credential_path", lambda _: tmp_path / "auth.json"
    )
    if cached is not None:
        (tmp_path / "models_cache.json").write_text(json.dumps({"models": cached}))
    calls = []

    async def start():
        pass

    async def request(method, params):
        calls.append((method, params))
        return pages[len(calls) - 1]

    instance = oauth_backend.OAuthBackend(NS(backend="codex", model_infer_overrides={}))
    instance.app_server = NS(start=start, request=request)
    return instance, calls


@pytest.mark.asyncio
async def test_native_catalog_overrides_stale_efforts_default_and_visibility(
    monkeypatch, tmp_path
):
    instance, calls = backend(
        monkeypatch,
        tmp_path,
        [{"data": [native()]}],
        [
            {
                "slug": "gpt-6-astra",
                "visibility": "hidden",
                "context_window": 272000,
                "effective_context_window_percent": 95,
                "default_reasoning_level": "xhigh",
                "supported_reasoning_levels": [{"effort": "xhigh"}],
            }
        ],
    )
    assert [row.key for row in await instance.list_models()] == ["gpt-6-astra"]
    assert instance.reasoning_levels("gpt-6-astra") == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ]
    assert instance.models["gpt-6-astra"]["default_reasoning_level"] == "medium"
    assert (await instance.model_info("gpt-6-astra"))[0] == 258400
    assert calls == [("model/list", {"includeHidden": False})]


@pytest.mark.asyncio
async def test_no_cache_still_lists_native_models_and_does_not_invent_context_window(
    monkeypatch, tmp_path
):
    instance, calls = backend(
        monkeypatch,
        tmp_path,
        [
            {"data": [native()], "nextCursor": "next"},
            {"data": [native("other"), native("hidden", hidden=True)]},
        ],
    )
    assert [row.key for row in await instance.list_models()] == ["gpt-6-astra", "other"]
    assert await instance.model_info("gpt-6-astra") is None
    assert calls[1][1]["cursor"] == "next"


@pytest.mark.asyncio
async def test_failed_catalog_does_not_silently_use_stale_cache_or_change_saved_effort(
    monkeypatch, tmp_path
):
    instance, _ = backend(
        monkeypatch, tmp_path, [{"data": []}], [{"slug": "old", "context_window": 123}]
    )
    instance.settings.model_infer_overrides = {
        "gpt-6-astra": {"reasoning_effort": "high"}
    }
    with pytest.raises(BackendError, match="no available models"):
        await instance.list_models()
    assert instance.request_overrides("gpt-6-astra") == {"reasoning_effort": "high"}


@pytest.mark.asyncio
async def test_repeated_cursor_fails_without_looping(monkeypatch, tmp_path):
    page = {"data": [native()], "nextCursor": "same"}
    instance, calls = backend(monkeypatch, tmp_path, [page, page])
    with pytest.raises(BackendError, match="unavailable"):
        await instance.list_models()
    assert len(calls) == 2
