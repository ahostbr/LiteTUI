import json
import os

import pytest
from textual.widgets import Button, Input, Select, Static, Switch, TextArea

from litetui.app import LiteTUI
from litetui.hooks_screen import HooksEditor, HooksScreen


def app_fixture():
    app = LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.jobs[:] = []
    app.settings.tool_policy_profile = "autonomous"
    return app


async def click(pilot, app, id):
    button = app.screen.query_one(id, Button)
    button.scroll_visible(immediate=True, animate=False)
    await pilot.pause()
    await pilot.click(id)
    await pilot.pause(.35)


@pytest.mark.asyncio
async def test_author_save_validate_test_and_reopen(tmp_path):
    app = app_fixture()
    async with app.run_test(size=(120, 50)) as pilot:
        app.push_screen(HooksScreen())
        await pilot.pause()
        await click(pilot, app, "#hook-new")
        field = app.screen.query_one("#hook-field-id", Input)
        field.focus()
        await pilot.press("home", "shift+end", "backspace", "k", "e", "y", "s")
        assert field.value == "keys"
        app.screen.query_one("#hook-mode", Select).value = "gate"
        app.screen.query_one("#hook-field-timeout", Input).value = "0"
        await click(pilot, app, "#hook-save")
        assert not app.hook_config.project_path.exists()
        assert "timeout" in str(app.screen.query_one("#hook-error", Static).render())
        app.screen.query_one("#hook-field-timeout", Input).value = "10"
        app.screen.query_one("#hook-field-argv", Input).value = json.dumps(["-c", 'print(\'{"decision":"allow"}\')'])
        await click(pilot, app, "#hook-save")
        assert app.hook_config.read("project"), str(app.screen.query_one("#hook-error", Static).render())
        assert app.hook_config.read("project")[0].mode == "gate"
        before = list(app.conversation)
        await click(pilot, app, "#hook-test")
        for _ in range(40):
            if app.hook_results:
                break
            await pilot.pause(.05)
        assert app.hook_results[("project", "keys")].allowed
        assert app.conversation == before
        await pilot.press("escape")
        app.push_screen(HooksScreen())
        await pilot.pause()
        assert app.screen.query_one("#hook-field-id", Input).value == "keys"
        # The actual mounted editor is exported for visual inspection.
        app.save_screenshot(filename="hooks-editor.svg", path=os.environ.get("HOOKS_TEST_SCREENSHOT_DIR", str(tmp_path)))


@pytest.mark.asyncio
async def test_settings_tab_and_slash_command_share_editor():
    from litetui.settings_screen import SettingsScreen
    app = app_fixture()
    async with app.run_test(size=(120, 50)) as pilot:
        app.push_screen(SettingsScreen(app.settings))
        await pilot.pause()
        assert app.screen.query_one("#tab-hooks").query_one(HooksEditor)
        await pilot.press("escape")
        app.settings.dialog_style = "sidebar"
        app._handle_command("/hooks")
        await pilot.pause()
        assert app.query(HooksEditor)


@pytest.mark.asyncio
async def test_disabled_project_override_and_delete_reveal_global():
    from litetui.lifecycle_hooks import Hook
    app = app_fixture()
    hook = Hook.parse({"id": "inherited", "events": ["tool_before"], "executable": "python"})
    app.hook_config.save("global", [hook], [])
    async with app.run_test(size=(120, 50)) as pilot:
        app.push_screen(HooksScreen())
        await pilot.pause()
        app.screen.query_one("#hook-scope", Select).value = "global"
        await pilot.pause()
        await click(pilot, app, "#hook-override")
        app.screen.query_one("#hook-enabled", Switch).value = False
        await click(pilot, app, "#hook-save")
        assert not app.hook_config.snapshot().hooks[0].enabled
        assert app.hook_config.snapshot().hooks[0].scope == "project"
        await click(pilot, app, "#hook-delete")
        await click(pilot, app, "#hook-save")
        assert app.hook_config.snapshot().hooks[0].scope == "global"
        assert app.hook_config.snapshot().hooks[0].enabled


@pytest.mark.asyncio
async def test_malformed_file_can_be_repaired_in_editor():
    app = app_fixture()
    app.hook_config.project_path.write_text("{broken")
    async with app.run_test(size=(120, 50)) as pilot:
        app.push_screen(HooksScreen())
        await pilot.pause()
        area = app.screen.query_one("#hook-repair-json", TextArea)
        assert area.display
        area.load_text('{"version": 1, "hooks": []}')
        await click(pilot, app, "#hook-repair")
        assert not app.hook_config.snapshot().error
