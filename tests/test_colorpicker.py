"""The color picker's math and wiring — hex isn't enough, but it must stay exact.

The picker's value IS its geometry: a click maps to a cell, a cell to (s,v),
(h,s,v) to a hex the theme creator stores. Every mapping here is pure and
round-trips, because a picker that returns a slightly different color than
the one under the cursor is a control that lies with confidence.
"""
from pathlib import Path

import pytest

from textual.widgets import Input

from litetui import app as m
from litetui.colorpicker import (
    GRID_H,
    GRID_W,
    HUE_W,
    MIN_W,
    ColorPickerBody,
    cell_to_hue,
    cell_to_sv,
    hex_to_hsv,
    hsv_to_hex,
    hue_to_cell,
    render_hue_bar,
    render_sv_field,
    sv_to_cell,
)
from litetui.settings import Settings
from litetui.settings_screen import SettingsScreen

SETTINGS_SRC = (Path(__file__).resolve().parent.parent / "src" / "litetui" / "settings_screen.py"
                ).read_text(encoding="utf-8")


# --- conversions -------------------------------------------------------------
def test_hex_hsv_roundtrip_on_theme_colors():
    for hexv in ("#c9a24d", "#00ff41", "#0a0a0b", "#e8e4dc", "#ffffff", "#000000"):
        assert hsv_to_hex(*hex_to_hsv(hexv)) == hexv


def test_hex_to_hsv_rejects_junk():
    for bad in ("red", "#12345", "#12345g", "", "#1234567"):
        with pytest.raises(ValueError):
            hex_to_hsv(bad)


def test_grid_corners_are_the_canonical_colors():
    """Top-left = white (s0 v1), top-right = pure hue, bottom = black."""
    s, v = cell_to_sv(0, 0)
    assert hsv_to_hex(0.0, s, v) == "#ffffff"
    s, v = cell_to_sv(GRID_W - 1, 0)
    assert hsv_to_hex(0.0, s, v) == "#ff0000"
    s, v = cell_to_sv(0, GRID_H - 1)
    assert hsv_to_hex(0.0, s, v) == "#000000"


def test_cell_sv_roundtrip_every_cell():
    """Click -> (s,v) -> cell must land on the SAME cell, for every cell —
    an off-by-one here means the marker sits beside the color you picked."""
    for col in range(GRID_W):
        for row in range(GRID_H):
            s, v = cell_to_sv(col, row)
            assert sv_to_cell(s, v) == (col, row), (col, row)


def test_hue_cell_roundtrip():
    for col in range(HUE_W):
        assert hue_to_cell(cell_to_hue(col)) == col


def test_out_of_range_clicks_clamp_instead_of_crashing():
    assert cell_to_sv(-5, -5) == cell_to_sv(0, 0)
    assert cell_to_sv(999, 999) == cell_to_sv(GRID_W - 1, GRID_H - 1)


# --- rendering ---------------------------------------------------------------
def test_sv_field_dimensions_and_marker():
    t = render_sv_field(0.6, 0.5, 0.5)
    lines = t.plain.split("\n")
    assert len(lines) == GRID_H // 2
    assert all(len(l) == GRID_W for l in lines)
    assert t.plain.count("+") == 1     # exactly one selection marker


def test_hue_bar_marks_the_selection():
    t = render_hue_bar(0.5)
    assert len(t.plain) == HUE_W
    assert t.plain.count("▼") == 1


# --- the field is no longer always 48 columns wide ---------------------------

#: Widths a docked picker actually gets. 30 and 36 are MEASURED panel-box widths
#: (a 100- and a 120-column terminal); GRID_W is what the modal paints, and it
#: is also the CAP -- `#cp-box` is 54 columns but the field never draws wider
#: than 48, which is why 54 is NOT in this list and asking for it clamps.
#: A round-trip that held at 48 and nowhere else would be a picker that lies
#: everywhere except the one host it was written for.
NARROW = [MIN_W, 12, 30, 36, 44, GRID_W]


@pytest.mark.parametrize("w", NARROW)
def test_cell_sv_roundtrip_at_every_painted_width(w):
    """The whole point of the width parameter: the map and the paint agree.

    `on_click` maps a click through `self._painted_w`, so if the round-trip
    holds only at GRID_W then every click in a narrow panel resolves to a
    different colour than the one under the cursor — silently, because a wrong
    colour is still a colour.
    """
    for col in range(w):
        for row in range(GRID_H):
            s, v = cell_to_sv(col, row, w)
            assert sv_to_cell(s, v, w) == (col, row), (w, col, row)


