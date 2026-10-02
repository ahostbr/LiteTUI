"""T0324-B: external Select labels must be literal in current and overlay rows."""
from types import SimpleNamespace

import pytest
from test_literal_display_sinks import PAYLOAD, SinkApp, make_app
from textual.widgets import Select, Static
from textual.widgets._select import SelectOverlay

from litetui.hooks_screen import HooksEditor


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["hook-select", "hook-rejected"])
async def test_hook_selection_and_rejected_prompt_labels_are_literal(target):
    async with SinkApp(HooksEditor()).run_test() as pilot:
        editor = pilot.app.query_one(HooksEditor)
        editor.rows = [SimpleNamespace(id=PAYLOAD, mode="observe", enabled=True)]
        editor.selected = PAYLOAD
        pilot.app.hook_config = SimpleNamespace(
            global_path="global", project_path="project",
            snapshot=lambda: SimpleNamespace(hooks=[], disabled=False, error=None))
        pilot.app.rejected_prompts = [{"content": PAYLOAD, "reason": PAYLOAD}]
        editor.refresh_rows()
        selector = editor.query_one("#" + target, Select)
        selector.value = PAYLOAD if target == "hook-select" else 0
        await pilot.pause()
        overlay = selector.query_one(SelectOverlay)
        assert PAYLOAD in overlay._get_visual(overlay.get_option_at_index(1)).plain
        assert PAYLOAD in selector.query_one("#label", Static).render().plain


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["default_model", "tool_summary_model", "subagent_model"])
async def test_settings_model_select_labels_are_literal(monkeypatch, field):
    from litetui.settings_screen import SettingsScreen
    app = make_app(monkeypatch)
    app.settings.default_model = PAYLOAD
    async with app.run_test(size=(120, 50)) as pilot:
        app.push_screen(SettingsScreen(app.settings, models=[PAYLOAD]))
        await pilot.pause()
        selector = app.screen.query_one("#f-" + field, Select)
        selector.value = PAYLOAD
        await pilot.pause()
        overlay = selector.query_one(SelectOverlay)
        option = next(option for option in overlay._options if PAYLOAD in str(option.prompt))
        assert PAYLOAD in overlay._get_visual(option).plain
        assert PAYLOAD in selector.query_one("#label", Static).render().plain


@pytest.mark.asyncio
async def test_model_preset_name_select_is_literal(monkeypatch):
    from litetui.plugins.model_switch import ModelConfigScreen
    app = make_app(monkeypatch)
    app.backend = SimpleNamespace(name="lmstudio", request_overrides=lambda _key: {})
    app.settings.llama_presets = {PAYLOAD: {}}
    async with app.run_test(size=(120, 50)) as pilot:
        app.push_screen(ModelConfigScreen("model"))
        await pilot.pause()
        selector = app.screen.query_one("#mc-preset-apply", Select)
        overlay = selector.query_one(SelectOverlay)
        assert overlay._get_visual(overlay.get_option_at_index(1)).plain == PAYLOAD
        selector.value = PAYLOAD
        await pilot.pause()
        assert selector.query_one("#label", Static).render().plain == PAYLOAD


def test_literal_options_preserve_values_and_existing_styled_renderables():
    from rich.text import Text

    from litetui.literal_display import literal_options
    identity = object()
    styled = Text("intentional", style="bold red")
    rows = literal_options([(PAYLOAD, identity), (styled, identity)])
    assert isinstance(rows[0][0], Text)
    assert rows[0][0].plain == PAYLOAD
    assert rows[0][1] is identity
    assert rows[1][0] is styled
    assert rows[1][1] is identity
