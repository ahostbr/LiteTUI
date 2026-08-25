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
from _settle import settle_until
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
        await settle_until(pilot, lambda: a.screen.query(ConfirmStopBody))

        a.screen.query_one("#no").focus()
        await pilot.pause()
        request_swap(a.screen.query_one(ConfirmStopBody))
        # A swap is call_next -> await _mount_view -> a _settle deferred again
        # until the body composes. Two pauses was a guess about all three, and it
        # was wrong 2 runs in 12 on this box. Wait for the carried focus to
        # ARRIVE; the assertions below still decide whether it is the right one.
        settled = await settle_until(
            pilot,
            lambda: ctrl.style == "modal"
            and getattr(a.screen.focused, "id", None) == "no",
        )

        # 🔴 THIS TEST HAS A ~1% RESIDUAL NOBODY HAS CAPTURED, SO IT REPORTS ITS
        # OWN STATE ON FAILURE. It fired once and the loop that caught it kept
        # only the summary line, so the assertion text was lost; ~180 runs since
        # (isolated, file-ordered, and under verified CPU load) have not
        # reproduced it. A 1% event is not worth grinding hundreds of runs for
        # when the next natural occurrence can just explain itself.
        #
        # `settled` is the discriminator and it is why it is captured rather
        # than discarded. Measured: this wait uses 1-2 frames of its 25, max 2
        # across 78 calls, and never hits the cap even under contention that
        # doubles wall-clock. So:
        #     settled False -> the carry never ARRIVED. A wait cannot fix that;
        #                      it is the LOST shape, like the _view-window bug.
        #     settled True  -> the state arrived and something moved it AFTER.
        # Those need opposite fixes, and the flag is the only cheap way to tell
        # them apart from a CI log.
        diag = (
            f"[settled={settled} style={ctrl.style!r} "
            f"focused={getattr(a.screen.focused, 'id', None)!r} "
            f"pending={ctrl.pending} bodies={len(a.screen.query(ConfirmStopBody))}]"
        )
        assert ctrl.style == "modal", f"the swap did not change host {diag}"
        focused = a.screen.focused
        assert getattr(focused, "id", None) == "no", (
            f"focus landed on {getattr(focused, 'id', None)!r} after the swap, "
            f"not the 'No, keep going' button it was on {diag}"
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
        # LET THE APP SETTLE FIRST. Opening a dialog in the same frame the app
        # is still mounting is a race that does not exist in production: this
        # dialog is raised by Escape during a turn, long after the chat input has
        # taken focus. Without this wait the app's own startup focus lands AFTER
        # the panel's and steals it — which is a real ordering, just not one any
        # user can reach, and "fixing" the host for it would be fixing a
        # scenario invented by the test.
        for _ in range(10):
            await pilot.pause()
            if a.screen.focused is not None:
                break

        ctrl = DialogController(a, ConfirmStopBody, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")

        # WAIT FOR THE STATE, DO NOT GUESS THE TICK. Focus is placed in the
        # host's deferred `_settle`, which re-defers until the body has composed,
        # so the number of frames is not fixed — it depends on which host and on
        # how busy the loop is. This test passed alone and failed inside the full
        # suite on exactly that difference. A bounded wait on the OBSERVABLE
        # condition is deterministic; adding another `pause()` until it goes
        # green is how a flake gets written down as a fix.
        body = None
        for _ in range(10):
            await pilot.pause()
            found = a.screen.query(ConfirmStopBody)
            if found and a.screen.focused is not None:
                inside = a.screen.focused in found[0].walk_children(with_self=True)
                if inside:
                    body = found[0]
                    break
        assert body is not None, "the dialog never took focus"

        focused = a.screen.focused
        assert focused is not None, "nothing focused — Enter would go to the chat"
        assert focused in body.walk_children(with_self=True), (
            f"focus landed on {focused!r}, outside the dialog body"
        )
        ctrl.resolve(False)
