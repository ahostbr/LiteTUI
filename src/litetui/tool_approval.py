"""The human approval boundary for sensitive tool calls."""
from __future__ import annotations

from dataclasses import dataclass

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from litetui.tool_policy import PolicyDecision, approval_preview


@dataclass(frozen=True)
class ToolApproval:
    """One human answer to one approval prompt.

    `__bool__` IS THE SAFETY HERE, not a convenience.  This type replaced a
    bare `bool`, so every truthiness check written against the old return value
    -- at the call site, in tests, in anything added later out of habit --
    keeps working AND keeps failing closed.

    🔴 THE MIGRATION THIS SHAPE EXISTS TO PREVENT: modelling the tri-state as
    strings ("deny" / "once" / "always") reads perfectly and is silently
    catastrophic, because `"deny"` is TRUTHY.  `if not approved:` would stop
    firing and the Deny button would start RUNNING the tool -- a change that
    inverts the guard it was meant to strengthen, while every test that only
    checks the allow path still passes.

    `push_screen_wait` also yields None when a screen is dismissed without a
    value (app teardown), and None is falsy too, so that path stays a denial.
    """

    approved: bool
    remember: bool = False

    def __bool__(self) -> bool:
        return self.approved


#: Refuse this call.  Escape, the Deny button and bare dismissal all produce it.
DENIED = ToolApproval(approved=False)
#: Run this call; ask again the next time.
ONCE = ToolApproval(approved=True)
#: Run this call and record a standing rule so the same question is not re-asked.
ALWAYS = ToolApproval(approved=True, remember=True)


class ToolApprovalScreen(ModalScreen[ToolApproval]):
    """Deny, allow once, or allow always.  Dismissal and Escape always deny.

    Deny is composed FIRST so it holds initial focus: a blind Enter on a modal
    that appeared mid-turn refuses, and that ordering is load-bearing rather
    than cosmetic.  "Always allow" carries the `error` variant because it is
    the most consequential button on the screen, not because it is a refusal.
    """

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
    #tool-approval-hint {
        color: $text-muted;
        margin-top: 1;
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
            yield Static(f"Allow `{self.tool_name}`?", id="tool-approval-title")
            yield Static(
                f"profile: {self.decision.profile}  ·  authority: {caps}\n"
                f"{self.decision.reason}",
                id="tool-approval-meta",
            )
            yield Static(approval_preview(self.args), id="tool-approval-args")
            # Say what "always" actually covers.  The stored rule is keyed by
            # tool AND authority, so it is much narrower than the button label
            # suggests on its own -- and a human who reads "Always allow" as
            # "never ask about this tool again" has agreed to something else.
            yield Static(
                f"Always = `{self.tool_name}` at this authority ({caps}); "
                "a wider request asks again.",
                id="tool-approval-hint",
            )
            with Horizontal(id="tool-approval-actions"):
                yield Button("Deny", id="tool-approval-deny")
                yield Button("Allow once", variant="warning", id="tool-approval-allow")
                yield Button("Always allow", variant="error", id="tool-approval-always")

    @on(Button.Pressed, "#tool-approval-allow")
    def _allow(self) -> None:
        self.dismiss(ONCE)

    @on(Button.Pressed, "#tool-approval-always")
    def _always(self) -> None:
        self.dismiss(ALWAYS)

    @on(Button.Pressed, "#tool-approval-deny")
    def _deny(self) -> None:
        self.dismiss(DENIED)

    def action_deny(self) -> None:
        self.dismiss(DENIED)
