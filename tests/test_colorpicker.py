"""The color picker's math and wiring — hex isn't enough, but it must stay exact.

The picker's value IS its geometry: a click maps to a cell, a cell to (s,v),
(h,s,v) to a hex the theme creator stores. Every mapping here is pure and
round-trips, because a picker that returns a slightly different color than
the one under the cursor is a control that lies with confidence.
"""
from pathlib import Path

import pytest

from litetui.colorpicker import (
    GRID_H,
    GRID_W,
    HUE_W,
    cell_to_hue,
    cell_to_sv,
    hex_to_hsv,
    hsv_to_hex,
    hue_to_cell,
    render_hue_bar,
    render_sv_field,
    sv_to_cell,
)

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


# --- wiring ------------------------------------------------------------------
def test_clicking_a_hex_field_is_the_pickers_door():
    """v1 shipped a "pick" Button beside a 100%-width Input — laid out
    zero-wide past the right edge, invisible: the dead-control class in a
    new costume. Now the FIELD is the trigger and the name field is not."""
    assert "def on_click" in SETTINGS_SRC
    assert 'wid.startswith("ct-")' in SETTINGS_SRC
    assert 'wid == "ct-name"' in SETTINGS_SRC          # name field excluded
    assert 'Button("pick"' not in SETTINGS_SRC          # the corpse stays gone


def test_picker_opens_with_the_fields_current_value():
    """The picker must start FROM what the field holds — opening on a default
    gray when the field says #c9a24d silently discards the starting point."""
    assert "ColorPickerScreen(initial=box.value.strip()" in SETTINGS_SRC
