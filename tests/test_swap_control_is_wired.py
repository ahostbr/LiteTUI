"""The swap control is WIRED, in every body and on both hosts. T222.

Ryan: "find and fix all possible buttons that should be doing this swap on all
GUIs". This file is the gate on the answer to that, and it exists because the
existing gate could not see the defect.

🔴 WHAT WAS ACTUALLY BROKEN, AND WHY A GREEN SUITE DID NOT SAY SO.
`test_swap_button_in_real_dialogs.py` proved the control was PRESENT in all four
dialogs, that it labelled itself correctly, and that a swap does not answer the
future. Every one of those assertions reaches the mechanism by calling
`request_swap(...)` DIRECTLY. Not one of them presses the button. So the suite
measured the function UNDER the control and never the control — and the press
was wired in `dialog_demo`, `tool_list`, `loop_list` and `mcp_list` by a private
`@on(Button.Pressed, "#<its-own-id>")` in each, and in NONE of the four real
dialogs. Pressing "Dock to side" on a tool approval did nothing at all.

⇒ EVERY ARM BELOW PRESSES. `btn.press()` is the whole point; an arm here that
calls `request_swap` would re-create the blindness this file was written for.

📌 AND THE ANSWER IS ASSERTED ON BOTH SIDES OF EVERY SWITCH. "No inert button"
is also satisfied by a button hidden everywhere, and "the swap does not answer"
is also satisfied by a swap that does nothing — so each of those is paired with
the arm that shows the other polarity actually happens.
"""
from __future__ import annotations

import sys
import threading
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _settle import settle_until
from litetui import app as m
from litetui.ask_user_question import (
    AskUserQuestionBody, AskUserQuestionScreen, QuestionState,
)
from litetui.loop_list import LoopListBody
from litetui.mcp_list import MCPListBody
from litetui.colorpicker import ColorPickerBody, ColorPickerScreen
from litetui.picker import PickerBody, PickerScreen
from litetui.scheduler import Job
from litetui.plugins.scheduler_ui import (
    CalendarBody, CalendarScreen, DayBody, DayScreen, JobBody, JobScreen,
)
from litetui.plugins.help_plugin import HelpBody, HelpScreen
from litetui.plugins.model_switch import ModelConfigBody, ModelConfigScreen
from litetui.side_panel import (
    DialogController, SidePanel, SwapButton, close_dialog, present_dialog,
    show_dialog,
)
from litetui.tool_approval import ToolApprovalBody, ToolApprovalScreen
from litetui.tool_list import ToolListBody
from litetui.tool_policy import INTERACTIVE, WORKSPACE_WRITE, PolicyDecision
from litetui.widgets import ConfirmStop, ConfirmStopBody

ROOT = Path(__file__).resolve().parent.parent

HELP_TEXT = "a line of help" + chr(10) + "and another"

A_DAY = date(2026, 8, 17)
JOBS = [Job(prompt="say hi", schedule="0 9 * * 1-5", label="morning")]


def _decision():
    return PolicyDecision("confirm", INTERACTIVE, frozenset({WORKSPACE_WRITE}), "why")


def _question():
    return [QuestionState(label="one", question="q1?",
                          options=[{"title": "a", "description": ""}])]


def make_app(monkeypatch=None):
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


#: THE ONE LIST, and it carries BOTH factories per dialog — the body a sidebar
#: mounts and the ORIGINAL ModalScreen a router pushes. Built as pairs from one
#: set of arguments, for the reason `picker.pick` gives: two factories built at
#: two places are two chances to drift.
def _pairs():
    states, done, box = _question(), threading.Event(), []
    return [
        ("picker",
         lambda: PickerBody("Pick a model", [("a", "model-a"), ("b", "model-b")]),
         lambda: PickerScreen("Pick a model", [("a", "model-a"), ("b", "model-b")])),
        ("confirm_stop", ConfirmStopBody, ConfirmStop),
        ("tool_approval",
         lambda: ToolApprovalBody("write", {"path": "x"}, _decision()),
         lambda: ToolApprovalScreen("write", {"path": "x"}, _decision())),
        ("ask_user_question",
         lambda: AskUserQuestionBody(states, done, box),
         lambda: AskUserQuestionScreen(states, done, box)),
        # ── T232: the seven screens that had no body/sidebar split at all ────
        ("help",
         lambda: HelpBody(HELP_TEXT), lambda: HelpScreen(HELP_TEXT)),
        ("colorpicker",
         lambda: ColorPickerBody("#808080", [], "primary"),
         lambda: ColorPickerScreen("#808080", [], "primary")),
        ("calendar",
         lambda: CalendarBody(JOBS), lambda: CalendarScreen(JOBS)),
        ("day",
         lambda: DayBody(JOBS, A_DAY), lambda: DayScreen(JOBS, A_DAY)),
        ("job",
         lambda: JobBody(None, "0 9 * * *"),
         lambda: JobScreen(None, "0 9 * * *")),
        ("model_config",
         lambda: ModelConfigBody("a-model"), lambda: ModelConfigScreen("a-model")),
    ]


