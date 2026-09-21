"""A backend created by resume must be usable without a startup connect."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from litetui import oauth_backend
from litetui.llm_backend import BackendError


def fresh_backend(tmp_path, monkeypatch, *, native=False):
    monkeypatch.setattr(oauth_backend, "read_credentials", lambda name: None)
    monkeypatch.setattr(oauth_backend, "credential_path", lambda name: tmp_path / "auth.json")
    backend = oauth_backend.OAuthBackend(
        SimpleNamespace(backend="codex", codex_native_engine=False)
    )
    if native:
        backend.app_server = SimpleNamespace(start=AsyncMock(), request=AsyncMock())
    return backend


def test_resumed_codex_loads_catalog_before_first_send(tmp_path, monkeypatch):
    backend = fresh_backend(tmp_path, monkeypatch)
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "saved-model", "context_window": 100000}
    ]}))
    assert backend.models == {}
    asyncio.run(backend.ensure_chat_ready("saved-model"))
    assert "saved-model" in backend.models
    # Once populated, readiness must not perform another catalog lookup.
    backend.list_models = AsyncMock(side_effect=AssertionError("unexpected reload"))
    asyncio.run(backend.ensure_chat_ready("saved-model"))


def test_resumed_codex_still_rejects_unavailable_saved_model(tmp_path, monkeypatch):
    backend = fresh_backend(tmp_path, monkeypatch)
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "available-model", "context_window": 100000}
    ]}))
    with pytest.raises(BackendError, match="Choose an available Codex model"):
        asyncio.run(backend.ensure_chat_ready("missing-model"))
    assert list(backend.models) == ["available-model"]


def test_resumed_codex_reports_metadata_failure_not_invalid_model(tmp_path, monkeypatch):
    backend = fresh_backend(tmp_path, monkeypatch)
    with pytest.raises(BackendError, match="metadata is unavailable"):
        asyncio.run(backend.ensure_chat_ready("saved-model"))


def test_resumed_native_codex_initializes_account_then_catalog(tmp_path, monkeypatch):
    backend = fresh_backend(tmp_path, monkeypatch, native=True)
    backend.app_server.request.side_effect = [
        {"account": {"type": "chatgpt"}},
        {"data": [{"model": "saved-model", "defaultReasoningEffort": "medium",
                   "supportedReasoningEfforts": [{"reasoningEffort": "medium", "description": ""}]}]},
    ]
    asyncio.run(backend.ensure_chat_ready("saved-model"))
    assert [call.args[0] for call in backend.app_server.request.await_args_list] == [
        "account/read", "model/list"
    ]
    assert "saved-model" in backend.models
