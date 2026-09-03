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
from textual.widget import Widget
from textual.widgets import Input, Label, Static

from litetui.side_panel import SwapButton, close_dialog

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


MIN_W = 8     # narrower than this the field is not a picker, it is a stripe


def clamp_width(width: int | None) -> int:
    """The painting width, clamped. `None` means "the full-size field"."""
    if not width:
        return GRID_W
    return max(MIN_W, min(GRID_W, int(width)))


# 🔴 THE WIDTH IS A PARAMETER BECAUSE THE PICKER IS NO LONGER ALWAYS 48 COLUMNS
# WIDE. A docked picker lives in a strip that caps at 60 and resolves to 39 on a
# 120-column terminal. Measured against the un-parameterised field, sidebar host,
# 40 rows (probe: panel width / #cp-box width / rendered #cp-field rows):
#
#     term 100 -> panel 33  box 30  field 24 rows      term 170 -> panel 56  box 53  field 24
#     term 120 -> panel 39  box 36  field 24 rows      term 180 -> panel 59  box 54  field 12
#     term 140 -> panel 46  box 43  field 24 rows      term 200 -> panel 60  box 54  field 12
#
# Below a 180-column terminal every 48-character line SOFT-WRAPPED, so the field
# drew 24 rows for 12 -- and `on_click` maps a click with `row = event.y * 2`,
# which is derived from the UNWRAPPED geometry. So a docked picker below that
# width did not merely look doubled: every click landed on a different colour
# than the one under the cursor. That is the "shipped control that lies" class,
# so the field is painted at the width it actually has instead.
#
# Defaults are the module constants, so every existing call and every existing
# round-trip arm keeps its exact meaning.

def cell_to_sv(col: int, row: int, width: int | None = None) -> tuple[float, float]:
    """SV-field cell -> (saturation, value). row 0 is the TOP (value 1)."""
    w = clamp_width(width)
    col = min(max(col, 0), w - 1)
    row = min(max(row, 0), GRID_H - 1)
    s = col / (w - 1)
    v = 1.0 - row / (GRID_H - 1)
    return s, v


def sv_to_cell(s: float, v: float, width: int | None = None) -> tuple[int, int]:
    w = clamp_width(width)
    return round(s * (w - 1)), round((1.0 - v) * (GRID_H - 1))


def hue_to_cell(h: float, width: int | None = None) -> int:
    # ⚠️ THE EPSILON IS NOT DEFENSIVE, IT IS THE ROUND-TRIP. `cell_to_hue(15, 44)`
    # is 15/44 = 0.34090909..., and `int(0.34090909... * 44)` is 14 — the float
    # lands a hair BELOW the integer it came from and truncation loses a whole
    # cell. Invisible at the old fixed HUE_W of 48 (every 1/48 is exact in
    # binary); the width parameter is what exposed it, and it is a real click
    # error, not a test artefact: clicking column 15 would select column 14.
    w = clamp_width(width)
    return min(int((h % 1.0) * w + 1e-9), w - 1)


def cell_to_hue(col: int, width: int | None = None) -> float:
    w = clamp_width(width)
    return min(max(col, 0), w - 1) / w


# ── render helpers (pure: return Text) ───────────────────────────────────────