PAIRS = _pairs()
IDS = [n for n, _, _ in PAIRS]

#: The three list panels. They have no ModalScreen of their own — `open_dialog`
#: gives them a controller in EITHER host — so they are the other generation of
#: this feature, and they had a private button and a private handler each.
PANELS = [("tool_list", ToolListBody), ("loop_list", LoopListBody),
          ("mcp_list", MCPListBody)]
PANEL_IDS = [n for n, _ in PANELS]


# ── the press, in the host that has a controller ────────────────────────────

@pytest.mark.parametrize("name,body,_modal", PAIRS, ids=IDS)
@pytest.mark.asyncio
async def test_pressing_the_control_swaps_in_a_sidebar(name, body, _modal):
    """The arm that went red first, and the simplest statement of the bug:
    `swappable()` said True, the label was right, and the press did nothing."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, body, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))
        btn = a.screen.query_one(SidePanel).query_one(SwapButton)
        assert btn.swappable() is True, f"{name}: no controller in a sidebar?"

        btn.press()
        await settle_until(pilot, lambda: ctrl.style == "modal")
        assert ctrl.style == "modal", f"{name}: PRESSING the control did nothing"
        assert ctrl.pending, f"{name}: THE PRESS ANSWERED THE DIALOG"


@pytest.mark.parametrize("name,body", PANELS, ids=PANEL_IDS)
@pytest.mark.asyncio
async def test_pressing_the_control_swaps_in_a_panel(name, body):
    """The three panels DID swap before this change, by four private handlers.
    They must still swap now that the control owns its own press — this is the
    arm that says the conversion did not trade one generation for the other."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, body, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))
        panel = a.screen.query_one(SidePanel)
        buttons = panel.query(SwapButton)
        assert len(buttons) == 1, f"{name}: expected one shared control, got {len(buttons)}"

        buttons.first().press()
        await settle_until(pilot, lambda: ctrl.style == "modal")
        assert ctrl.style == "modal", f"{name}: PRESSING the control did nothing"
        assert ctrl.pending, f"{name}: THE PRESS ANSWERED THE DIALOG"


