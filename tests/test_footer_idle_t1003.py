"""T1003 - an idle footer stays still, and never tears.

The user (with two screenshots): "some thing is wrong with the litetui command
template thing it keeps flickering like that" - the palette button alternated
between "☰ commands" and "☰ coommands" on an idle seat.

Two causes, both measured here against the REAL app through a driver that
captures every write (T981's method: a HeadlessDriver that claims not to be
headless, so Textual renders exactly as it would to a terminal):

- The glyph. Rich sizes U+2630 at 2 cells (Unicode 16 made the trigrams wide);
  its East Asian Width is still N, and a terminal on older tables draws it in 1.
  Textual then places "commands" one column right of where the terminal puts
  it, so a repaint starting mid-button lands on the wrong cell: "☰ c" + "oommands".
- The repaint. `Static.content = x` refreshes layout even when x is what is
  already there, so every same-text `_refresh_ctx_label` (the 15 s Claude cache
  ticker, each Codex task event, the footer's own resize handler) rewrote the
  footer row - re-tearing the button each time.
"""
from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from rich.cells import cell_len  # noqa: E402
from textual.drivers.headless_driver import HeadlessDriver  # noqa: E402

from litetui import app as m  # noqa: E402
from litetui import widgets  # noqa: E402

ROWS = 34
# T1113: status is the upper row of the two-row footer.
FOOTER_ROW = f"\x1b[{ROWS - 1};"  # the cursor move that starts a write on the footer row


class CaptureDriver(HeadlessDriver):
    @property
    def is_headless(self):
        return False

    def write(self, data):
        self._app.writes.append(data)


def make_app():
    class Capture(m.LiteTUI):
        def __init__(self):
            self.writes = []
            super().__init__()
            self.driver_class = CaptureDriver

    a = Capture()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.convo_id = "c1"
    return a


def footer_writes(a) -> str:
    return "".join(w for w in a.writes if FOOTER_ROW in w)


def footer_text(a) -> str:
    """What the footer writes SAY, escape sequences removed. The capture driver
    splits a write every ~16 cells with a cursor move to the NEXT cell, so the
    drawn text is contiguous while the raw bytes are not: a literal search in the
    raw writes missed "thin|k:high" once the text before it changed length
    (T1049: "|| interactive on" is one cell longer than ">> autonomous on")."""
    import re
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", footer_writes(a))


def terminal_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


@pytest.mark.asyncio
async def test_footer_glyphs_are_measured_the_way_a_terminal_draws_them() -> None:
    # A glyph Rich and the terminal disagree on shifts everything after it by
    # one cell on screen but not in Textual's model of the screen. Read from
    # the MOUNTED button, so the arm tests what is drawn, not a constant.
    a = make_app()
    async with a.run_test(size=(120, ROWS), headless=False) as pilot:
        await pilot.pause(0.5)
        labels = [str(w.content) for w in a.query(".pause-button, .mic-button")]
    labels += [widgets.PauseButton.LABEL_RUN, widgets.PauseButton.LABEL_PAUSED,
               widgets.MicButton.LABEL_IDLE, widgets.MicButton.LABEL_REC]
    for label in labels:
        assert cell_len(label) == terminal_width(label), repr(label)


def test_no_text_still_names_the_trigram_button():
    # The Themes help pointed at "the footer's ☰ commands button" after the
    # button itself changed. Every spelling: the glyph and its three escapes.
    src = Path(__file__).resolve().parent.parent / "src" / "litetui"
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for spelling in ("☰", "\\u2630", "\\U00002630",
                         "\\N{TRIGRAM FOR HEAVEN}"):
            assert spelling not in text, (path.name, spelling)


@pytest.mark.asyncio
async def test_an_idle_footer_neither_recomposes_nor_repaints(monkeypatch) -> None:
    recomposes = []
    real = widgets.ContextFooter.recompose

    async def counted(self):
        recomposes.append(1)
        return await real(self)

    monkeypatch.setattr(widgets.ContextFooter, "recompose", counted)
    a = make_app()
    async with a.run_test(size=(120, ROWS), headless=False) as pilot:
        a.ctx_max = 200_000
        a.ctx_used = 54_321
        await pilot.pause(2.0)  # boot settles: first layout, workers, timers
        recomposes.clear()
        a.writes.clear()
        # What an idle seat's timers do: the Claude cache ticker (every 15 s)
        # and Codex task events call this with nothing changed.
        for _ in range(20):
            a._refresh_ctx_label()
            await pilot.pause(0.5)
        out = footer_writes(a)
        assert len(recomposes) == 0, f"{len(recomposes)} recomposes in 10 s idle"
        assert out == "", f"{len(out)} footer bytes in 10 s idle"
        # No "coommands" assertion: the tear happens terminal-side, so Textual
        # never writes that string. The glyph arm above is what guards it.


@pytest.mark.asyncio
async def test_a_real_change_still_repaints_the_footer() -> None:
    a = make_app()
    async with a.run_test(size=(120, ROWS), headless=False) as pilot:
        a.ctx_max = 200_000
        a.ctx_used = 12_345
        await pilot.pause(2.0)
        a.writes.clear()
        a.ctx_used = 54_321
        await pilot.pause(0.5)
        assert "54,321" in footer_text(a)
        a.writes.clear()
        a.ctx_used = 180_000  # crosses 90%: the percent AND its colour change
        await pilot.pause(0.5)
        assert "90%" in footer_text(a)
        a.writes.clear()
        a.thinking_level = "high"
        a._refresh_ctx_label()
        await pilot.pause(0.5)
        assert "think:high" in footer_text(a)
        # NEGATIVE: the instrument reads only what was written. Nothing changed,
        # so nothing is written, and think:high must not be found.
        a.writes.clear()
        a._refresh_ctx_label()
        await pilot.pause(0.5)
        assert "think:high" not in footer_text(a)
