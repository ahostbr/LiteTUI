"""The swap control is in all four real dialogs, and it tells the truth. T086.

Ryan asked for it explicitly — "there should be a button to swap back in forth
DURING the dialog". T075 built it in `dialog_demo.py`, the four conversion
briefs did not carry it, and `grep -rn demo-swap` returned the demo and nothing
else. This is the gate on that not happening again.

🔴 THE TABLE IS THE POINT. Every case below is derived from `REAL_BODIES`, so a
fifth dialog joins these assertions by being added to one list — it cannot be
"the one nobody swept", which is precisely how the swap button and the
DEFAULT_CSS scoping bug both reached Ryan.

⚠️ THE PARAGRAPH THAT USED TO BE HERE WAS WRONG, AND IT WAS WRONG CONFIDENTLY.
It said `present_dialog`'s modal branch has no DialogController "so on that path
a swap is a no-op and the button must SAY SO rather than sit there pressable and
inert". The premise was true; the conclusion was never reached. Nothing consulted
`swappable()`, so the button said nothing — and nothing in this file ever PRESSED
it, in either host, which is how all four dialogs shipped with the control wired
to no handler at all. T222 gave the press to the control and gave a routed modal
a third exit, so a screen a router pushed can now dock without a controller.

🔴 EVERY SWAP ASSERTION IN THIS FILE STILL CALLS `request_swap` DIRECTLY. That is
exactly what made the defect invisible here, and it is left that way so the
distinction stays visible: these arms test the MECHANISM. The arms that press the
CONTROL are in tests/test_swap_control_is_wired.py. Adding another `request_swap`
arm here does not cover the button.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _settle import settle_until
from litetui import app as m
from litetui.ask_user_question import AskUserQuestionBody, QuestionState
from litetui.picker import PickerBody, PickerScreen
from litetui.plugins.scheduler_ui import JobBody
from litetui.side_panel import (
    DialogController, SidePanel, SwapButton, present_dialog,
)
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
    ("confirm_stop", ConfirmStopBody, ConfirmStop),
    ("picker",
     lambda: PickerBody("Pick a model", [("a", "model-a")]),
     lambda: PickerScreen("Pick a model", [("a", "model-a")])),
]


@pytest.mark.parametrize("name,body,modal", REAL_MODALS,
                         ids=[n for n, _, _ in REAL_MODALS])
@pytest.mark.asyncio
async def test_the_modal_path_can_swap_ONLY_when_a_router_pushed_it(name, body, modal):
    """🔴 THIS TEST USED TO ASSERT THE OPPOSITE, AND ITS PREMISE WAS THE BUG.

    It was `test_on_the_modal_path_the_control_says_it_CANNOT_swap`. It pushed
    the screen ITSELF, asserted `swappable() is False`, and called that "the
    branch Ryan is on". Ryan's branch goes through `present_dialog`, which is not
    what it tested — and `swappable()` had no callers, so "the control says so"
    was a method returning False to nobody. Precise, passing, and about a
    situation the app does not produce.

    THE REAL DISCRIMINATOR IS WHO PUSHED THE SCREEN. A router marks the screen it
    pushes and watches its dismissal for `SWAP`; a screen a caller pushed itself
    is left alone, because dismissing that one with a sentinel would hand a value
    its own caller never asked for. Both halves are asserted here — a green on
    the "cannot" half alone is also what you get from never adding the feature.
    """
    a = make_app()
    a.settings.dialog_style = "modal"
    async with a.run_test(size=(120, 40)) as pilot:
        # ROUTED: present_dialog pushes it, so it can dock.
        present_dialog(a, body, modal, None)
        appeared = await settle_until(pilot, lambda: bool(a.screen.query(SwapButton)))
        assert appeared, f"{name}: no swap control on the routed modal path"

        btn = a.screen.query_one(SwapButton)
        assert str(btn.label) == "Dock to side", (
            f"{name}: a modal dialog should offer the SIDEBAR as its destination"
        )
        assert btn.swappable() is True, (
            f"{name}: a routed modal must be able to dock — the router is watching"
        )
        a.screen.dismiss(None)
        await pilot.pause()

        # BARE: nobody is watching this one's exit, so the control must not act.
        screen = modal()
        a.push_screen(screen)
        await settle_until(pilot, lambda: bool(a.screen.query(SwapButton)))
        bare = a.screen.query_one(SwapButton)
        assert bare.swappable() is False, (
            f"{name}: claims it can swap, but no router pushed this screen"
        )
        assert bare.display is False, (
            f"{name}: an inert swap control is still being offered"
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


# ── T252: a swap that arrives while the previous one is still mounting ───────

@pytest.mark.parametrize("ticks", list(range(0, 8)))
@pytest.mark.asyncio
async def test_a_swap_arriving_mid_mount_does_not_crash(ticks):
    """🔴 THE CRASH IS IN THE VIEW BEING TORN DOWN, NOT THE ONE BEING BUILT.

    `_mount_view`'s modal branch did not await its push (`side_panel.py:184`)
    while its sidebar sibling one line up DID (`:180`), so `swap()` returned
    while `_ModalHost` had not entered `_compose`. A second swap 1-2 event-loop
    ticks later pruned that half-composed subtree: `App._prune` marks
    `_pruning` across `walk_children` (app.py:4302), `Widget.mount` then EARLY
    RETURNS SILENTLY on `_pruning` (widget.py:1424-1425) so the `Select` never
    gets its children, and `_pre_process` dispatches `events.Mount()`
    UNCONDITIONALLY anyway (message_pump.py:591) -> `Select._on_mount`
    (_select.py:623) -> `query_one(SelectOverlay)` -> NoMatches -> the app dies.

    ⬜ A USER CANNOT REACH THIS, AND THE ARM SAYS SO SO NOBODY RE-INFLATES THE
    SEVERITY FROM THE STACK TRACE. Measured by pressing the real control with
    the button RE-QUERIED FROM THE LIVE VIEW each time (an earlier probe held a
    stale handle and reported the opposite): at every gap >= 1 tick the count is
    `pressed=1` — THE SECOND PRESS NEVER HAPPENS, because the new view's
    SwapButton has not composed yet. The window is strictly BEFORE the control
    exists. So this is reachable only from a programmatic `ctrl.swap()`, which
    is why the arm drives the controller directly and lives in THIS file — the
    header's rule: these arms test the MECHANISM, the ones that press the
    control are in test_swap_control_is_wired.py.

    ⚠️ `assert ctrl.pending` IS NOT WHAT CATCHES THIS. It PASSES ON A DEAD APP:
    `_handle_exception` stores the error and sets `_return_code = 1`
    (app.py:3190-3203) but the block body keeps running, so the remaining ticks,
    this assertion, `resolve()` and the `wait_for` all complete normally. The
    crash surfaces at `__aexit__`, where `run_test`'s finally re-raises
    `app._exception` (app.py:2137-2145) — which is why `pending` is asserted
    INSIDE the block (outside it would never execute) and why nothing here
    reaches into `app._exception` by hand.

    ⚠️ THE TICKS ARE `asyncio.sleep(0)`, NOT `pilot.pause()`, AND THAT IS
    LOAD-BEARING. `sleep(0)` yields to the scheduler exactly once.
    `pilot.pause()` waits for the app to go idle and the screen to settle,
    draining many turns — it would step straight OVER the window and this arm
    would be GREEN on broken code.

    🔴 IT IS A RANGE AND NOT THE EXACT WINDOW, BECAUSE THE WINDOW MOVES WITH THE
    HARNESS. Measured here, deterministically (5/5 each): before the fix
    k = 2, 3, 4 CRASH and k = 0, 1, 5..11 are clean. The thinker who found this
    measured the window at k = 1, 2 in a slightly different setup — so pinning
    either pair would have made the arm SILENTLY VACUOUS on the other machine:
    `[1, 2]` here would test one dead tick and one live one and miss k = 3 and
    k = 4 entirely. 0..7 brackets both with margin, costs ~6 s, and fails if the
    window ever moves within it.

    ⬜ NEGATIVE CONTROL, with the fix applied: k = 0..39 — ten times the widest
    observed window — passed 40/40. The fix CLOSES the window; it does not move
    it. Re-run that sweep, not just this arm, if `_mount_view` is ever touched.

    ⚠️ TWO WAYS TO ACCIDENTALLY BLIND THIS ARM, both worth knowing before you
    edit it. Do NOT wrap the block in `pytest.raises` — the fix must make it not
    raise at all, so a `raises` would invert the gate and pass on the broken
    code. And `_handle_exception` keeps only the FIRST exception
    (`app.py:3201`, `if self._exception is None`), so ANY earlier app error in
    this test would occupy that slot and mask the crash entirely: keep this
    app clean and do nothing else in the block.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, lambda: JobBody(None, "0 9 * * *"),
                                "sidebar", "right")
        task = asyncio.create_task(ctrl.open())
        await pilot.pause()                    # the sidebar view is up and composed

        await ctrl.swap()                      # -> modal
        for _ in range(ticks):
            await asyncio.sleep(0)             # land inside the mount window
        await ctrl.swap()                      # prunes a half-composed subtree

        # Not padding: the crash happens in the pruned widget's OWN message task,
        # which has to be scheduled after `swap()` returns. Without this the app
        # dies during teardown instead — still caught, with a less legible stack.
        for _ in range(10):
            await asyncio.sleep(0)

        assert ctrl.pending, "a swap resolved the future"
        ctrl.resolve(None)
        await asyncio.wait_for(task, 3)
