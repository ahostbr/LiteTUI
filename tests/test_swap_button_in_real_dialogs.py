"""The swap control is in all four real dialogs, and it tells the truth. T086.

Ryan asked for it explicitly — "there should be a button to swap back in forth
DURING the dialog". T075 built it in `dialog_demo.py`, the four conversion
briefs did not carry it, and `grep -rn demo-swap` returned the demo and nothing
else. This is the gate on that not happening again.

🔴 THE TABLE IS THE POINT. Every case below is derived from `REAL_BODIES`, so a
fifth dialog joins these assertions by being added to one list — it cannot be
"the one nobody swept", which is precisely how the swap button and the
DEFAULT_CSS scoping bug both reached Ryan.

⚠️ AND `swappable()` IS ASSERTED IN BOTH DIRECTIONS, because the honest answer
differs per host. `request_swap` needs a DialogController; only SidePanel and
_ModalHost carry one (`grep -rn 'self\\.controller\\s*=' src/litetui/*.py`
returns exactly two lines). `present_dialog`'s modal branch deliberately pushes
the ORIGINAL ModalScreen, which has none — so on that path a swap is a no-op and
the button must SAY SO rather than sit there pressable and inert.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _settle import settle_until
from litetui import app as m
from litetui.ask_user_question import AskUserQuestionBody, QuestionState
from litetui.picker import PickerBody, PickerScreen
from litetui.side_panel import DialogController, SidePanel, SwapButton
from litetui.tool_approval import ToolApprovalBody
from litetui.tool_policy import INTERACTIVE, WORKSPACE_WRITE, PolicyDecision
from litetui.widgets import ConfirmStop, ConfirmStopBody


def _approval_decision():
    return PolicyDecision("confirm", INTERACTIVE, frozenset({WORKSPACE_WRITE}), "why")


#: THE ONE LIST. Add a dialog here and every assertion below covers it.
REAL_BODIES = [
    ("picker", lambda: PickerBody("Pick a model", [("a", "model-a"), ("b", "model-b")])),
    ("confirm_stop", ConfirmStopBody),
    ("tool_approval", lambda: ToolApprovalBody("write", {"path": "x"}, _approval_decision())),
    ("ask_user_question", lambda: AskUserQuestionBody(
        [QuestionState(label="one", question="q1?",
                       options=[{"title": "a", "description": ""}])],
        threading.Event(), [],
    )),
]
IDS = [name for name, _ in REAL_BODIES]


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


@pytest.mark.parametrize("name,factory", REAL_BODIES, ids=IDS)
@pytest.mark.asyncio
async def test_every_real_dialog_offers_the_swap_control(name, factory):
    """The regression that reached Ryan: three of four dialogs had no button."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        opened = await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))
        assert opened, f"{name}: the dialog never opened — this proves nothing"

        panel = a.screen.query_one(SidePanel)
        # SCOPED to the panel, never a global query. OpenBolt's probe matched a
        # DIFFERENT modal's box by querying globally and reported a bug that was
        # not there; in a sidebar the chat and the dialog share one screen.
        buttons = panel.query(SwapButton)
        assert len(buttons) == 1, f"{name}: expected one swap control, got {len(buttons)}"


@pytest.mark.parametrize("name,factory", REAL_BODIES, ids=IDS)
@pytest.mark.asyncio
async def test_in_a_sidebar_it_offers_the_modal_and_can_actually_do_it(name, factory):
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))
        btn = a.screen.query_one(SidePanel).query_one(SwapButton)

        assert str(btn.label) == "Open as modal", (
            f"{name}: names its current state instead of its destination"
        )
        assert btn.swappable() is True, (
            f"{name}: in a sidebar the swap has a controller and must work"
        )


@pytest.mark.parametrize("name,factory", REAL_BODIES, ids=IDS)
@pytest.mark.asyncio
async def test_a_swap_does_NOT_answer_the_dialog(name, factory):
    """🔴 THE INVARIANT T075 MUTATION-PROVED, RE-ASSERTED ON THE REAL DIALOGS.

    A swap changes the HOST. The controller keeps owning the future. If a swap
    ever resolved it, pressing "Open as modal" would silently answer a pending
    tool approval — the one direction that must never happen.
    """
    from litetui.side_panel import request_swap

    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))

        request_swap(a.screen.query_one(SidePanel).query_one(SwapButton))
        await settle_until(pilot, lambda: ctrl.style == "modal")

        assert ctrl.style == "modal", f"{name}: the swap did not change host"
        assert ctrl.pending, f"{name}: THE SWAP ANSWERED THE DIALOG"


#: The ORIGINAL modal screens `present_dialog` actually pushes — NOT `_ModalHost`.
#: That branch is deliberate (its docstring defends the identity two tests bind
#: to), and it is the branch Ryan is on: `dialog_style` defaults to "modal".
REAL_MODALS = [
    ("confirm_stop", ConfirmStop),
    ("picker", lambda: PickerScreen("Pick a model", [("a", "model-a")])),
]


@pytest.mark.parametrize("name,factory", REAL_MODALS, ids=[n for n, _ in REAL_MODALS])
@pytest.mark.asyncio
async def test_on_the_modal_path_the_control_says_it_CANNOT_swap(name, factory):
    """🔴 THE OTHER DIRECTION, AND IT IS THE ONE THAT NEARLY SHIPPED A DEAD BUTTON.

    `present_dialog` pushes the original ModalScreen, which carries no
    DialogController — so `request_swap` there is a NO-OP. A button that is
    visible, pressable and inert is the defect class this codebase spent the day
    clearing, so the control reports the truth instead of pretending.

    ⚠️ THE DEMO COULD NOT HAVE CAUGHT THIS: dialog_demo always runs under a
    controller, so T075's swap has never executed this path.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = factory()
        a.push_screen(screen)
        appeared = await settle_until(pilot, lambda: bool(a.screen.query(SwapButton)))
        assert appeared, f"{name}: no swap control on the modal path"

        btn = a.screen.query_one(SwapButton)
        assert str(btn.label) == "Dock to side", (
            f"{name}: a modal dialog should offer the SIDEBAR as its destination"
        )
        assert btn.swappable() is False, (
            f"{name}: claims it can swap, but this path has no controller"
        )
        screen.dismiss(None)
        await pilot.pause()


def test_the_control_carries_its_own_style_so_no_body_needs_a_rule():
    """Adding the button touched no stylesheet, and this is why.

    Textual scopes DEFAULT_CSS to the DECLARING class — the mechanism that broke
    the sidebar conversion when the bodies' rules stayed on the screens they were
    lifted out of. Declared on SwapButton, that same scoping makes the rule
    travel with the control.
    """
    assert "SwapButton" in SwapButton.DEFAULT_CSS
    assert "width" in SwapButton.DEFAULT_CSS


def test_the_swap_control_is_not_a_second_copy_of_the_demos():
    """The demo yields the SHARED control, not its own button.

    If someone re-adds a private button to dialog_demo.py, the relabel rule has
    two owners again and they can disagree about what the label says.
    """
    import inspect

    from litetui import dialog_demo

    src = inspect.getsource(dialog_demo.DemoDialogBody)
    assert "SwapButton(" in src, "the demo stopped using the shared control"
    assert 'Button("Open as modal"' not in src, (
        "a private swap button came back to the demo"
    )
