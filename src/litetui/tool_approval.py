"""The human approval boundary for sensitive tool calls."""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from litetui.tool_policy import PolicyDecision, approval_preview


class ToolApprovalScreen(ModalScreen[bool]):
    """Approve one call, once.  Dismissal and Escape always deny."""

    BINDINGS = [Binding("escape", "deny", "Deny", show=False)]

    DEFAULT_CSS = """
    ToolApprovalScreen {
        align: center middle;
        background: $background 70%;
    }
    #tool-approval-box {
        width: 78;
        max-width: 92%;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #tool-approval-title {
        color: $warning;
        text-style: bold;
        margin-bottom: 1;
    }
    #tool-approval-meta {
        color: $text-muted;
        margin-bottom: 1;
    }
    #tool-approval-args {
        height: auto;
        max-height: 20;
        padding: 1;
        background: $surface-darken-1;
        color: $text;
        overflow-y: auto;
    }
    #tool-approval-actions {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    #tool-approval-actions Button { margin-left: 1; }
    """

    def __init__(self, tool_name: str, args: dict, decision: PolicyDecision):
        super().__init__()
        self.tool_name = tool_name
        self.args = args
        self.decision = decision

    def compose(self) -> ComposeResult:
        caps = " · ".join(sorted(self.decision.capabilities))
        with Vertical(id="tool-approval-box"):
            yield Static(f"Allow `{self.tool_name}` once?", id="tool-approval-title")
            yield Static(
                f"profile: {self.decision.profile}  ·  authority: {caps}\n"
                f"{self.decision.reason}",
                id="tool-approval-meta",
            )
            yield Static(approval_preview(self.args), id="tool-approval-args")
            with Horizontal(id="tool-approval-actions"):
                yield Button("Deny", id="tool-approval-deny")
                yield Button("Allow once", variant="warning", id="tool-approval-allow")

    @on(Button.Pressed, "#tool-approval-allow")
    def _allow(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#tool-approval-deny")
    def _deny(self) -> None:
        self.dismiss(False)

    def action_deny(self) -> None:
        self.dismiss(False)
