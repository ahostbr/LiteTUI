"""WS7 — a covered non-top `_ModalHost` must be torn down when it is resolved.

DEFECT (pinned, side_panel.py `_ModalHost.close_view`): the teardown is

    await await_subtree_composed(self.body)
    if self.app.screen is self:
        self.app.pop_screen()

`pop_screen` only removes the TOP of the stack, so the `if self.app.screen is
self` guard is the right refusal for a top modal — but for a modal that is
COVERED by another screen it does nothing, and the modal is left LEAKED in the
screen stack. The state is reachable: `AskUserQuestionScreen`
(ask_user_question.py:829) and both `SettingsExitConfirm` pushes
(settings_screen.py:465/1682) can land on top of an open `_ModalHost`. A leaked
modal resurfaces stale the moment the covering screen is dismissed.

This file is a gate on the covered case: `resolve()` on a covered modal must
remove the modal from the current mode's stack WITHOUT popping the covering
screen (the covering screen is a dialog of its own and must not be dismissed
with a default answer).

The top-modal path (unchanged `pop_screen`) is the control arm.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.screen import Screen
from textual.widgets import Static

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _settle import settle_until
from litetui import app as m
from litetui.side_panel import DialogController, _ModalHost


def make_app() -> "m.LiteTUI":
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


class _Cover(Screen):
    """A screen pushed ON TOP of the modal — the covering dialog."""

    def compose(self):
        yield Static("cover")


async def _open_modal(a, pilot) -> "DialogController":
    ctrl = DialogController(a, lambda: Static("body"), "modal")
    a.run_worker(ctrl.open(), name="dlg")
    ok = await settle_until(pilot, lambda: isinstance(a.screen, _ModalHost))
    assert ok, f"the modal never became active; screen={a.screen!r}"
    assert isinstance(ctrl._view, _ModalHost), repr(ctrl._view)
    return ctrl


@pytest.mark.asyncio
async def test_a_covered_modal_is_torn_down_when_resolved():
    """THE DEFECT. Resolving a covered modal must remove it, not leak it."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = await _open_modal(a, pilot)
        modal = ctrl._view

        covering = _Cover()
        a.push_screen(covering)
        ok = await settle_until(pilot, lambda: a.screen is covering)
        assert ok, f"the covering screen never became active; screen={a.screen!r}"

        ctrl.resolve("done")
        for _ in range(8):
            await pilot.pause()

        assert a.screen is covering, (
            f"the covering screen must stay active, got {a.screen!r}")
        assert modal not in a._screen_stack, (
            f"covered _ModalHost LEAKED in the screen stack: {a._screen_stack}")


@pytest.mark.asyncio
async def test_a_top_modal_still_pops_normally():
    """CONTROL: an uncovered (top) modal still tears down via pop_screen."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = await _open_modal(a, pilot)
        modal = ctrl._view

        ctrl.resolve("done")
        for _ in range(8):
            await pilot.pause()

        assert modal not in a._screen_stack, (
            f"top _ModalHost still on the stack: {a._screen_stack}")
