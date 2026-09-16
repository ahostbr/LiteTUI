"""T751 (0.23.1): the official Codex app-server is opt-in; LiteTUI's loop is the default.

RYAN 2026-09-16: "WHAT I WANT IS ALL THE GOOD PARTS OF THE CACHEING WITHOUT LOSING
R HARNESS". Every other module discriminates on ``hasattr(backend, "app_server")``,
so these arms pin the one place that attribute is born.
"""

from types import SimpleNamespace as NS

import pytest

from litetui import codex_settings
from litetui import model_transport as mt
from litetui.oauth_backend import OAuthBackend
from litetui.settings import Settings


def test_default_codex_backend_runs_litetui_loop_no_app_server():
    backend = OAuthBackend(Settings(backend="codex"))
    assert not hasattr(backend, "app_server")
    assert Settings().codex_native_engine is False


def test_native_flag_constructs_the_app_server(monkeypatch):
    born = []

    class FakeServer:
        def __init__(self):
            born.append(self)

    import litetui.codex_app_server as cas
    monkeypatch.setattr(cas, "AppServer", FakeServer)
    backend = OAuthBackend(Settings(backend="codex", codex_native_engine=True))
    assert backend.app_server is born[0]


def test_for_app_routes_by_engine_and_carries_prompt_cache_key():
    app = NS(backend=OAuthBackend(Settings(backend="codex")), convo_id="convo-42", client=None)
    transport = mt.for_app(app)
    assert isinstance(transport, mt.OAuthTransport)
    assert transport.prompt_cache_key == "convo-42"
    body = mt.codex_request({"model": "gpt-test", "messages": [], "prompt_cache_key": "convo-42"})
    assert body["prompt_cache_key"] == "convo-42"

    app.backend.app_server = NS()  # what codex_native_engine=True produces
    assert type(mt.for_app(app)).__name__ == "AppServerTransport"


def test_settings_ownership_follows_the_engine_not_the_name():
    litetui_loop = NS(name="codex")
    native = NS(name="codex", app_server=object())
    assert codex_settings.control(litetui_loop, "autocompact_enabled") is None
    assert codex_settings.control(native, "autocompact_enabled") is codex_settings.ENGINE
    assert codex_settings.control("codex", "autocompact_enabled") is None  # a bare name is not an engine


@pytest.mark.asyncio
async def test_litetui_loop_path_reads_credentials_and_cli_model_cache(tmp_path, monkeypatch):
    import json
    cred = tmp_path / "auth.json"
    monkeypatch.setattr(mt, "credential_path", lambda name: cred)
    import litetui.oauth_backend as ob
    monkeypatch.setattr(ob, "credential_path", lambda name: cred)
    monkeypatch.setattr(ob, "read_credentials", lambda name: NS(access="t", account_id="a"))
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-test", "context_window": 1000, "visibility": "list"},
        {"slug": "hidden", "context_window": 1000, "visibility": "hidden"},
        {"slug": "no-window"},
    ]}), encoding="utf-8")
    backend = OAuthBackend(Settings(backend="codex"))
    assert await backend.ensure_running() == "ok"
    rows = await backend.list_models()
    assert [r.key for r in rows] == ["gpt-test"]
    assert await backend.model_info("gpt-test") == (950, "llm", True)
