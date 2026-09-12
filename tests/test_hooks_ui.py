import json
import os

import pytest
from textual.widgets import Button, Input, Select, Static, Switch, TextArea

from litetui.app import LiteTUI
from litetui.hooks_screen import HooksEditor, HooksScreen


@pytest.fixture
def app_fixture(monkeypatch):
    # These arms own editor workers, not fleet/scheduler monitors. Keep the
    # actual hook lifecycle and Test-script workers, and prove they terminate.
    monkeypatch.setattr("litetui.cron.monitor", lambda app: None)
    created = []

    def build():
        app = LiteTUI()
        app._connect = lambda: None
        app._fetch_ctx_window = lambda: None
        app._inbox_monitor = lambda: None
        app.jobs[:] = []
        app.settings.tool_policy_profile = "autonomous"
        started = []
        run_worker = app.run_worker

        def track(*args, **kwargs):
            worker = run_worker(*args, **kwargs)
            started.append(worker)
            return worker

        app.run_worker = track
        created.append((app, started))
        return app

    yield build
    for app, started in created:
        assert not app.workers, "a Hooks UI app retained workers after run_test"
        assert all(worker.is_finished for worker in started), "a Hooks UI worker outlived its app"
        assert not {worker.group for worker in started} & {"inbox", "cron"}


async def click(pilot, app, id):
    button = app.screen.query_one(id, Button)
    button.scroll_visible(immediate=True, animate=False)
    await pilot.pause()
    await pilot.click(id)
    await pilot.pause(.35)


@pytest.mark.asyncio
async def test_author_save_validate_test_and_reopen(tmp_path, app_fixture):
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
async def test_settings_tab_and_slash_command_share_editor(app_fixture):
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
async def test_disabled_project_override_and_delete_reveal_global(app_fixture):
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
async def test_malformed_file_can_be_repaired_in_editor(app_fixture):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("settings_host", [False, True])
@pytest.mark.parametrize("broken", [False, True])
async def test_swap_preserves_unsaved_hook_state_and_disk_baseline(settings_host, broken, app_fixture):
    from functools import partial

    from litetui.hooks_screen import HooksBody
    from litetui.lifecycle_hooks import Hook
    from litetui.settings_screen import SettingsBody
    from litetui.side_panel import DialogController, SwapButton

    app = app_fixture()
    hook = Hook.parse({"id": "existing", "events": ["tool_before"], "executable": "python"}, "global")
    app.hook_config.save("global", [hook], [])
    if broken:
        app.hook_config.global_path.write_text("{broken", encoding="utf-8")
    async with app.run_test(size=(120, 50)) as pilot:
        factory = partial(SettingsBody, app.settings) if settings_host else HooksBody
        ctrl = DialogController(app, factory, "sidebar", "right")
        app.run_worker(ctrl.open(), name="hook-swap")
        for _ in range(8):
            await pilot.pause()
        editor = app.query_one(HooksEditor)
        editor.query_one("#hook-scope", Select).value = "global"
        await pilot.pause()
        if not broken:
            editor.query_one("#hook-new", Button).press()
            await pilot.pause()
        editor.query_one("#hook-field-argv", Input).value = "[unfinished"
        editor.query_one("#hook-sample", TextArea).load_text('{"event":')
        editor.query_one("#hook-repair-json", TextArea).load_text('{"version": 1, "hooks": []}')
        expected = editor.get_state()
        # A concurrent change must still conflict after swapping, not become
        # the new baseline and get silently overwritten by Save/Repair.
        app.hook_config.global_path.write_text("{changed externally", encoding="utf-8")
        for style in ("modal", "sidebar"):
            ctrl._view.body.query_one(SwapButton).press()
            for _ in range(12):
                await pilot.pause()
            assert ctrl.style == style and ctrl.pending
            editor = ctrl._view.body.query_one(HooksEditor)
            carried = editor.get_state()
            for key in ("scope", "rows", "baseline", "selected", "broken_bytes", "values", "text", "display"):
                assert carried[key] == expected[key], f"swap lost {key}"
        editor.query_one("#hook-field-argv", Input).value = "[]"
        editor.query_one("#hook-repair" if broken else "#hook-save", Button).press()
        await pilot.pause()
        assert app.hook_config.global_path.read_text(encoding="utf-8") == "{changed externally"
        assert str(editor.query_one("#hook-error", Static).render())
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_theme_round_trip_after_hooks_editor_in_the_same_process(tmp_path, app_fixture):
    from test_theme_extra_tokens import (
        test_footer_colours_round_trip_and_preserve_warnings,
    )

    # Exercise actual author/save/Test/reopen and then the complete existing
    # theme acceptance arm, with all its CSS/Rich/persistence assertions intact.
    await test_author_save_validate_test_and_reopen(tmp_path, app_fixture)
    await test_footer_colours_round_trip_and_preserve_warnings(tmp_path)
