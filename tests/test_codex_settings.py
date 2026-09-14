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
            # Threshold is currently configured outside this settings panel.
            if name == "tool_context_threshold_chars":
                continue
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
