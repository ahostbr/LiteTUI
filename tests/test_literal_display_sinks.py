"""T0324-B: real display sinks must treat external strings as data."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import OptionList, Static

from litetui.approval_relay import HumanApproval
from litetui.hooks_screen import HooksEditor
from litetui.picker import PickerBody
from litetui.widgets import CompactionCard, FoldBlock

PAYLOAD = "[bold]literal[/bold] [broken=$true]"


class SinkApp(App):
    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def compose(self):
        yield self.widget


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["#hook-error", "#hook-status", "#hook-result"])
async def test_hook_output_and_paths_are_literal(selector):
    async with SinkApp(HooksEditor()).run_test() as pilot:
        target = pilot.app.query_one(selector, Static)
        target.update(PAYLOAD)
        await pilot.pause()
        assert target.render().plain == PAYLOAD


@pytest.mark.asyncio
async def test_approval_tool_name_is_literal():
    async with SinkApp(HumanApproval("appr-123", PAYLOAD)).run_test() as pilot:
        target = pilot.app.query_one(HumanApproval).query(Static).first()
        assert PAYLOAD in target.render().plain


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["title", "hint", "row"])
async def test_picker_title_hint_and_model_or_convo_row_are_literal(part):
    title = PAYLOAD if part == "title" else "title"
    hint = PAYLOAD if part == "hint" else "hint"
    row = PAYLOAD if part == "row" else "row"
    async with SinkApp(PickerBody(title, [("row", row)], hint=hint)).run_test() as pilot:
        assert pilot.app.query_one("#picker-title", Static).render().plain == title
        assert pilot.app.query_one("#picker-hint", Static).render().plain == hint
        options = pilot.app.query_one(OptionList)
        assert options._get_visual(options.get_option_at_index(0)).plain == row


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["header", "plan"])
async def test_fold_header_and_compaction_plan_are_literal(part):
    card = CompactionCard(PAYLOAD if part == "plan" else "plan", "prompt")
    fold = FoldBlock(PAYLOAD if part == "header" else "header", "body")
    async with SinkApp(card).run_test() as pilot:
        assert card._plan.render().plain == (PAYLOAD if part == "plan" else "plan")
        await pilot.app.mount(fold)
        label = PAYLOAD if part == "header" else "header"
        assert label in fold.header.render().plain
        fold.set_expanded(True)
        assert label in fold.header.render().plain


def make_app(monkeypatch):
    from litetui.app import LiteTUI
    app = LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app._inbox_monitor = lambda: None
    monkeypatch.setattr("litetui.cron.monitor", lambda _app: None)
    return app


@pytest.mark.asyncio
async def test_notification_defaults_literal_and_explicit_markup_survives(monkeypatch):
    from textual.widgets._toast import Toast
    app = make_app(monkeypatch)
    async with app.run_test(notifications=True) as pilot:
        app.notify(PAYLOAD, timeout=30)
        await pilot.pause()
        assert next(iter(app.query(Toast))).render().plain == PAYLOAD
        app.notify("[bold]intentional[/bold]", markup=True, timeout=30)
        await pilot.pause()
        assert any(toast.render().plain == "intentional" for toast in app.query(Toast))


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["#set-error", "#voice-status"])
async def test_settings_validation_and_voice_errors_are_literal(monkeypatch, selector):
    from litetui.settings_screen import SettingsScreen
    app = make_app(monkeypatch)
    async with app.run_test(size=(120, 50)) as pilot:
        app.push_screen(SettingsScreen(app.settings))
        await pilot.pause()
        target = app.screen.query_one(selector, Static)
        target.update(PAYLOAD)
        assert target.render().plain == PAYLOAD


@pytest.mark.asyncio
async def test_model_identity_paths_and_errors_are_literal(monkeypatch):
    from litetui.plugins.model_switch import ModelConfigScreen
    app = make_app(monkeypatch)
    app.backend = SimpleNamespace(name="lmstudio", request_overrides=lambda _key: {})
    app.model_rows = {PAYLOAD: SimpleNamespace(source=PAYLOAD, path=Path(PAYLOAD),
        extra_paths=[Path(PAYLOAD)], modalities=[PAYLOAD], loaded=False)}
    async with app.run_test(size=(120, 50)) as pilot:
        app.push_screen(ModelConfigScreen(PAYLOAD))
        await pilot.pause()
        assert PAYLOAD in app.screen.query_one("#set-title", Static).render().plain
        # TabbedContent relocates TabPane content under a generated content ID.
        displayed = [widget.render().plain for widget in app.screen.query(Static)]
        for prefix in ("Source: ", "Path: ", "Also at: ", "Modalities: "):
            value = str(Path(PAYLOAD)) if prefix in ("Path: ", "Also at: ") else PAYLOAD
            assert prefix + value in displayed, displayed
        target = app.screen.query_one("#mc-error", Static)
        target.update(PAYLOAD)
        assert target.render().plain == PAYLOAD


@pytest.mark.asyncio
async def test_invalid_color_notification_is_literal(monkeypatch):
    from textual.widgets import Input
    from textual.widgets._toast import Toast

    from litetui.colorpicker import ColorPickerBody
    app = make_app(monkeypatch)
    async with app.run_test(notifications=True) as pilot:
        color = ColorPickerBody()
        await app.mount(color)
        field = color.query_one("#cp-hex", Input)
        color.on_input_submitted(Input.Submitted(field, PAYLOAD))
        await pilot.pause()
        assert PAYLOAD in next(iter(app.query(Toast))).render().plain


@pytest.mark.asyncio
async def test_help_command_arguments_remain_literal():
    from litetui.plugins.help_plugin import HelpBody
    reference = "/skills [name] /model [n] /think [level] /compact [hint]"
    async with SinkApp(HelpBody(reference)).run_test() as pilot:
        assert pilot.app.query_one("#help-body", Static).render().plain == reference


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["header", "collapsed-preview"])
async def test_user_message_border_title_is_literal(part):
    from litetui.widgets import UserMessage
    message = UserMessage(PAYLOAD, header=PAYLOAD if part == "header" else None)
    async with SinkApp(message).run_test() as pilot:
        if part == "collapsed-preview":
            message.set_collapsed(True)
        await pilot.pause()
        assert PAYLOAD in message._border_title.plain
        message.mark_delivered()
        assert PAYLOAD in message._border_title.plain


@pytest.mark.asyncio
async def test_app_user_bubble_header_is_literal(monkeypatch):
    app = make_app(monkeypatch)
    async with app.run_test() as pilot:
        message = app._user_bubble("text", False, header=PAYLOAD)
        await pilot.pause()
        assert PAYLOAD in message._border_title.plain