@pytest.mark.parametrize("w", NARROW)
def test_hue_roundtrip_at_every_painted_width(w):
    for col in range(w):
        assert hue_to_cell(cell_to_hue(col, w), w) == col, (w, col)


@pytest.mark.parametrize("w", NARROW)
def test_the_field_is_exactly_w_columns_and_always_12_rows(w):
    """🔴 THE ROW COUNT IS THE ASSERTION THAT WOULD HAVE CAUGHT THE BUG.

    Measured on the un-parameterised field in a sidebar, 40-row screen: below a
    180-column terminal every 48-character line SOFT-WRAPPED and #cp-field drew
    24 rows instead of 12 (term 100 -> 24, 120 -> 24, 170 -> 24, 180 -> 12).
    `on_click` computes `row = event.y * 2` from the UNWRAPPED geometry, so the
    doubled height was not a cosmetic problem: it sheared the click map.

    GRID_H // 2 rows of exactly `w` characters is the invariant that makes both
    the drawing and the map true at any width.
    """
    lines = render_sv_field(0.6, 0.5, 0.5, w).plain.split(chr(10))
    assert len(lines) == GRID_H // 2, f"{w}: {len(lines)} rows, not {GRID_H // 2}"
    assert all(len(line) == w for line in lines), (
        f"{w}: line widths {sorted({len(line) for line in lines})}"
    )
    assert len(render_hue_bar(0.6, w).plain) == w


def test_a_bad_width_clamps_instead_of_crashing():
    """Zero is the honest input, not a hypothetical: `content_size.width` is 0
    before the first layout, and the body asks for it in `on_mount`."""
    from litetui.colorpicker import clamp_width
    assert clamp_width(0) == GRID_W          # "not laid out yet" == full size
    assert clamp_width(None) == GRID_W
    assert clamp_width(-4) == MIN_W
    assert clamp_width(9999) == GRID_W
    lines = render_sv_field(0.2, 0.3, 0.4, 0).plain.split(chr(10))
    assert all(len(line) == GRID_W for line in lines)


# --- wiring ------------------------------------------------------------------
def test_clicking_a_hex_field_is_the_pickers_door():
    """v1 shipped a "pick" Button beside a 100%-width Input — laid out
    zero-wide past the right edge, invisible: the dead-control class in a
    new costume. Now the FIELD is the trigger and the name field is not."""
    assert "def on_click" in SETTINGS_SRC
    assert 'wid.startswith("ct-")' in SETTINGS_SRC
    assert 'wid == "ct-name"' in SETTINGS_SRC          # name field excluded
    assert 'Button("pick"' not in SETTINGS_SRC          # the corpse stays gone


@pytest.mark.asyncio
async def test_picker_opens_with_the_fields_current_value():
    """The picker must start FROM what the field holds — opening on a default
    gray when the field says #c9a24d silently discards the starting point.

    🔴 THIS WAS A SOURCE-TEXT ASSERTION AND IT BROKE ON A REFACTOR THAT KEPT THE
    BEHAVIOUR. It read `"ColorPickerScreen(initial=box.value.strip()" in
    SETTINGS_SRC`; routing the call through `present_dialog` with two factories
    turned it red while the picker still opened on exactly the right colour. A
    gate that fails when the code is right, and would equally pass on a
    commented-out line containing that text, is measuring the prose about the
    thing. So it now OPENS the dialog and reads the value off the body.
    """
    a = m.LiteTUI()
    a.available_models = []
    a.model_id = None
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = SettingsScreen(Settings())
        a.push_screen(screen)
        for _ in range(4):
            await pilot.pause()

        box = Input(value="#c9a24d", id="ct-primary")
        await screen.mount(box)
        await pilot.pause()

        class _Ev:
            widget = box
            def stop(self):
                pass

        screen.on_click(_Ev())
        for _ in range(6):
            await pilot.pause()

        body = a.screen.query_one(ColorPickerBody)
        assert body.value == "#c9a24d", (
            f"the picker opened on {body.value}, not the colour in the field"
        )
