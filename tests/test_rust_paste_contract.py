"""Source paste-event probe: mount PromptInput only, never LiteTUI/runtime."""
from __future__ import annotations

import asyncio

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
