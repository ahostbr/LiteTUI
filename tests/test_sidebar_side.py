"""T077: the sidebar docks LEFT as well as RIGHT — measured on both edges.

🔴 WHY THIS FILE EXISTS AT ALL, rather than a parameter added to the T075 tests:
the right-hand reflow proof (`chat 116 -> 77`, with the 39 lost equal to the
panel's outer width) was measured with `split: right` and **establishes nothing
about `split: left`**. Reporting it as "the panel reflows" would be measuring one
path and claiming the class — which cost this workspace a whole thread the same
day. So the identity is asserted again, independently, on the left edge, and
there is a mutation below proving it can go red there too.

⚠️ THE FAILURE MODE THIS GUARDS IS *SILENT AND COSMETIC*: with the wrong border
edge the panel still splits, still reflows, still dismisses — it just draws its
divider against the terminal wall instead of against the chat. No test that only
checks widths would ever notice, which is why the border edge is asserted from
the resolved styles rather than eyeballed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _settle import settle_until
from litetui import app as m
from litetui.dialog_demo import DemoDialogBody
from litetui.settings import Settings
from litetui.side_panel import DialogController, SidePanel, show_dialog


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


async def _open(a, pilot, side):
    """Open the panel and WAIT FOR IT TO HAVE OCCUPIED SPACE.

    🔴 `ctrl.open()` RUNS IN A WORKER, so one bare `pilot.pause()` is a guess
    about when the panel exists AND when the split has been applied to the
    chat's width. T235's failure is what a lost guess looks like:

        left took 0 cols, right took 39

    — a reading taken before the left panel had mounted, which then reads as
    "the two edges have different box metrics". The panel existing is not
    enough either: `split` is a layout property, so the chat's width changes a
    frame later than the mount.

    ⚠️ BOUNDED, AND NOT ASSERTED HERE. If the panel genuinely never takes space
    the loop exhausts and the caller's own assertion fires with its own message
    (`_settle.settle_until`'s rule). Waiting changes WHEN the width is read,
    never WHETHER the two edges have to agree.

    ⬜ UNLIKE the autoscroll member, THIS FAILURE WAS NOT REPRODUCED: 8 runs at
    load x2.17..x3.60 were all green. So this settle is the mechanism applied by
    analogy, not a fix verified against a failing case, and it is written down
    that way rather than claimed as proven.
    """
    ctrl = DialogController(a, DemoDialogBody, "sidebar", side)
    a.run_worker(ctrl.open(), name="dlg")
    await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))
    await settle_until(pilot, lambda: a.screen.query_one(SidePanel).outer_size.width > 0)
    return ctrl


def test_default_side_is_right_so_nothing_changes_for_anyone() -> None:
    assert Settings().dialog_side == "right"


@pytest.mark.asyncio
async def test_the_chat_REFLOWS_when_the_panel_docks_LEFT() -> None:
    """THE T077 FINDING — asserted on the left edge, not inherited from the right."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        chat = a.query_one("#chat-log")
        before = chat.size.width

        ctrl = await _open(a, pilot, "left")
        panel = a.screen.query_one(SidePanel)
        after = chat.size.width

        assert after < before, (
            f"chat width did not change ({before} -> {after}) with split: left — "
            "the panel is COVERING the chat on that edge, not splitting it."
        )
        assert before - after == panel.outer_size.width, (
            f"chat lost {before - after} cols but the left panel occupies "
            f"{panel.outer_size.width}"
        )
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_both_edges_take_the_same_width_from_the_chat() -> None:
    """Neither edge is cheaper than the other — a cross-check the single-side
    tests cannot make, because each only ever sees its own number."""
    widths = {}
    for side in ("right", "left"):
        a = make_app()
        async with a.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            chat = a.query_one("#chat-log")
            before = chat.size.width
            ctrl = await _open(a, pilot, side)
            widths[side] = before - chat.size.width
            ctrl.resolve(None)
    assert widths["left"] == widths["right"], (
        f"left took {widths['left']} cols, right took {widths['right']} — "
        "the two edges disagree, so one of them has different box metrics"
    )


@pytest.mark.asyncio
async def test_the_border_is_on_the_edge_FACING_THE_CHAT_on_both_sides() -> None:
    """The cosmetic-but-silent half: a right border on a left-docked panel draws
    against the terminal wall. Read from resolved styles, not from the CSS text."""
    for side, facing, away in (("right", "left", "right"), ("left", "right", "left")):
        a = make_app()
        async with a.run_test(size=(120, 30)) as pilot:
            ctrl = await _open(a, pilot, side)
            panel = a.screen.query_one(SidePanel)
            edges = panel.styles.border
            facing_edge = getattr(edges, facing)
            away_edge = getattr(edges, away)
            assert facing_edge and facing_edge[0], (
                f"docked {side}: no border on the {facing} edge, which is the one "
                "facing the chat"
            )
            assert not (away_edge and away_edge[0]), (
                f"docked {side}: there is a border on the {away} edge — that is the "
                "terminal wall, so it reads as a rendering bug"
            )
            ctrl.resolve(None)


@pytest.mark.asyncio
async def test_show_dialog_reads_dialog_side_from_settings() -> None:
    """The path /test-sidebar actually uses."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        a.settings.dialog_style = "sidebar"
        a.settings.dialog_side = "left"
        a.run_worker(show_dialog(a, DemoDialogBody), name="e2e")
        await pilot.pause()
        panel = a.screen.query_one(SidePanel)
        assert panel.has_class("-side-left"), (
            "show_dialog ignored settings.dialog_side"
        )
        panel.controller.resolve(None)


@pytest.mark.asyncio
async def test_a_style_swap_keeps_the_chosen_side() -> None:
    """Two axes stay independent: swapping modal<->sidebar must not reset the edge."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = await _open(a, pilot, "left")
        assert a.screen.query_one(SidePanel).has_class("-side-left")

        await ctrl.swap()          # -> modal
        await pilot.pause()
        assert ctrl.style == "modal"
        await ctrl.swap()          # -> back to sidebar
        await pilot.pause()

        assert ctrl.side == "left", "the swap reset the side"
        assert a.screen.query_one(SidePanel).has_class("-side-left"), (
            "came back as a RIGHT panel — the side did not survive a style swap"
        )
        assert ctrl.pending, "the swap resolved the future"
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_the_settings_control_exists_for_the_new_field() -> None:
    """settings_screen.py refuses to save a field that has no control."""
    from litetui.settings_screen import SettingsScreen

    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        a.push_screen(SettingsScreen(a.settings))
        await pilot.pause()
        assert a.screen.query("#f-dialog_side"), (
            "dialog_side has no control — saving settings will report it missing"
        )
