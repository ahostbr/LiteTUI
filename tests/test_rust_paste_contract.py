"""Source paste-event probe: mount PromptInput only, never LiteTUI/runtime."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.widgets._input import Selection

from litetui.widgets import PromptInput


def test_focused_prompt_consumes_bracketed_paste_before_app_handler():
    class InputApp(App):
        def __init__(self):
            super().__init__()
            self.bubbled_pastes = 0

        def compose(self) -> ComposeResult:
            yield PromptInput(id="prompt")

        def on_paste(self, event: events.Paste) -> None:
            self.bubbled_pastes += 1

    async def check():
        async with InputApp().run_test() as pilot:
            widget = pilot.app.query_one(PromptInput)
            cases = [
                ("one\r\ntwo", "aboneef"),
                ("one\ntwo", "aboneef"),
                ("\nrest", "abef"),
                ("one\u2028two", "aboneef"),
                ("", "abcdef"),
            ] + [("one" + chr(code) + "two", "aboneef")
                 for code in (10, 13, 11, 12, 28, 29, 30, 133, 8232, 8233)]
            for text, expected in cases:
                widget.value = "abcdef"
                widget.selection = Selection(2, 4)
                widget.post_message(events.Paste(text))
                await pilot.pause()
                assert widget.value == expected
                assert pilot.app.bubbled_pastes == 0
    asyncio.run(check())


def test_source_os_paste_inserts_at_active_cursor_without_deleting_selection():
    # Execute the actual source method against a minimal widget host. No LiteTUI
    # construction, clipboard access, runtime or duplicated normalizer is used.
    from textual.widgets import Input
    source = Path(__file__).resolve().parents[1] / "src/litetui/app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LiteTUI")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_insert_into_input")
    namespace = {"Input": Input}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)  # noqa: S102 — checked-in method only, no user input
    insert = namespace["_insert_into_input"]

    class InputApp(App):
        def compose(self) -> ComposeResult:
            yield PromptInput(id="message-input")

    async def check():
        async with InputApp().run_test() as pilot:
            widget = pilot.app.query_one(PromptInput)
            for anchor, cursor, expected in [(2, 4, "abcdone  twoef"), (4, 2, "abone  twocdef")]:
                widget.value = "abcdef"
                widget.selection = Selection(anchor, cursor)
                assert insert(pilot.app, "one\r\n \r\n two") == 8
                assert widget.value == expected
                assert widget.cursor_position == cursor + 8
                assert widget.selection.is_empty
            for text in ("", "\r\n \n\t\r\n"):
                widget.value = "abcdef"
                widget.selection = Selection(2, 4)
                assert insert(pilot.app, text) == 0
                assert widget.selection == Selection(2, 4)
    asyncio.run(check())


def test_ctrl_c_copies_input_or_bubbles_to_quit_help_without_quitting():
    class InputApp(App):
        def __init__(self):
            super().__init__()
            self.copied = []
            self.quit_help = 0

        def compose(self) -> ComposeResult:
            yield PromptInput(id="prompt")

        def copy_to_clipboard(self, text: str) -> None:
            self.copied.append(text)  # Deliberate OS/terminal boundary replacement.

        def action_help_quit(self) -> None:
            self.quit_help += 1

    async def check():
        async with InputApp().run_test() as pilot:
            widget = pilot.app.query_one(PromptInput)
            widget.value = "selected 雪"
            widget.select_all()
            await pilot.press("ctrl+c")
            assert pilot.app.copied == ["selected 雪"]
            widget.cursor_position = len(widget.value)
            await pilot.press("ctrl+c")
            assert pilot.app.quit_help == 1
            assert pilot.app.copied == ["selected 雪"]
            assert widget.value == "selected 雪"
    asyncio.run(check())
