"""T080: PickerScreen on the sidebar host — one class, five call sites.

The picker is the highest-leverage conversion in the set: ONE class serves
/model, /resume, /convos, the engine picker (twice) and the skills picker. That
also makes it the one where a regression is widest, so the modal path is
asserted to be UNCHANGED rather than merely equivalent.

🔴 THE ASSERTION THAT MATTERS MOST IS THE Esc/SWAP DISTINCTION. Both leave the
user without an answer, and both are "None-ish", but they must not look alike to
the caller:

    Esc   -> the dialog RESOLVES with None -> the callback FIRES with None
    swap  -> the dialog does NOT resolve   -> the callback is NOT CALLED

Every picker callback here starts `if not choice: return`. If a swap delivered
None, swapping hosts mid-pick would silently cancel the pick — and on /resume
that reads as "the app ignored my click".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui.picker import PickerBody, PickerScreen, pick
from litetui.side_panel import DialogController, SidePanel, close_dialog, request_swap
from textual.widgets import OptionList

ROWS = [(f"id-{i}", f"row {i}") for i in range(12)]


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


@pytest.mark.asyncio
async def test_the_modal_path_is_still_a_real_PickerScreen() -> None:
    """Identity. test_modal_centering asserts the CSS selector names this class."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        a.settings.dialog_style = "modal"
        got = []
        pick(a, "Pick one", ROWS, got.append)
        await pilot.pause()
        assert isinstance(a.screen, PickerScreen), (
            f"modal path produced {type(a.screen).__name__}, not PickerScreen"
        )
        assert a.screen.query_one(PickerBody), "the screen lost its body"


@pytest.mark.asyncio
async def test_selection_resolves_in_BOTH_hosts() -> None:
    for style in ("modal", "sidebar"):
        a = make_app()
        got = []
        async with a.run_test(size=(120, 30)) as pilot:
            a.settings.dialog_style = style
            pick(a, "Pick one", ROWS, got.append)
            await pilot.pause()
            body = a.screen.query_one(PickerBody)
            close_dialog(body, "id-3")
            await pilot.pause()
        assert got == ["id-3"], f"{style}: callback got {got}, expected ['id-3']"


@pytest.mark.asyncio
async def test_sidebar_path_mounts_a_panel_not_a_screen() -> None:
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        a.settings.dialog_style = "sidebar"
        pick(a, "Pick one", ROWS, lambda v: None)
        await pilot.pause()
        assert len(a.screen.query(SidePanel)) == 1, "sidebar style mounted no panel"
        assert not isinstance(a.screen, PickerScreen), "it still pushed the modal"
        a.screen.query(SidePanel)[0].controller.resolve(None)


@pytest.mark.asyncio
async def test_current_row_is_preselected_in_the_sidebar_too() -> None:
    """`current=` is how /model shows you what you are already on."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        a.settings.dialog_style = "sidebar"
        pick(a, "Pick one", ROWS, lambda v: None, current="id-7")
        await pilot.pause()
        ol = a.screen.query_one("#picker-list", OptionList)
        assert ol.highlighted == 7, (
            f"highlighted row is {ol.highlighted}, not the current one (7)"
        )
        a.screen.query(SidePanel)[0].controller.resolve(None)


@pytest.mark.asyncio
async def test_a_swap_carries_the_HIGHLIGHTED_ROW_and_leaves_the_future_pending() -> None:
    """A picker rebuilt at row 0 silently relocates the user's selection.

    This is worse than losing typed text: nothing on screen says it moved, and
    the next Enter picks a different thing than the one that was highlighted.
    """
    a = make_app()
    got = []
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(
            a, lambda: PickerBody("Pick one", ROWS), "sidebar", "right"
        )
        a.run_worker(ctrl.open(), name="dlg")
        await pilot.pause()

        a.screen.query_one("#picker-list", OptionList).highlighted = 9
        await pilot.pause()
        request_swap(a.screen.query_one(PickerBody))
        await pilot.pause()
        await pilot.pause()

        assert ctrl.style == "modal", "the swap did not change host"
        after = a.screen.query_one("#picker-list", OptionList).highlighted
        assert after == 9, f"the swap moved the highlight to {after}, losing row 9"
        assert ctrl.pending, "THE SWAP RESOLVED THE FUTURE"
        assert got == [], f"a swap delivered {got} to the caller"
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_ESC_DELIVERS_NONE_BUT_A_SWAP_DELIVERS_NOTHING() -> None:
    """🔴 The distinction the whole conversion turns on, asserted side by side.

    Both are "no answer". Only one of them is an ANSWER. Every picker callback
    begins `if not choice: return`, so a swap that delivered None would cancel
    the pick and look exactly like the user pressing Esc.
    """
    # ── a swap must deliver NOTHING ──────────────────────────────────────────
    a = make_app()
    swapped: list = []
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(
            a, lambda: PickerBody("Pick one", ROWS), "sidebar", "right"
        )

        async def _run():
            swapped.append(await ctrl.open())

        a.run_worker(_run(), name="dlg")
        await pilot.pause()
        request_swap(a.screen.query_one(PickerBody))
        await pilot.pause()
        await pilot.pause()
        assert swapped == [], f"the SWAP delivered {swapped} — it answered the picker"
        ctrl.resolve(None)
        await pilot.pause()
    assert swapped == [None], "after a real cancel the caller should get None"

    # ── Esc must deliver None ────────────────────────────────────────────────
    b = make_app()
    esc: list = []
    async with b.run_test(size=(120, 30)) as pilot:
        b.settings.dialog_style = "sidebar"
        pick(b, "Pick one", ROWS, esc.append)
        await pilot.pause()
        panel = b.screen.query_one(SidePanel)
        panel.action_cancel()
        await pilot.pause()
    assert esc == [None], f"Esc delivered {esc}, expected [None]"
