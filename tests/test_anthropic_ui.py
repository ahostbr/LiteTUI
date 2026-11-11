"""Native API user paths, isolated from live credentials, fleet and paid HTTP."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest
from _owned_seat import owned_app
from test_anthropic_api import KEY, MODEL, backend, completed
from textual.widgets import Input

from litetui import model_transport
from litetui.plugins import model_switch
from litetui.settings import Settings
from litetui.settings_screen import SettingsBody, SettingsScreen


def native_app(b):
    app = owned_app(initial_backend="anthropic", initial_model=MODEL)
    app.seat.registered = True
    app._inbox_monitor = lambda: None
    app.connect = app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.settings = b.settings
    app.settings.tools_enabled = app.tools_enabled = False
    app.settings.autocompact_enabled = False
    app.settings.thinking_level = app.thinking_level = "high"
    app.backend = b
    app.model_id = MODEL
    app._client_binding = model_transport.bind_client(app.client, b)
    return app


def test_first_boot_does_not_replace_api_backend_with_local_engine(monkeypatch):
    monkeypatch.delenv("LITETUI_BACKEND", raising=False)
    monkeypatch.setattr(model_switch.shutil, "which", lambda _: "lms")
    monkeypatch.setattr(model_switch.llm_backend, "llama_available", lambda: True)
    monkeypatch.setattr(model_switch, "pick", lambda *a, **k: pytest.fail("API backend met local engine picker"))
    app = NS(settings=Settings(backend="anthropic"), backend=NS(name="anthropic"))
    model_switch._activate(app)
    assert app.settings.backend == "anthropic"


@pytest.mark.asyncio
async def test_paid_key_settings_are_masked_and_collectable():
    b = backend(lambda _: pytest.fail("settings must not send messages"),
                anthropic_workspace_id="wrkspc_fixture")
    await b.list_models()
    app = native_app(b)
    async with app.run_test(size=(130, 46)) as pilot:
        app.push_screen(SettingsScreen(app.settings))
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        search = body.query_one("#set-search", Input)
        search.value = "Anthropic API"
        await pilot.pause()
        await pilot.pause()
        key = body.query_one("#f-anthropic_api_key", Input)
        assert key.password and key.value == KEY and key.region.height > 0
        collected = body._collect()
        assert collected.anthropic_api_key == KEY
        assert collected.anthropic_workspace_id == "wrkspc_fixture"
        if destination := os.environ.get("LITETUI_ANTHROPIC_SCREENSHOT"):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            app.save_screenshot(filename=Path(destination).name, path=str(Path(destination).parent))


@pytest.mark.asyncio
async def test_owned_app_turn_reaches_messages_and_commits_assistant_reply():
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=completed("Native API reply"))

    app = native_app(backend(handle))
    async with app.run_test(size=(120, 40)) as pilot:
        app.conversation = [{"role": "system", "content": "Test host"}]
        app._append({"role": "user", "content": "Hello native API"})
        app._stream()
        async with asyncio.timeout(15):
            while app._chat_running():
                await pilot.pause()
                await asyncio.sleep(0.01)
        await pilot.pause()
        assert bodies, "No native Messages request arrived"
        assert bodies[0]["model"] == MODEL
        assert bodies[0]["output_config"] == {"effort": "high"}
        assert any(m.get("role") == "assistant" and m.get("content") == "Native API reply"
                   for m in app.conversation)
