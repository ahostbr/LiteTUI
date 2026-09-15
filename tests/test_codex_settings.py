from types import SimpleNamespace as NS

import pytest
from textual.app import App

from litetui.codex_settings import CONTROLS, loop_description
from litetui.settings import Settings
from litetui.settings_screen import SettingsBody, SettingsScreen


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["codex", "lmstudio"])
async def test_settings_explain_native_scope_and_preserve_local_values(backend):
    settings = Settings(backend=backend, temperature=0.85, tool_iterations=123)

    class Host(App):
        model_id = "gpt-6-astra"

        def on_mount(self):
            self.backend = NS(
                name=backend,
                reasoning_levels=lambda _: [
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                ],
            )
            self.push_screen(SettingsScreen(settings))

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        for name, capability in CONTROLS.items():
            widget = body.query_one(f"#f-{name}")
            assert widget.disabled == (
                backend == "codex" and not capability.editable
            ), name
        saved = body._collect()
        assert saved.temperature == 0.85
        assert saved.tool_iterations == 123
        assert saved.thinking_level == "medium"


def test_loop_description_does_not_claim_a_local_cap_for_codex():
    assert "123" not in loop_description("codex", 123)
    assert "123" in loop_description("lmstudio", 123)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["low", "medium", "high", "xhigh", "max", "ultra"])
@pytest.mark.parametrize("backend", ["codex", "lmstudio"])
async def test_global_reasoning_default_respects_backend_scope(monkeypatch, tmp_path, mode, backend):
    from dataclasses import replace

    from litetui import convo_settings
    from litetui import settings as settings_mod
    from litetui.app import LiteTUI

    app = LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    notes = []
    app._system = notes.append
    save = settings_mod.save
    monkeypatch.setattr(settings_mod, "save", lambda value: save(value, root=tmp_path))
    async with app.run_test() as pilot:
        app.backend = NS(name=backend, remote=True, shutdown=lambda: None)
        app.settings = replace(app.settings, backend=backend, thinking_level="medium")
        app.convo_dir = tmp_path / "conversation"
        app.convo_path = app.convo_dir / "convo.jsonl"
        app._adopt_convo_settings(born=True)
        app.thinking_level = mode
        before = convo_settings.load(app.convo_path.parent)
        assert before.thinking_level == mode
        default = "high" if mode == "low" else "low"
        app._on_settings_saved(replace(app.settings, thinking_level=default))
        await pilot.pause()
        expected = mode if backend == "codex" else default
        assert app.thinking_level == expected
        assert convo_settings.load(app.convo_path.parent).thinking_level == expected
        if backend == "codex":
            assert convo_settings.load(app.convo_path.parent) == before
        assert settings_mod.load(root=tmp_path).thinking_level == default
        assert convo_settings.born_from(settings_mod.load(root=tmp_path)).thinking_level == default
        assert any("new conversations" in note for note in notes) == (backend == "codex")
