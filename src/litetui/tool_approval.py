"""The human approval boundary for sensitive tool calls."""
from __future__ import annotations

from dataclasses import dataclass

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Static

from litetui.side_panel import SwapButton, close_dialog
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

    # 🔴 ONLY THE SCREEN-LEVEL RULES LIVE HERE NOW. Textual SCOPES DEFAULT_CSS to
    # the class that declares it, so every `#tool-approval-*` rule written here
    # applied ONLY inside this screen — and the same body mounted in a SidePanel
    # would have rendered completely unstyled. Not a crash, not a test failure:
    # an approval dialog that looks like plain text, on the one surface where
    # "looks wrong" and "is a different dialog" are hard to tell apart.
    # The dimming overlay and centering ARE the modal's own, so they stay.
    DEFAULT_CSS = """
    ToolApprovalScreen {
        align: center middle;
        background: $background 70%;
    }
    """

    def __init__(self, tool_name: str, args: dict, decision: PolicyDecision):
        super().__init__()
        self.tool_name = tool_name
        self.args = args
        self.decision = decision

    def compose(self) -> ComposeResult:
        yield ToolApprovalBody(self.tool_name, self.args, self.decision)

    def action_deny(self) -> None:
        self.dismiss(DENIED)


class ToolApprovalBody(Vertical):
    """The approval content, host-agnostic — modal OR sidebar.

    🔴 `ToolApprovalScreen` IS NOT REPLACED. It is still a distinct ModalScreen
    with its own DEFAULT_CSS, because that CSS carries the dimming overlay
    (`background: $background 70%`) and the centering that make the modal a
    modal. A conversion that routed it through the generic host would have
    changed what the approval LOOKS like on the default setting.

    ⚠️ THE TRI-STATE IS NOT TOUCHED. `ToolApproval.__bool__` returns `approved`,
    and DENIED is FALSY — that is the safety, not a convenience. Modelling this
    as strings ("deny"/"once"/"always") reads perfectly and is catastrophic,
    because `"deny"` is truthy and `if not answer:` would stop firing. The host
    returns None on cancel, which is falsy too, so the whole conversion keeps
    failing closed. Do not tidy this into an enum.
    """

    # The CONTENT styling, moved down from the screen so it applies in BOTH
    # hosts. `width` is the one value that had to change: it was a flat `78`,
    # which is wider than the sidebar strip (max 60). `100%` with a `max-width`
    # fills the panel and still caps the modal at its original width.
    # 🔴 THE BODY *IS* THE BOX. It does not wrap one.
    #
    # The first version nested a `Vertical(id="tool-approval-box")` inside a
    # `Widget` body, which added ONE level of DOM depth over the original — and
    # that level SWALLOWED MOUSE CLICKS. `get_widget_at` on the Deny button's
    # own centre returned `ToolApprovalBody`, not the Button.
    #
    # ⚠️ EVERY KEYBOARD PATH STILL PASSED. Escape denied, `Button.press()`
    # resolved, focus moved correctly. Only a real click failed, and the three
    # tests that caught it were the pre-existing ones that click — none of the
    # tests I wrote for ConfirmStop or Picker click anything, so my own suites
    # would have shipped this.
    #
    # 📌 THE BASE CLASS WAS NEVER THE CAUSE, and I checked rather than assuming:
    # `Widget` -> `Container` changed nothing while the extra level was present,
    # and with the level gone a `Widget` base works fine. Removing the level is
    # what fixed it HERE. Measured both ways, because "I swapped two things and
    # it started working" is not a diagnosis.
    #
    # ⚠️ CORRECTION TO MY OWN FIRST WRITE-UP: this used to end "it is the DEPTH,
    # not the type", stated as a general law. IT IS NOT ONE. T082 wrapped
    # `AskUserQuestionBody`'s compose in an extra level as a deliberate mutation
    # and that dialog's click test STAYED GREEN — so an extra level is not
    # sufficient to swallow clicks. Something more specific to this dialog is
    # involved (an auto-sized body under the screen's `align: center middle` is
    # the obvious suspect) and I have NOT isolated it.
    # ⇒ Established: removing the level fixed THIS case and the base class did
    # not. Not established: that depth alone explains it anywhere else.
    #
    # `width: 78` is the original value and stays the basis; `max-width: 100%`
    # is what lets it fit a 60-column sidebar.
    DEFAULT_CSS = """
    ToolApprovalBody {
        width: 78;
        max-width: 100%;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
        layout: vertical;
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
        yield SwapButton()
        with Horizontal(id="tool-approval-actions"):
            yield Button("Deny", id="tool-approval-deny")
            yield Button("Allow once", variant="warning", id="tool-approval-allow")
            yield Button("Always allow", variant="error", id="tool-approval-always")

    # ── state carry across a live host swap ──────────────────────────────────
    def get_state(self) -> dict:
        """Which button was focused, and how far the preview was scrolled.

        🔴 THE FOCUSED BUTTON IS SAFETY STATE HERE, not a nicety. Deny is
        composed FIRST so a blind Enter refuses; if a swap silently returned
        focus to the default while the user had deliberately moved to "Allow
        once", the next Enter would answer a different question than the one
        they were looking at. Restoring it keeps the user's own choice under
        their finger.
        """
        try:
            focused = self.screen.focused
        except Exception:
            focused = None
        args = self.query_one("#tool-approval-args")
        return {
            "focused_id": getattr(focused, "id", None),
            "args_scroll_y": getattr(args, "scroll_offset", None)
            and args.scroll_offset.y,
        }

    def set_state(self, state: dict) -> None:
        y = state.get("args_scroll_y")
        if y:
            self.query_one("#tool-approval-args").scroll_to(y=y, animate=False)
        fid = state.get("focused_id")
        if fid:
            self.query_one(f"#{fid}").focus()

    @on(Button.Pressed, "#tool-approval-allow")
    def _allow(self) -> None:
        close_dialog(self, ONCE)

    @on(Button.Pressed, "#tool-approval-always")
    def _always(self) -> None:
        close_dialog(self, ALWAYS)

    @on(Button.Pressed, "#tool-approval-deny")
    def _deny(self) -> None:
        close_dialog(self, DENIED)
