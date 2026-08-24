"""T075 spike: sidebar dialogs CARVE space; modals COVER; a swap must do NEITHER.

Several of these are not regression guards — they are THE SPIKE'S MEASUREMENT,
written as assertions so the answers are reproducible instead of being a
screenshot somebody has to squint at:

  1. does a long unwrapped monospace line in a ~60-col panel wrap, truncate, or
     force a horizontal scrollbar?
  2. with the panel open, does the chat REFLOW or does it just get CLIPPED?
  3. after a swap, is the future STILL PENDING?
  4. does focus land somewhere sensible in the new host?

🔴 (3) IS THE ONE THAT MATTERS. A swap that resolves the future silently answers
the dialog, and on the real approval dialog that means allowing or denying a
tool call nobody decided on. It is asserted POSITIVELY here — `controller.pending`
is checked while the dialog is still open — because "no exception was raised" is
not evidence that a future is unresolved.

⚠️ THE REFLOW TEST COMPARES CHAT WIDTH BEFORE AND AFTER, not the panel's own
width. A panel that renders at 60 columns proves only that the panel obeyed its
CSS; it says nothing about whether that space came OUT of the chat or was
painted ON TOP of it. Those look identical from the panel's side.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui.dialog_demo import DemoDialogBody
from litetui.settings import Settings
from litetui.side_panel import (
    DialogController,
    SidePanel,
    close_dialog,
    request_swap,
    show_dialog,
)


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


async def _open(a, pilot, style):
    """Open the demo dialog and hand back its controller plus the answer sink."""
    got: list = []
    ctrl = DialogController(a, DemoDialogBody, style)

    async def _run():
        got.append(await ctrl.open())

    a.run_worker(_run(), name="dlg")
    await pilot.pause()
    return ctrl, got


def test_default_is_modal_so_nothing_changes_for_anyone_who_does_not_opt_in():
    """The spike must be inert by default. A new default IS a behaviour change."""
    assert Settings().dialog_style == "modal"


@pytest.mark.asyncio
async def test_sidebar_style_mounts_a_panel_and_modal_style_does_not() -> None:
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        ctrl, _ = await _open(a, pilot, "sidebar")
        assert len(a.screen.query(SidePanel)) == 1, "sidebar style mounted no panel"
        ctrl.resolve(None)
        await pilot.pause()

        # NEGATIVE CONTROL: the same call under the other setting must NOT make
        # a SidePanel. Without this, a panel that mounts unconditionally passes
        # the assertion above and the toggle is decorative.
        ctrl2, _ = await _open(a, pilot, "modal")
        assert len(a.screen.query(SidePanel)) == 0, (
            "modal style still mounted a SidePanel — the style is not consulted"
        )
        ctrl2.resolve(None)


@pytest.mark.asyncio
async def test_the_chat_REFLOWS_it_does_not_get_clipped() -> None:
    """FINDING 2. `split: right` must take the space out of the layout."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        chat = a.query_one("#chat-log")
        before = chat.size.width

        ctrl, _ = await _open(a, pilot, "sidebar")
        panel = a.screen.query_one(SidePanel)
        after = chat.size.width

        assert after < before, (
            f"chat width did not change ({before} -> {after}): the panel is "
            "COVERING the chat, not splitting it. That kills the route."
        )
        # COMPARE AGAINST outer_size, NOT size. `size` is the CONTENT box: it
        # excludes the 1-col `border-left: vkey` and the 2 cols of `padding: 0 1`.
        # Measured at 120 cols: chat 116 -> 77 (-39), panel content 36, and
        # 36 + 1 + 2 = 39. The first draft asserted against `size` and failed by
        # exactly those 3 columns — the code was right and the test's arithmetic
        # was wrong, so the reconciliation is spelled out rather than the number
        # being quietly adjusted.
        assert before - after == panel.outer_size.width, (
            f"chat lost {before - after} cols but the panel occupies "
            f"{panel.outer_size.width} — the space did not come cleanly out of "
            "the chat, so something else moved too."
        )
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_panel_width_is_clamped() -> None:
    """Part of FINDING 1: the strip really is ~60 cols, so the wrap test is fair."""
    a = make_app()
    async with a.run_test(size=(200, 30)) as pilot:
        ctrl, _ = await _open(a, pilot, "sidebar")
        panel = a.screen.query_one(SidePanel)
        # 33% of 200 is 66, above max-width, so this asserts the CLAMP rather
        # than the percentage — a 120-col terminal would not have.
        assert panel.outer_size.width <= 60, (
            f"panel occupies {panel.outer_size.width} cols; max-width: 60 not applied"
        )
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_a_swap_carries_the_typed_text_AND_LEAVES_THE_FUTURE_PENDING() -> None:
    """🔴 THE WHOLE FEATURE. Type, swap, assert both halves."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl, got = await _open(a, pilot, "sidebar")

        a.screen.query_one("#demo-note").value = "half-written reason"
        body = a.screen.query_one(DemoDialogBody)

        request_swap(body)
        await pilot.pause()
        await pilot.pause()

        assert ctrl.style == "modal", f"swap did not change style (still {ctrl.style})"
        assert len(a.screen.query(SidePanel)) == 0, "the old view was not torn down"

        # HALF ONE: the text survived the rebuild.
        assert a.screen.query_one("#demo-note").value == "half-written reason", (
            "the swap lost what was already typed"
        )
        # HALF TWO, AND THE DANGEROUS ONE: nobody answered the dialog.
        assert ctrl.pending, "THE SWAP RESOLVED THE FUTURE — it answered the dialog"
        assert got == [], f"the caller was handed {got} by a swap, not by a decision"

        # And it is still answerable afterwards, which proves the surviving
        # future is the SAME one the caller is holding rather than a fresh one.
        close_dialog(a.screen.query_one(DemoDialogBody), "allow")
        await pilot.pause()
    assert got == ["allow"], f"post-swap answer was {got}, expected ['allow']"


@pytest.mark.asyncio
async def test_swap_back_returns_to_the_sidebar_and_still_does_not_resolve() -> None:
    """Both directions. A one-way swap would pass the test above."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl, got = await _open(a, pilot, "modal")
        body = a.screen.query_one(DemoDialogBody)
        request_swap(body)
        await pilot.pause()
        await pilot.pause()

        assert ctrl.style == "sidebar", f"swap back gave {ctrl.style}"
        assert len(a.screen.query(SidePanel)) == 1, "no panel after swapping to sidebar"
        assert ctrl.pending, "the reverse swap resolved the future"
        assert got == []
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_focus_lands_inside_the_new_host_after_a_swap() -> None:
    """FINDING 4. Focus must not fall through to the app root."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl, _ = await _open(a, pilot, "sidebar")
        body = a.screen.query_one(DemoDialogBody)
        request_swap(body)
        await pilot.pause()
        await pilot.pause()

        focused = a.screen.focused
        assert focused is not None, "nothing is focused after the swap"
        new_body = a.screen.query_one(DemoDialogBody)
        assert focused in new_body.walk_children(with_self=True) or focused is new_body, (
            f"focus landed on {focused!r}, which is outside the dialog body"
        )
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_the_swap_button_names_its_destination_not_its_current_state() -> None:
    """A button labelled with where you already are is the toggle ambiguity."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl, _ = await _open(a, pilot, "sidebar")
        assert str(a.screen.query_one("#demo-swap").label) == "Open as modal"
        request_swap(a.screen.query_one(DemoDialogBody))
        await pilot.pause()
        await pilot.pause()
        assert str(a.screen.query_one("#demo-swap").label) == "Dock to side"
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_dismiss_resolves_the_awaited_value() -> None:
    """The `dismiss(value)` contract: an awaiting caller must be unchanged."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        ctrl, got = await _open(a, pilot, "sidebar")
        close_dialog(a.screen.query_one(DemoDialogBody), "always")
        await pilot.pause()
    assert got == ["always"], f"awaited value was {got}, expected ['always']"


@pytest.mark.asyncio
async def test_show_dialog_honours_the_setting() -> None:
    """The end-to-end path /test-sidebar uses, not just the controller."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        a.settings.dialog_style = "sidebar"
        a.run_worker(show_dialog(a, DemoDialogBody), name="e2e")
        await pilot.pause()
        assert len(a.screen.query(SidePanel)) == 1, (
            "show_dialog did not read settings.dialog_style"
        )
        close_dialog(a.screen.query_one(DemoDialogBody), None)


@pytest.mark.asyncio
async def test_the_settings_control_exists_for_the_new_field() -> None:
    """settings_screen.py:607 REFUSES to save when a field has no control.

    A field added without its control does not fail there — it fails at SAVE
    time, in Ryan's hands, with a partial settings object. This is the cheap
    place to catch it.
    """
    from litetui.settings_screen import SettingsScreen

    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        a.push_screen(SettingsScreen(a.settings))
        await pilot.pause()
        assert a.screen.query("#f-dialog_style"), (
            "dialog_style has no control — saving settings will report it missing"
        )