@pytest.mark.parametrize("name,body", PANELS, ids=PANEL_IDS)
@pytest.mark.asyncio
async def test_the_shared_control_does_not_squash_the_panel_button_row(name, body):
    """🔴 A RENDERING CLAIM NEEDS A RENDERED MEASUREMENT.

    The panels put the swap control in a Horizontal beside "Close", and
    SwapButton's own `width: 100%` — right for a control on its own line —
    takes the entire row there. `.inline` is the fix; this is the arm that says
    so, because a suite that never looked at it is what shipped the inert
    control in the first place.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, body, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))
        panel = a.screen.query_one(SidePanel)
        swap = panel.query_one(SwapButton)
        close = next(b for b in panel.query("Button")
                     if str(b.label).strip().lower() == "close")
        await settle_until(pilot, lambda: close.size.width > 0 and swap.size.width > 0)
        assert close.size.width > 0, (
            f"{name}: the swap control took the whole row and Close renders at 0 columns"
        )
        assert swap.size.width > 0, f"{name}: the swap control itself renders at 0 columns"
        ctrl.resolve(None)


# ── the press, on the ORIGINAL modal a router pushed ────────────────────────

@pytest.mark.parametrize("name,body,modal", PAIRS, ids=IDS)
@pytest.mark.asyncio
async def test_pressing_dock_to_side_on_a_routed_modal_docks_it(name, body, modal):
    """The branch Ryan is on: `dialog_style` defaults to "modal".

    The modal keeps its identity — same class, same CSS selector, same
    `isinstance` — and gains only a third EXIT. So the assertions are that the
    original screen was really up, that it is gone afterwards, and that the
    caller's own callback survived the move unanswered.
    """
    a = make_app()
    a.settings.dialog_style = "modal"
    async with a.run_test(size=(120, 40)) as pilot:
        seen: list = []
        present_dialog(a, body, modal, seen.append)
        await settle_until(pilot, lambda: bool(a.screen.query(SwapButton)))
        btn = a.screen.query_one(SwapButton)
        assert str(btn.label) == "Dock to side", (
            f"{name}: a modal must offer the SIDEBAR as its destination"
        )
        assert btn.swappable() is True, f"{name}: the routed modal still cannot swap"

        btn.press()
        assert await settle_until(pilot, lambda: bool(a.screen.query(SidePanel))), (
            f"{name}: Dock to side did not open the sidebar"
        )
        assert not a.screen.query(f"{modal().__class__.__name__}"), (
            f"{name}: the modal is still on the stack"
        )
        assert seen == [], f"{name}: THE SWAP ANSWERED THE DIALOG with {seen!r}"

        close_dialog(a.screen.query_one(SidePanel).body, "ANSWER")
        await settle_until(pilot, lambda: bool(seen))
        assert seen == ["ANSWER"], (
            f"{name}: the caller's own callback did not survive the swap: {seen!r}"
        )


@pytest.mark.asyncio
async def test_the_awaited_router_never_hands_the_sentinel_to_its_caller():
    """CONDITION 3, ARM ONE — `_execute_tool` reads `if not answer:`.

    Tool approval is the one dialog whose caller AWAITS, and it is the one where
    a leaked sentinel would matter most: `_execute_tool`'s very next line treats
    a falsy answer as DENY-and-stop-the-turn. `show_dialog` must not return at
    all for a swap — it falls through to the controller — so there is no value
    for that line to misread.
    """
    a = make_app()
    a.settings.dialog_style = "modal"
    async with a.run_test(size=(120, 40)) as pilot:
        out: list = []

        async def _ask():
            out.append(await show_dialog(
                a,
                lambda: ToolApprovalBody("write", {"path": "x"}, _decision()),
                modal_factory=lambda: ToolApprovalScreen("write", {"path": "x"}, _decision()),
            ))

        a.run_worker(_ask(), name="ask")
        await settle_until(pilot, lambda: isinstance(a.screen, ToolApprovalScreen))
        a.screen.query_one(SwapButton).press()
        assert await settle_until(pilot, lambda: bool(a.screen.query(SidePanel))), (
            "the awaited modal did not dock"
        )
        assert out == [], f"the swap RETURNED {out!r} to the awaiting caller"

        close_dialog(a.screen.query_one(SidePanel).body, "APPROVED")
        await settle_until(pilot, lambda: bool(out))
        assert out == ["APPROVED"], f"the awaiting caller got {out!r}"


@pytest.mark.asyncio
async def test_on_stop_answer_never_sees_the_sentinel():
    """CONDITION 3, ARM TWO — `_on_stop_answer` reads `if not stop:`.

    ⚠️ THE SIDEBAR ASSERTION BELOW IS LOAD-BEARING. Without it this arm passes
    against the BROKEN code too: if the press does nothing, the callback is not
    called either, and `got == []` is true for the wrong reason. Proving the
    swap HAPPENED is what makes the silence mean something.
    """
    a = make_app()
    a.settings.dialog_style = "modal"
    async with a.run_test(size=(120, 40)) as pilot:
        got: list = []
        real = a._on_stop_answer

        def spy(v):
            got.append(v)
            return real(v)

        present_dialog(a, ConfirmStopBody, ConfirmStop, spy)
        await settle_until(pilot, lambda: isinstance(a.screen, ConfirmStop))
        a.screen.query_one(SwapButton).press()
        assert await settle_until(pilot, lambda: bool(a.screen.query(SidePanel))), (
            "the swap did not happen, so this arm proves nothing about the callback"
        )
        assert got == [], f"_on_stop_answer was handed {got!r} by a SWAP"
        assert a._stop_requested is False, "a swap requested a stop"


# ── the control is offered exactly where it can act ─────────────────────────

@pytest.mark.asyncio
async def test_the_control_is_hidden_where_it_cannot_act():
    """`swappable()`'s caller, negative polarity.

    A body inside a plain ModalScreen that NO router pushed — ask_user_question's
    `loop is None` fallback, or any future bare `push_screen` — genuinely cannot
    swap. It must not render a control that does nothing.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = PickerScreen("Pick", [("a", "model-a")])
        a.push_screen(screen)
        await settle_until(pilot, lambda: bool(a.screen.query(SwapButton)))
        btn = a.screen.query_one(SwapButton)
        assert btn.swappable() is False, "a bare push has no controller and no router"
        assert btn.display is False, "an inert swap control is still rendered"
        screen.dismiss(None)
        await pilot.pause()


