"""Real Textual mounting for Claude approval ownership; no live model calls."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from litetui import tool_policy
from litetui.claude_turn import approval_dialog
from litetui.tool_approval import ToolApprovalBody


class ApprovalApp(App):
    def __init__(self, style):
        super().__init__()
        self.settings = SimpleNamespace(dialog_style=style, dialog_side="right")

    def compose(self) -> ComposeResult:
        yield Static("Claude test")


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["modal", "sidebar"])
async def test_cancel_closes_owned_approval_view(style):
    app = ApprovalApp(style)
    decision = tool_policy.evaluate("strict", tool_policy.WRITE_POLICY, {"path": "x.txt"}, ".")
    async with app.run_test() as pilot:
        task = asyncio.create_task(approval_dialog(app, "Write", {"file_path": "x.txt"}, decision))
        await pilot.pause()
        assert len(app.screen.query(ToolApprovalBody)) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await pilot.pause()
        assert len(app.screen.query(ToolApprovalBody)) == 0
