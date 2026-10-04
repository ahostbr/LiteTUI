"""The human approval boundary for sensitive tool calls."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from litetui.side_panel import SwapButton, close_dialog
from litetui.tool_policy import PolicyDecision, approval_preview

#: How long a headless child waits for its host to answer one approval.
#:
#: MEASURED, NOT PICKED, and the two candidates were both wrong. The
#: LiteSuite adapter's COMMAND_TIMEOUT_MS is 60s, but that governs an RPC
#: COMMAND (request in, response out) and nothing human is on the other end
#: of one; borrowing it would give a person 60 seconds to read a tool call
#: and decide. The sibling wire ask, `user_input_requested` (T558-A), waits
#: FOREVER -- correct for a question that is the whole point of the turn,
#: wrong here, because a host that never renders the card would hang a
#: headless child with nothing to show for it.
#:
#: 300s is the same budget a background task already gets in this app. It is
#: EMITTED with the request (`timeout_s`) rather than kept private, so the
#: host can show a countdown and does not have to guess when its answer
#: stops being wanted.
APPROVAL_TIMEOUT_S = 300.0
#: T1049-B (plan S2, clock C1): set by agent_launcher.start_headless_child on a
#: supervised child. "0" = no deadline (an OWNER parent's keypress, Sentinel
#: d47235da: never a timeout-deny); otherwise the parent's relay timeout + 60 s.
APPROVAL_TIMEOUT_ENV = "LITETUI_APPROVAL_TIMEOUT_S"


def approval_timeout_s() -> float | None:
    """This process's approval deadline; None = unbounded. Read at call time."""
    import os
    raw = os.environ.get(APPROVAL_TIMEOUT_ENV)
    if raw is None or raw == "":
        return APPROVAL_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return APPROVAL_TIMEOUT_S  # a malformed value keeps the bounded default
    return None if value <= 0 else value



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
        yield Static(
            f"Allow `{self.tool_name}`?", markup=False, id="tool-approval-title"
        )
        yield Static(
            f"profile: {self.decision.profile}  ·  authority: {caps}\n"
            f"{self.decision.reason}",
            markup=False,
            id="tool-approval-meta",
        )
        yield Static(
            approval_preview(self.args), markup=False, id="tool-approval-args"
        )
        # Say what "always" actually covers.  The stored rule is keyed by
        # tool AND authority, so it is much narrower than the button label
        # suggests on its own -- and a human who reads "Always allow" as
        # "never ask about this tool again" has agreed to something else.
        yield Static(
            f"Always = `{self.tool_name}` at this authority ({caps}); "
            "a wider request asks again.",
            markup=False,
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


# ── the wire ask (T577) ──────────────────────────────────────────────


def _approval_registry(app) -> dict:
    """Pending wire approvals for THIS app, keyed by id.

    Per app instance, never module level: the suite builds many apps in one
    process and a shared registry would let one app's answer resolve another's
    question.
    """
    reg = getattr(app, "_approval_waiters", None)
    if reg is None:
        reg = {}
        app._approval_waiters = reg
    return reg


def resolve_over_rpc(app, approval_id: str, allow: bool, remember: bool = False) -> bool:
    """Answer a pending wire approval. False when the id is unknown or done.

    AN UNKNOWN ID IS AN ERROR AT THE CALLER, NOT A NO-OP HERE -- the same
    doctrine `ask_user_question.resolve_over_rpc` states: the two ways to reach
    this with a stale id are a late answer and a typo, and both leave the host
    believing it approved something that is still waiting, or worse, still
    waiting on something that already denied and ended the turn.
    """
    fut = _approval_registry(app).get(approval_id)
    if fut is None or fut.done():
        return False
    from litetui import approval_authority
    record = getattr(app, "_approval_authority_records", {}).get(approval_id)
    if record is not None:
        deadline = record["authority"].deadline
        if deadline is not None and approval_authority.time.monotonic() >= deadline:
            return False
    if record is not None and not approval_authority.settle(
            app, approval_id, outcome="approved" if allow else "denied"):
        return False
    fut.set_result(ALWAYS if (allow and remember) else (ONCE if allow else DENIED))
    return True


def take_spawner_answer(app, msg: dict) -> bool:
    """Consume addressed unexpired mail from an eligible frozen exact ID.

    Registration edits cannot change a pending permission or its60/120s clock.
    The ordinary human RPC host answer path remains separate.
    """
    from litetui import approval_relay, harness

    seat_id = getattr(getattr(app, "seat", None), "agent_id", None)
    if not seat_id or msg.get("to") != seat_id or harness._expired(msg):
        return False
    text = msg.get("body") or (msg.get("payload") or {}).get("text") or ""
    match = approval_relay._ANSWER.fullmatch(str(text).strip())
    if match is None:
        return False
    from litetui import approval_authority
    ident = match.group(2)
    future = _approval_registry(app).get(ident)
    if future is None or future.done() or not approval_authority.can_answer(app, ident, msg):
        return False
    allow = match.group(1) == "APPROVE"
    if not approval_authority.settle(app, ident, outcome="approved" if allow else "denied",
                                     answerer_id=msg.get("from")):
        return False
    future.set_result(ONCE if allow else DENIED)
    return True


async def approve_over_rpc(app, name: str, args, decision: PolicyDecision,
                           *, timeout: float | None = None):
    """Ask the HOST to approve one tool call. None when nobody answered.

    \U0001f534 NONE AND DENIED ARE DIFFERENT ANSWERS AND THE CALLER MUST NOT MERGE
    THEM. `DENIED` is a person choosing; `None` is a host that never spoke. They
    lead to the same refusal -- a call nobody approved does not run -- but NOT to
    the same transcript line, because "the user refused" tells the model to stop
    asking and "nobody was listening" tells it something is broken. The old
    `not answer` contract would have collapsed the two, which is precisely the
    silent policy change `test_show_dialog_is_left_alone_on_purpose` was written
    to prevent when this door was still unbuilt.

    Awaited ON THE EVENT LOOP, so the waiter is an asyncio Future. The rpc
    reader is a thread but dispatches through `app.call_from_thread`
    (rpc.py `_reader_loop`), so `resolve_over_rpc` already runs on this loop and
    no cross-thread primitive is needed. `ask_user_question` uses a
    `threading.Event` because ITS caller runs on a worker thread; copying that
    here would be cargo cult.
    """
    # READ AT CALL TIME, not bound as a default: a module constant captured
    # in a signature cannot be changed by anything, including an arm that
    # needs the timeout to be short enough to measure.
    # T1049-B: None = this process's deadline (approval_timeout_s); <= 0 = none at
    # all (an owner parent relaying a child's CONFIRM to Ryan's keypress).
    timeout = approval_timeout_s() if timeout is None else (None if timeout <= 0 else timeout)
    approval_id = "appr-" + uuid4().hex[:12]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    spawners = getattr(app, "_rpc_approval_spawners", None)
    if spawners is None:
        spawners = app._rpc_approval_spawners = {}
    from litetui import approval_authority, approval_relay, seat_authority

    status = "cancelled"
    try:
        _approval_registry(app)[approval_id] = fut
        spawners[approval_id] = approval_relay.current_spawner(app)
        approval_authority.create(app, approval_id, approver=spawners[approval_id],
                                  route=seat_authority.confirm_route(app), timeout=timeout)
        app._rpc_emit({
            "type": "tool_approval_requested",
            "id": approval_id,
            "tool": name,
            "input": args,
            "profile": str(getattr(app, "_active_tool_profile", None)),
            "why": decision.reason,
            "timeout_s": timeout,
        })
        try:
            answer = await approval_authority.wait_for_answer(app, approval_id, fut)
            approval_authority.raise_if_cancelled()
            return answer if approval_authority.persistence_ready(app, approval_id) else None
        except TimeoutError:
            status = "timeout"
            return None
        except asyncio.CancelledError:
            return None
    except approval_authority.ApprovalAuditError:
        status = "audit-error"
        return None
    finally:
        _approval_registry(app).pop(approval_id, None)
        spawners.pop(approval_id, None)
        approval_authority.close(app, approval_id, status)
