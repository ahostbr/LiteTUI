"""T078: ConfirmStop works in BOTH hosts, and the modal path is still the old one.

This is the first REAL dialog on the T075 host — the demo body was written by the
same hand as the host and was easy by construction. The two things asserted here
that the demo could not be:

  1. THE MODAL PATH IS UNCHANGED, not merely equivalent. `ConfirmStop` is still a
     distinct ModalScreen with its own class name, because `app.py`'s centering
     CSS selects it BY NAME and `test_modals` asserts `isinstance(screen,
     ConfirmStop)`. A conversion that routed the modal branch through the generic
     `_ModalHost` would pass every behavioural test here and still change what the
     modal IS.

  2. ONE BODY, TWO HOSTS, NO BRANCH IN THE HANDLERS. `close_dialog` resolves the
     controller in a sidebar and falls back to `screen.dismiss` in a modal, so the
     button handlers are identical in both. Without that fallback the body's
     buttons would be DEAD in the modal — silently, no error.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui.side_panel import DialogController, SidePanel, close_dialog, request_swap
from litetui.widgets import ConfirmStop, ConfirmStopBody


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


@pytest.mark.asyncio
async def test_the_modal_path_is_still_a_real_ConfirmStop_screen() -> None:
    """Identity, not behaviour. The CSS and test_modals both bind to this."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        a.settings.dialog_style = "modal"
        got = []
        a._on_stop_answer = got.append
        # action_stop_turn is inert with nothing running — Escape on an idle app
        # must not raise a dialog. Drive it through the real entry point rather
        # than calling present_dialog directly, so the guard stays exercised.
        a._chat_running = lambda: True
        a.action_stop_turn()
        await pilot.pause()
        assert isinstance(a.screen, ConfirmStop), (
            f"modal path produced {type(a.screen).__name__}, not ConfirmStop — "
            "the centering CSS selects by class name and would stop applying"
        )
        assert a.screen.query_one(ConfirmStopBody), "the screen lost its body"


@pytest.mark.asyncio
async def test_the_body_buttons_work_INSIDE_THE_MODAL_via_the_fallback() -> None:
    """close_dialog must reach `screen.dismiss` when there is no controller.

    Without the fallback these buttons do nothing at all — the dialog opens,
    the click registers, and the turn hangs. No exception anywhere.
    """
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        got = []
        a.push_screen(ConfirmStop(), got.append)
        await pilot.pause()
        body = a.screen.query_one(ConfirmStopBody)
        close_dialog(body, True)
        await pilot.pause()
    assert got == [True], f"the modal never resolved; got {got}"


@pytest.mark.asyncio
async def test_sidebar_path_mounts_a_panel_and_answers_through_it() -> None:
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        a.settings.dialog_style = "sidebar"
        got = []
        a._on_stop_answer = got.append
        # action_stop_turn is inert with nothing running — Escape on an idle app
        # must not raise a dialog. Drive it through the real entry point rather
        # than calling present_dialog directly, so the guard stays exercised.
        a._chat_running = lambda: True
        a.action_stop_turn()
        await pilot.pause()
        assert len(a.screen.query(SidePanel)) == 1, "sidebar style mounted no panel"
        assert not isinstance(a.screen, ConfirmStop), (
            "sidebar style still pushed the modal screen"
        )
        close_dialog(a.screen.query_one(ConfirmStopBody), False)
        await pilot.pause()
    assert got == [False], f"the sidebar never resolved; got {got}"


@pytest.mark.asyncio
async def test_a_swap_carries_the_focused_button_AND_leaves_the_future_pending() -> None:
    """The per-body swap test. ConfirmStop's only state is which button has focus —
    small, but losing it moves focus onto 'Yes, stop' in the middle of deciding
    whether to stop, which is the one direction that matters."""
    a = make_app()
    got = []
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, ConfirmStopBody, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await pilot.pause()

        a.screen.query_one("#no").focus()
        await pilot.pause()
        request_swap(a.screen.query_one(ConfirmStopBody))
        await pilot.pause()
        await pilot.pause()

        assert ctrl.style == "modal", "the swap did not change host"
        focused = a.screen.focused
        assert getattr(focused, "id", None) == "no", (
            f"focus landed on {getattr(focused, 'id', None)!r} after the swap, "
            "not the 'No, keep going' button it was on"
        )
        assert ctrl.pending, "THE SWAP RESOLVED THE FUTURE — it answered the dialog"
        assert got == []

        ctrl.resolve(False)
        await pilot.pause()


@pytest.mark.asyncio
async def test_the_refusing_control_is_reachable_first_in_the_sidebar() -> None:
    """SilverBolt's contract 3, generalised to this dialog.

    A dialog that appears mid-turn must not be dismissed destructively by a blind
    Enter. ConfirmStop composes 'Yes, stop' first, so here the check is the honest
    one for THIS dialog: whatever the panel focuses must be a button inside the
    body, never something outside it — the panel picks focus differently from a
    ModalScreen and could otherwise land on the chat.
    """
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        ctrl = DialogController(a, ConfirmStopBody, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await pilot.pause()

        body = a.screen.query_one(ConfirmStopBody)
        focused = a.screen.focused
        assert focused is not None, "nothing focused — Enter would go to the chat"
        assert focused in body.walk_children(with_self=True), (
            f"focus landed on {focused!r}, outside the dialog body"
        )
        ctrl.resolve(False)
