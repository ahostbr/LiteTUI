"""A terminal color picker for the theme creator — because hex isn't enough.

The shape follows the classic web picker (Ryan's reference: nhn/tui.color-picker):
a saturation/value field for the current hue, a hue bar, a preset row, a live
preview, and the hex as an editable escape hatch rather than the only door.

RENDERING. A terminal cell is a coarse pixel, so the field uses the half-block
trick: every "▀" character carries TWO vertical cells — foreground paints the
top, background paints the bottom — doubling vertical resolution. The SV field
is GRID_W x GRID_H logical cells drawn in GRID_H/2 rows of text; the hue bar is
one row. Mouse clicks map back through the same geometry, and arrow keys walk
the grid for terminals where the mouse is a lie.

The conversions live here as pure functions so the mapping is testable without
a terminal: hsv->hex, hex->hsv, and the click->cell geometry.
"""
from __future__ import annotations

import colorsys

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

GRID_W = 48   # saturation axis (left 0 -> right 1)
GRID_H = 24   # value axis, logical cells (top 1 -> bottom 0); GRID_H/2 text rows
HUE_W = 48    # hue bar cells across 0..360

HALF = "▀"  # upper half block


# ── pure conversions ─────────────────────────────────────────────────────────

def hsv_to_hex(h: float, s: float, v: float) -> str:
    """h in [0,1), s/v in [0,1] -> '#rrggbb'."""
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, min(max(s, 0.0), 1.0), min(max(v, 0.0), 1.0))
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def hex_to_hsv(hexcolor: str) -> tuple[float, float, float]:
    """'#rrggbb' -> (h, s, v). Raises ValueError on a malformed hex."""
    t = hexcolor.strip().lstrip("#")
    if len(t) != 6 or any(c not in "0123456789abcdefABCDEF" for c in t):
        raise ValueError(f"not a #RRGGBB hex: {hexcolor!r}")
    r, g, b = (int(t[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(r, g, b)


def cell_to_sv(col: int, row: int) -> tuple[float, float]:
    """SV-field cell -> (saturation, value). row 0 is the TOP (value 1)."""
    col = min(max(col, 0), GRID_W - 1)
    row = min(max(row, 0), GRID_H - 1)
    s = col / (GRID_W - 1)
    v = 1.0 - row / (GRID_H - 1)
    return s, v


def sv_to_cell(s: float, v: float) -> tuple[int, int]:
    return round(s * (GRID_W - 1)), round((1.0 - v) * (GRID_H - 1))


def hue_to_cell(h: float) -> int:
    return min(int((h % 1.0) * HUE_W), HUE_W - 1)


def cell_to_hue(col: int) -> float:
    return min(max(col, 0), HUE_W - 1) / HUE_W


# ── render helpers (pure: return Text) ───────────────────────────────────────

def render_sv_field(h: float, sel_s: float, sel_v: float) -> Text:
    """The saturation/value field at hue h, half-block doubled, with a marker
    at the selected cell (drawn as '+' so it survives any background)."""
    sel_col, sel_row = sv_to_cell(sel_s, sel_v)
    out = Text()
    for tr in range(GRID_H // 2):
        top_row, bot_row = tr * 2, tr * 2 + 1
        for col in range(GRID_W):
            s_top, v_top = cell_to_sv(col, top_row)
            s_bot, v_bot = cell_to_sv(col, bot_row)
            ch = HALF
            if col == sel_col and top_row == sel_row:
                ch = "+"
            elif col == sel_col and bot_row == sel_row:
                ch = "+"
            out.append(ch, Style(color=hsv_to_hex(h, s_top, v_top),
                                 bgcolor=hsv_to_hex(h, s_bot, v_bot)))
        if tr != GRID_H // 2 - 1:
            out.append("\n")
    return out


def render_hue_bar(sel_h: float) -> Text:
    sel = hue_to_cell(sel_h)
    out = Text()
    for col in range(HUE_W):
        hue = cell_to_hue(col)
        ch = "▼" if col == sel else HALF
        out.append(ch, Style(color=hsv_to_hex(hue, 1.0, 1.0),
                             bgcolor=hsv_to_hex(hue, 1.0, 0.55)))
    return out


class ColorPickerScreen(ModalScreen[str | None]):
    """Pick a color; dismisses with '#rrggbb' or None on cancel.

    Presets are the CURRENT theme's resolved tokens — the palette you are
    most likely nudging toward is the one already on screen.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("enter", "accept", "Use color", show=False, priority=True),
        Binding("up", "nudge('up')", show=False),
        Binding("down", "nudge('down')", show=False),
        Binding("left", "nudge('left')", show=False),
        Binding("right", "nudge('right')", show=False),
        Binding("pageup", "nudge('hue_up')", show=False),
        Binding("pagedown", "nudge('hue_down')", show=False),
    ]

    def __init__(self, initial: str = "#808080",
                 presets: list[tuple[str, str]] | None = None,
                 token_name: str = ""):
        super().__init__()
        try:
            self._h, self._s, self._v = hex_to_hsv(initial)
        except ValueError:
            self._h, self._s, self._v = 0.0, 0.0, 0.5
        self._presets = presets or []
        self._token = token_name

    # -- compose ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="cp-box"):
            yield Static(
                f"Pick {self._token}" if self._token else "Pick a color",
                id="cp-title",
            )
            yield Static(render_sv_field(self._h, self._s, self._v), id="cp-field")
            yield Static(render_hue_bar(self._h), id="cp-hue")
            if self._presets:
                row = Text()
                for name, hexv in self._presets:
                    row.append("  ", Style(bgcolor=hexv))
                    row.append(" ")
                yield Static(row, id="cp-presets")
            with Horizontal(id="cp-row"):
                yield Static(Text("      ", Style(bgcolor=self.value)), id="cp-swatch")
                yield Input(value=self.value, id="cp-hex")
                yield Label("Enter = use · Esc = cancel · arrows/PgUp/PgDn", id="cp-hint")

    @property
    def value(self) -> str:
        return hsv_to_hex(self._h, self._s, self._v)

    def _repaint(self) -> None:
        self.query_one("#cp-field", Static).update(
            render_sv_field(self._h, self._s, self._v))
        self.query_one("#cp-hue", Static).update(render_hue_bar(self._h))
        self.query_one("#cp-swatch", Static).update(Text("      ", Style(bgcolor=self.value)))
        hexbox = self.query_one("#cp-hex", Input)
        if hexbox.value.strip().lower() != self.value:
            hexbox.value = self.value

    # -- input --------------------------------------------------------------
    def on_click(self, event) -> None:
        w = getattr(event, "widget", None)
        wid = getattr(w, "id", None)
        if wid == "cp-field":
            col = int(event.x)
            row = int(event.y) * 2  # each text row is two logical cells
            self._s, self._v = cell_to_sv(col, row)
            self._repaint()
        elif wid == "cp-hue":
            self._h = cell_to_hue(int(event.x))
            self._repaint()
        elif wid == "cp-presets" and self._presets:
            # each preset paints 2 cells + 1 gap = 3 columns
            idx = int(event.x) // 3
            if 0 <= idx < len(self._presets):
                try:
                    self._h, self._s, self._v = hex_to_hsv(self._presets[idx][1])
                    self._repaint()
                except ValueError:
                    pass

    def on_input_submitted(self, event) -> None:
        if getattr(event.input, "id", None) != "cp-hex":
            return
        event.stop()
        try:
            self._h, self._s, self._v = hex_to_hsv(event.value)
            self._repaint()
        except ValueError:
            self.notify(f"Not a #RRGGBB hex: {event.value!r}", severity="warning")

    def action_nudge(self, direction: str) -> None:
        step = 1 / (GRID_W - 1)
        vstep = 1 / (GRID_H - 1)
        if direction == "left":
            self._s = max(0.0, self._s - step)
        elif direction == "right":
            self._s = min(1.0, self._s + step)
        elif direction == "up":
            self._v = min(1.0, self._v + vstep)
        elif direction == "down":
            self._v = max(0.0, self._v - vstep)
        elif direction == "hue_up":
            self._h = (self._h + 1 / HUE_W) % 1.0
        elif direction == "hue_down":
            self._h = (self._h - 1 / HUE_W) % 1.0
        self._repaint()

    def action_accept(self) -> None:
        # An edited-but-unsubmitted hex still wins if it parses — the box is
        # the escape hatch, and losing a typed value to a stale grid state
        # would make the hatch a decoy.
        raw = self.query_one("#cp-hex", Input).value.strip()
        try:
            self._h, self._s, self._v = hex_to_hsv(raw)
        except ValueError:
            pass
        self.dismiss(self.value)

    def action_cancel(self) -> None:
        self.dismiss(None)