@pytest.mark.parametrize("name,body,modal", PAIRS, ids=IDS)
@pytest.mark.asyncio
async def test_the_control_is_shown_wherever_it_can_act(name, body, modal):
    """`swappable()`'s caller, POSITIVE polarity — both hosts.

    Without this, "no inert button" is satisfied by a button hidden everywhere,
    which is the same feature deleted rather than fixed.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, body, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))
        btn = a.screen.query_one(SidePanel).query_one(SwapButton)
        assert btn.display is True, f"{name}: hidden in a SIDEBAR, where it works"
        ctrl.resolve(None)
        await pilot.pause()

    a2 = make_app()
    a2.settings.dialog_style = "modal"
    async with a2.run_test(size=(120, 40)) as pilot:
        present_dialog(a2, body, modal, None)
        await settle_until(pilot, lambda: bool(a2.screen.query(SwapButton)))
        btn = a2.screen.query_one(SwapButton)
        assert btn.display is True, f"{name}: hidden on a ROUTED MODAL, where it works"


# ── the census: one owner for the press ─────────────────────────────────────

def test_no_body_wires_its_own_swap():
    """🔴 SCAN THE PACKAGE, DO NOT HAND-LIST THE FILES.

    The count that produced the wrong story about this bug came from a grep over
    six files I had already decided were the relevant ones; the package held four
    private handlers, three of which I could not see. This walks src/litetui.

    `request_swap` may be CALLED only by the control that owns the press. A body
    that wires its own is the fifth copy of a rule with four owners, and the next
    body author copies whichever one they open first.
    """
    pkg = ROOT / "src" / "litetui"
    offenders = sorted(
        p.relative_to(pkg).as_posix()
        for p in pkg.rglob("*.py")
        if p.name != "side_panel.py" and "request_swap(" in p.read_text(encoding="utf-8")
    )
    assert offenders == [], (
        "these modules call request_swap themselves; SwapButton owns the press:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.asyncio
async def test_the_control_carries_no_action_so_its_message_is_posted_at_all():
    """🔴 THE ONE REMAINING WAY TO SILENTLY UNWIRE THIS CONTROL.

    `Button.press()` does NOT post `Button.Pressed` when `Button.action` is set —
    it branches to `run_action` instead (textual/widgets/_button.py:355-369). So
    an author who gives SwapButton an action gets a control that animates on
    press, never reaches the handler, and turns nothing red: the exact shape of
    the defect this whole file exists for, re-created one layer down.

    This is an ARM rather than a comment because a comment is read once, and this
    fails on the commit that sets it.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, ConfirmStopBody, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))
        btn = a.screen.query_one(SidePanel).query_one(SwapButton)
        assert btn.action is None, (
            "SwapButton has an `action`, so press() runs that instead of posting "
            "Button.Pressed — the swap handler can never fire"
        )
        ctrl.resolve(None)


def test_the_control_owns_its_press():
    """The positive half of the census — the owner really is an owner.

    A green `test_no_body_wires_its_own_swap` is also what you get by deleting
    the feature, so the two are asserted together.
    """
    import inspect

    from litetui.side_panel import SwapButton as SB

    src = inspect.getsource(SB)
    assert "request_swap(self)" in src, "SwapButton stopped handling its own press"
    assert "event.stop()" in src, (
        "the swap press must not bubble — a body could read it as an answer"
    )
