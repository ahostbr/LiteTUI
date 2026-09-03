"""The colour picker's keys reach the colour — in BOTH hosts. T247.

🔴 WHAT WAS BROKEN, AND WHY NOTHING WENT RED. Textual focuses the first
focusable widget when a screen mounts. Until this change the ONLY focusable
thing in the picker was `Input#cp-hex`, and an `Input` handles left and right
itself as cursor movement — so those two never reached `action_nudge`. The hint
label said "arrows/PgUp/PgDn". Every existing test in `test_colorpicker.py`
asks a PURE question (does `cell_to_sv` round-trip, is the field `w` columns
wide); none of them presses a key, so the dead keys were invisible to a green
suite for the whole life of the widget.

⚠️ AND MY FIRST MEASUREMENT OF THE DEFECT WAS WRONG BY TWO KEYS, WHICH IS WHY
THIS FILE STARTS FROM A SATURATED COLOUR. The original probe started at
`#808080` — a GREY, s=0 — and reported right/pageup/pagedown all dead. At zero
saturation a hue rotation changes the hex by NOTHING, so "pageup: #8b8b8b ->
#8b8b8b" is equally consistent with a dead key and a live one. Re-measured from
`#c9a24d` reading h/s/v directly, on the pre-fix tree:

    right     s 0.61692 -> 0.61692   DEAD      pageup    h 0.11425 -> 0.13508  LIVE
    left      s 0.61692 -> 0.61692   DEAD      pagedown  h 0.13508 -> 0.11425  LIVE
    up        v 0.78824 -> 0.83171   LIVE      down      v 0.83171 -> 0.78824  LIVE

TWO keys were dead, not four: `Input` consumes left/right and does NOT consume
PageUp/PageDown. So every arm here reads the AXIS IT DRIVES, never the hex — a
hex comparison cannot see a hue change on a grey, and that is the shape of the
false negative it produced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _settle import settle_until
from litetui import app as m
from litetui.colorpicker import ColorPickerBody, ColorPickerScreen, _SVField
from litetui.side_panel import DialogController, SidePanel
from textual.widgets import Input

#: A SATURATED, non-grey start. Every axis has room to move in both directions
#: and every move is visible in h/s/v.
START = "#c9a24d"

#: (key, axis index into (h, s, v)). One row per binding the picker declares.
KEYS = [
    ("right", 1, "saturation"),
    ("left", 1, "saturation"),
    ("up", 2, "value"),
    ("down", 2, "value"),
    ("pageup", 0, "hue"),
    ("pagedown", 0, "hue"),
]


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


def _hsv(body: ColorPickerBody):
    return (round(body._h, 5), round(body._s, 5), round(body._v, 5))


@pytest.mark.parametrize("key,axis,name", KEYS, ids=[k for k, _, _ in KEYS])
@pytest.mark.asyncio
async def test_every_key_moves_its_axis_in_a_modal(key, axis, name):
    """The path Ryan is on: `dialog_style` defaults to "modal"."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        a.push_screen(ColorPickerScreen(START, [], "primary"))
        await settle_until(pilot, lambda: bool(a.screen.query(ColorPickerBody)))
        body = a.screen.query_one(ColorPickerBody)
        await settle_until(pilot, lambda: a.screen.focused is not None)

        before = _hsv(body)
        await pilot.press(key)
        for _ in range(3):
            await pilot.pause()
        after = _hsv(body)
        assert after[axis] != before[axis], (
            f"{key} did not move {name}: {before[axis]} -> {after[axis]} "
            "(the key never reached action_nudge)"
        )


@pytest.mark.parametrize("key,axis,name", KEYS, ids=[k for k, _, _ in KEYS])
@pytest.mark.asyncio
async def test_every_key_moves_its_axis_in_a_sidebar(key, axis, name):
    """A DOCKED picker must answer the same keys.

    `SidePanel._take_focus` walks `query("*")` in DOM order, so the field is
    what it lands on — but that is a consequence of the fix, not a given, and a
    host that focused something else would leave the keys dead again in exactly
    one of the two hosts.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, lambda: ColorPickerBody(START, [], "primary"),
                                "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))
        body = a.screen.query_one(SidePanel).body
        await settle_until(pilot, lambda: bool(body.children))
        for _ in range(4):
            await pilot.pause()

        before = _hsv(body)
        await pilot.press(key)
        for _ in range(3):
            await pilot.pause()
        after = _hsv(body)
        assert after[axis] != before[axis], (
            f"{key} did not move {name} in a SIDEBAR: "
            f"{before[axis]} -> {after[axis]}"
        )
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_the_field_is_what_takes_focus():
    """The mechanism, asserted directly rather than inferred from the keys.

    If a later change makes something else focusable earlier in the DOM, the
    keys go dead again and every arm above fails with the same message. This one
    names the cause, so the next reader is not left to rediscover it.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        a.push_screen(ColorPickerScreen(START, [], "primary"))
        await settle_until(pilot, lambda: a.screen.focused is not None)
        for _ in range(3):
            await pilot.pause()
        assert isinstance(a.screen.focused, _SVField), (
            f"focus landed on {type(a.screen.focused).__name__}, not the "
            "colour field — the arrows go to whatever has focus"
        )


@pytest.mark.asyncio
async def test_the_hex_box_keeps_its_own_cursor_keys():
    """🔴 THE OTHER POLARITY, AND THE REASON `priority=True` WAS REJECTED.

    "The arrows nudge the colour" is also satisfiable by a fix that makes the
    four bindings `priority=True` — which fires them BEFORE the focused widget
    and so takes left/right away from the hex box. Measured on a minimal
    Textual 8.0.2 app, an Input focused with the caret at column 3:

        priority right   cursor 3 -> 3   binding fired=1
        plain    left    cursor 3 -> 2   binding fired=0     (control)

    The hex box is the picker's documented escape hatch, so a caret that cannot
    move is not an acceptable price. This arm fails on that fix and passes on
    the one that shipped.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        a.push_screen(ColorPickerScreen(START, [], "primary"))
        await settle_until(pilot, lambda: bool(a.screen.query(ColorPickerBody)))
        body = a.screen.query_one(ColorPickerBody)
        box = body.query_one("#cp-hex", Input)
        box.focus()
        await pilot.pause()
        box.cursor_position = 3
        await pilot.pause()

        before_hsv = _hsv(body)
        await pilot.press("right")
        for _ in range(3):
            await pilot.pause()

        assert box.cursor_position == 4, (
            f"the hex box's caret did not move (3 -> {box.cursor_position}) — "
            "something is stealing the Input's own cursor keys"
        )
        assert _hsv(body) == before_hsv, (
            "the colour changed while the HEX BOX had focus: the field's "
            "bindings are firing from the wrong surface"
        )
