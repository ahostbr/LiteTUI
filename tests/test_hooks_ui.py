import json
import os

import pytest
from _settle import settle_until
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
    """Scroll the target into view, WAIT FOR IT TO BE THERE, then click. T717.

    🔴 ONE PAUSE WAS A GUESS, AND UNDER LOAD IT LOST. `scroll_visible` asks for
    a scroll; the region does not move until the app has processed it. The old
    helper paused ONCE and clicked, so on a busy box the click was computed
    against the OLD region and Textual refused it:

        textual.pilot.OutOfBounds: Target offset is outside of currently-visible
        screen region.
        widget = '#hook-repair', offset = Offset(x=8, y=86)   # on a 50-row screen

    That is the arm that went red in T704's rep 1 and was carried as a flake.

    📌 THE PREDICATE IS PILOT'S OWN CHECK, not an approximation of it.
    `Pilot.click` computes `target.region.offset + offset` and refuses unless
    that POINT is in `screen.region` (pilot.py:433-444). Waiting for the whole
    region to be visible would be a different, stricter question that can be
    false while the click would have worked — and waiting for "some" visibility
    could pass while the point is still outside. Ask exactly what it asks.

    A longer sleep would have made this rarer and left it load-sensitive; the
    wait is bounded and on the CONDITION, which is the same law `_settle.py`
    exists for.
    """
    button = app.screen.query_one(id, Button)
    button.scroll_visible(immediate=True, animate=False)
    landed = await settle_until(pilot, lambda: button.region.offset in app.screen.region)
    assert landed, (
        f"{id} never scrolled into the visible region: "
        f"click point {button.region.offset} is outside {app.screen.region}"
    )
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


# ── T717: the helper waits for the scroll, and the old one did not ──────────

async def _click_the_old_way(pilot, app, id):
    """The pre-T717 helper, kept VERBATIM as the control.

    Without it, the new helper's arm would prove only that clicking works on an
    idle box — which the old one also did. This is the thing that has to fail.
    """
    button = app.screen.query_one(id, Button)
    button.scroll_visible(immediate=True, animate=False)
    await pilot.pause()
    await pilot.click(id)
    await pilot.pause(.35)


def _defer_the_scroll(monkeypatch, seconds: float = 0.2) -> None:
    """Make `scroll_visible` land `seconds` later instead of at once.

    This is what a loaded box does to the real helper: the scroll is requested,
    and the region has not moved by the time the click is computed. Injecting it
    turns a load-dependent failure into a deterministic one, so the guard can be
    a test rather than a rate.

    🔴 IT MUST BE A TIMER, NOT A CHAIN OF REFRESHES, AND THAT COST ME AN ARM.
    My first version deferred through `call_after_refresh` — and
    `Pilot.pause()` waits for the app to go IDLE, so a single pause drained the
    whole chain however deep it was. The control arm then passed alone (where
    the box was slow enough) and failed in a three-file run, which is a flaky
    arm, not a guard. A timer is not drained by going idle.

    📌 THE MARGIN IS MEASURED, not assumed: one `pilot.pause()` costs ~16 ms
    here, and `settle_until`'s 25 pauses cost ~851 ms — a 52x separation. At
    0.2 s the old helper (one pause) cannot win and the new helper cannot lose.
    If those numbers ever converge this arm becomes a race; the assertion below
    names the click-time visibility, so it would fail loudly rather than drift.
    """
    from textual.widget import Widget

    real = Widget.scroll_visible

    def deferred(self, *args, **kwargs):
        self.set_timer(seconds, lambda: real(self, *args, **kwargs))

    monkeypatch.setattr(Widget, "scroll_visible", deferred)


@pytest.mark.asyncio
async def test_the_old_click_helper_fails_when_the_scroll_is_slow(tmp_path, app_fixture, monkeypatch):
    """THE CONTROL: one pause is not enough, and this proves it deterministically."""
    from textual.pilot import OutOfBounds

    app = app_fixture()
    app.hook_config.project_path.write_text("{broken")
    async with app.run_test(size=(120, 50)) as pilot:
        app.push_screen(HooksScreen())
        await pilot.pause()
        area = app.screen.query_one("#hook-repair-json", TextArea)
        area.load_text('{"version": 1, "hooks": []}')

        # 🔴 THE PRECONDITION IS ASSERTED, NOT ASSUMED. This arm only means
        # anything while the target starts OUT of view — if it is already
        # visible, no scroll is needed, the old helper clicks it happily and
        # the arm passes for a reason that has nothing to do with the bug.
        # Measured: with two more hook files in the run the button WAS already
        # in view and this arm went red. Scroll home first, then prove it.
        app.screen.scroll_home(animate=False)
        await pilot.pause()
        button = app.screen.query_one("#hook-repair", Button)
        assert button.region.offset not in app.screen.region, (
            "the target is already visible, so this arm cannot test the scroll"
        )

        _defer_the_scroll(monkeypatch)
        with pytest.raises(OutOfBounds):
            await _click_the_old_way(pilot, app, "#hook-repair")


@pytest.mark.asyncio
async def test_the_click_helper_waits_for_the_scroll_to_land(tmp_path, app_fixture, monkeypatch):
    """THE TREATMENT: the same injected delay, and the helper waits it out."""
    app = app_fixture()
    app.hook_config.project_path.write_text("{broken")
    async with app.run_test(size=(120, 50)) as pilot:
        app.push_screen(HooksScreen())
        await pilot.pause()
        area = app.screen.query_one("#hook-repair-json", TextArea)
        area.load_text('{"version": 1, "hooks": []}')
        _defer_the_scroll(monkeypatch)
        await click(pilot, app, "#hook-repair")        # must not raise
        assert not app.hook_config.snapshot().error, "the repair never landed"