def render_sv_field(h: float, sel_s: float, sel_v: float,
                    width: int | None = None) -> Text:
    """The saturation/value field at hue h, half-block doubled, with a marker
    at the selected cell (drawn as '+' so it survives any background).

    `no_wrap` is load-bearing, not cosmetic -- the calendar grid carries it for
    the same reason. If the painted width is ever one frame stale (a resize
    lands between the paint and the layout), cropping keeps the field 12 rows
    tall and the click map honest; wrapping would double the height and shear
    every mapped row below the fold.
    """
    w = clamp_width(width)
    sel_col, sel_row = sv_to_cell(sel_s, sel_v, w)
    out = Text(no_wrap=True)
    for tr in range(GRID_H // 2):
        top_row, bot_row = tr * 2, tr * 2 + 1
        for col in range(w):
            s_top, v_top = cell_to_sv(col, top_row, w)
            s_bot, v_bot = cell_to_sv(col, bot_row, w)
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


def render_hue_bar(sel_h: float, width: int | None = None) -> Text:
    w = clamp_width(width)
    sel = hue_to_cell(sel_h, w)
    out = Text(no_wrap=True)
    for col in range(w):
        hue = cell_to_hue(col, w)
        ch = "▼" if col == sel else HALF
        out.append(ch, Style(color=hsv_to_hex(hue, 1.0, 1.0),
                             bgcolor=hsv_to_hex(hue, 1.0, 0.55)))
    return out


class _SVField(Static):
    """The saturation/value field — AND THE THING THE ARROW KEYS DRIVE, so it is
    what takes focus.

    🔴 FOUR OF THE PICKER'S SEVEN KEYS WERE DEAD AND THIS IS WHY. Textual focuses
    the first focusable widget when a screen mounts. Until now the ONLY focusable
    thing in this dialog was `Input#cp-hex`, and an `Input` handles
    left/right/pageup/pagedown itself, so those four never reached
    `action_nudge`. Measured at 7b42eee with a pilot, modal path:

        focused on mount   Input#cp-hex
        right              #808080 -> #808080     the Input ate it
        up                 #808080 -> #8b8b8b     reached action_nudge
        pageup             #8b8b8b -> #8b8b8b     the Input ate it

    Only `enter` worked, because only `enter` was `priority`. The hint label has
    been promising "arrows/PgUp/PgDn" the whole time.

    ⚠️ THE OTHER CANDIDATE FIX WAS MEASURED AND REJECTED, NOT ARGUED AWAY.
    Making the four bindings `priority=True` and taking them off the Input does
    make them fire — and it takes the HEX BOX'S CURSOR KEYS WITH IT. Minimal
    Textual 8.0.2 app, an Input focused with the caret at column 3:

        priority right   cursor 3 -> 3   binding fired=1     <- the box is stuck
        plain    left    cursor 3 -> 2   binding fired=0     <- control
        priority pageup                  binding fired=1

    A priority binding preempts the focused widget by design, so option (B)
    would trade four dead keys for a hex field you cannot move the caret in —
    and that field is the picker's documented escape hatch ("the box is the
    escape hatch, and a hatch that loses what you typed is a decoy").

    ⇒ So the FIELD becomes focusable and owns the keys. Focus starts here
    because this widget is first in the DOM; Tab or a click reaches the hex box,
    where the same arrows go back to being cursor movement. Both work, neither
    is stolen, and where the arrows apply is visible from the focus tint.
    """

    can_focus = True

    #: `tint`, NOT a border. The focus has to be VISIBLE — a focusable widget
    #: that looks identical focused and unfocused is a dialog where the keys
    #: work and nobody can tell which surface they are aimed at. But a border
    #: would take two columns and two rows off a drawing whose width is now
    #: measured and whose click map is derived from it (see `clamp_width`), so
    #: the indicator must not change the box. `tint` overlays without resizing.
    DEFAULT_CSS = """
    _SVField:focus { tint: $accent 10%; }
    """

    BINDINGS = [
        Binding("up", "nudge('up')", "Lighter", show=False),
        Binding("down", "nudge('down')", "Darker", show=False),
        Binding("left", "nudge('left')", "Less saturated", show=False),
        Binding("right", "nudge('right')", "More saturated", show=False),
        Binding("pageup", "nudge('hue_up')", "Hue +", show=False),
        Binding("pagedown", "nudge('hue_down')", "Hue -", show=False),
    ]

    def _picker(self):
        """The body that owns the colour. Walks ancestors rather than reading
        `self.screen`, so this works in a modal AND in a `SidePanel` — the
        coupling that would otherwise go inert with no exception."""
        for node in self.ancestors_with_self:
            if isinstance(node, ColorPickerBody):
                return node
        return None

    def action_nudge(self, direction: str) -> None:
        picker = self._picker()
        if picker is not None:
            picker.action_nudge(direction)


#: The keys the picker answers to, declared once and installed on BOTH the body
#: and the screen. A ModalScreen is what has focus on the modal path; the body is
#: what a `SidePanel` mounts. One list means the two hosts cannot drift apart on
#: which keys work, which is how a converted dialog quietly becomes two dialogs.
#:
#: `escape` is deliberately NOT here: `SidePanel` binds it to cancel, and a second
#: binding for the same key one level down is a coin toss over which fires. The
#: screen below adds its own, exactly as it always had.
_PICKER_KEYS = [
    Binding("enter", "accept", "Use color", show=False, priority=True),
    Binding("up", "nudge('up')", show=False),
    Binding("down", "nudge('down')", show=False),
    Binding("left", "nudge('left')", show=False),
    Binding("right", "nudge('right')", show=False),
    Binding("pageup", "nudge('hue_up')", show=False),
    Binding("pagedown", "nudge('hue_down')", show=False),
]


class ColorPickerBody(Widget):
    """The picker content, host-agnostic. Exits through `close_dialog`.

    ⚠️ FOUR OF THE SEVEN KEYS ABOVE ARE ALREADY DEAD ON THE SHIPPED MODAL, AND
    THIS CONVERSION NEITHER CAUSES THAT NOR FIXES IT. Measured before the split,
    at 7b42eee, by pushing `ColorPickerScreen` and pressing keys through a pilot:

        focused on mount   Input#cp-hex   (the ONLY focusable in the dialog)
        right              #808080 -> #808080     the Input eats it (cursor)
        up                 #808080 -> #8b8b8b     reaches `action_nudge`
        pageup             #8b8b8b -> #8b8b8b     the Input eats it

    Textual focuses the first focusable when a screen mounts, and `Input` handles
    left/right/pageup/pagedown itself; none of those bindings is `priority`, so
    they never reach this level while the hex box holds focus. Only `enter` is
    priority, which is why Enter works and the arrows are a coin toss decided by
    what has focus. The hint label still says "arrows/PgUp/PgDn".

    ⇒ It is REPORTED, not repaired here: fixing it is a focus-model decision
    (make the field focusable and give it the keys, or make the bindings
    priority and take them off the Input) and this commit is a host split. What
    the split MUST do is not change the answer, and it does not: `SidePanel`'s
    `_take_focus` walks `query("*")` in DOM order and lands on the same
    `Input#cp-hex`, so both hosts are equally alive and equally dead.
    """

    #: `width: auto; height: auto` so the host's own `align: center middle`
    #: centres the BOX and not a full-width wrapper -- ConfirmStopBody's shape,
    #: and for its reason. `#cp-box` carries no screen-relative percentage, so
    #: unlike HelpBody there is no base to re-home.
    DEFAULT_CSS = """
    ColorPickerBody { width: auto; height: auto; layout: vertical; }
    """

    BINDINGS = list(_PICKER_KEYS)

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
        #: The width the field was LAST PAINTED AT. `on_click` maps through this
        #: exact number rather than `GRID_W`, so the map can never disagree with
        #: the drawing -- the calendar's rule ("the hit-map is built beside the
        #: paint") applied to a picker that is no longer a fixed size.
        self._painted_w = GRID_W

    def _avail_width(self) -> int:
        """Columns the field has to paint into, or the full width if unknown.

        Zero before the first layout, which `clamp_width` reads as "full size" --
        the modal answer, and the right one to start from.
        """
        try:
            return clamp_width(self.query_one("#cp-field", _SVField).content_size.width)
        except Exception:
            return GRID_W

    # -- compose ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="cp-box"):
            yield Static(
                f"Pick {self._token}" if self._token else "Pick a color",
                id="cp-title",
            )
            yield _SVField(render_sv_field(self._h, self._s, self._v), id="cp-field")
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
                # The hint names WHERE the keys apply now that they work.
                # "arrows/PgUp/PgDn" alone was true of no focus state at all.
                yield Label("Enter = use · Esc = cancel · arrows on the field · "
                            "Tab for hex", id="cp-hint")
            # On its own line, NOT in `#cp-row`: the box is 54 columns and that
            # row already holds an 8-column swatch, a 12-column hex field and
            # the hint. An `.inline` button there would take the hint's space,
            # and the hint is the only thing that names the keys.
            yield SwapButton()

    @property
    def value(self) -> str:
        return hsv_to_hex(self._h, self._s, self._v)

    def on_mount(self) -> None:
        # The first paint happens in `compose` at full width, before any layout
        # exists. Repaint once the real width is known, or a docked picker shows
        # a 48-column field cropped to the panel until the user touches it.
        self.call_after_refresh(self._repaint)

    def on_resize(self, _event) -> None:
        # The field is DRAWN to fit, so a resize is a repaint, not a reflow --
        # CalendarScreen's rule, and the same drawing-not-layout reason.
        self._repaint()

    #: Bounded re-defers while waiting for compose. `_ViewMixin._settle` carries
    #: the same counter for the same reason: a deferred callback can land before
    #: the children exist AND after the view is gone, and `query_one` raises in
    #: both states.
    _paint_tries: int = 4

    def _repaint(self) -> None:
        # ⚠️ THE LIVENESS CHECK GUARDS THE DEFERRED RE-ENTRY, NOT THE ENTRY.
        # `self.is_mounted` is False inside a widget's own `on_mount` (measured
        # on DayBody: children present, query matching, is_mounted False), so a
        # check at the top of a paint method silently skips the first paint.
        # This one is reached through `call_after_refresh` today and would not
        # have shown it -- it is written the safe way round anyway, because the
        # next caller is what makes the difference and it is not here yet.
        if not self.query("#cp-field"):
            if self._paint_tries > 0 and self.is_mounted:
                self._paint_tries -= 1
                self.call_after_refresh(self._repaint)
            return
        w = self._avail_width()
        self._painted_w = w
        self.query_one("#cp-field", _SVField).update(
            render_sv_field(self._h, self._s, self._v, w))
        self.query_one("#cp-hue", Static).update(render_hue_bar(self._h, w))
        self.query_one("#cp-swatch", Static).update(Text("      ", Style(bgcolor=self.value)))
        hexbox = self.query_one("#cp-hex", Input)
        if hexbox.value.strip().lower() != self.value:
            hexbox.value = self.value

    # -- state carry across a live host swap --------------------------------
    def get_state(self) -> dict:
        """The COLOUR is the state, and the typed hex outranks the grid.

        A swap that rebuilt the picker at the colour it OPENED with would throw
        away every nudge and every typed value silently -- the failure
        `DialogController._mount_view` was rewritten to stop. The hex box wins
        when it parses, for `action_accept`'s reason: the box is the escape
        hatch, and a hatch that loses what you typed is a decoy.
        """
        raw = self.query_one("#cp-hex", Input).value.strip()
        try:
            hex_to_hsv(raw)
        except ValueError:
            return {"hex": self.value}
        return {"hex": raw}

    def set_state(self, state: dict) -> None:
        raw = state.get("hex")
        if not raw:
            return
        try:
            self._h, self._s, self._v = hex_to_hsv(raw)
        except ValueError:
            return
        self._repaint()

    # -- input --------------------------------------------------------------
    def on_click(self, event) -> None:
        w = getattr(event, "widget", None)
        wid = getattr(w, "id", None)
        if wid == "cp-field":
            col = int(event.x)
            row = int(event.y) * 2  # each text row is two logical cells
            self._s, self._v = cell_to_sv(col, row, self._painted_w)
            self._repaint()
        elif wid == "cp-hue":
            self._h = cell_to_hue(int(event.x), self._painted_w)
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
        # One PAINTED cell, not one full-size cell: in a 36-column panel a
        # GRID_W step would move the marker by more than a cell and the arrows
        # would skip colours the field is showing.
        step = 1 / max(1, self._painted_w - 1)
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
        close_dialog(self, self.value)

    def action_cancel(self) -> None:
        close_dialog(self, None)


class ColorPickerScreen(ModalScreen[str | None]):
    """Pick a color; dismisses with '#rrggbb' or None on cancel.

    Presets are the CURRENT theme's resolved tokens — the palette you are
    most likely nudging toward is the one already on screen.

    The content lives in `ColorPickerBody` so a sidebar can mount the SAME
    widget. This screen is not replaced by `_ModalHost`: `app.py` styles it by
    class name (`ColorPickerScreen { align: center middle; }`), so routing the
    modal path through the generic host would render it uncentred.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        *_PICKER_KEYS,
    ]

    def __init__(self, initial: str = "#808080",
                 presets: list[tuple[str, str]] | None = None,
                 token_name: str = ""):
        super().__init__()
        self._initial = initial
        self._presets = presets or []
        self._token = token_name

    def compose(self) -> ComposeResult:
        yield ColorPickerBody(self._initial, self._presets, self._token)

    @property
    def value(self) -> str:
        """The live colour, read off the body. Kept so a caller that held the
        SCREEN and asked it for a value still gets one after the split."""
        return self.query_one(ColorPickerBody).value

    def _body(self) -> ColorPickerBody:
        return self.query_one(ColorPickerBody)

    def action_nudge(self, direction: str) -> None:
        self._body().action_nudge(direction)

    def action_accept(self) -> None:
        self._body().action_accept()

    def action_cancel(self) -> None:
        self.dismiss(None)
