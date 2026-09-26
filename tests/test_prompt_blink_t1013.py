"""T1013 - an idle prompt's cursor blink must not repaint the whole prompt box.

Measured in T1003 (5ab5f38): an idle seat wrote ~25 KB / 10 s, all of it on the
prompt box rows - every 0.5 s blink repainted the whole bordered box, ~1.25 KB
a time, which fills LiteSuite's 32 KB /pty/read ring in ~13 s.

Driven against the REAL app through a driver that captures every write
(a HeadlessDriver that claims not to be headless, so Textual renders exactly as
it would to a terminal).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from textual.drivers.headless_driver import HeadlessDriver  # noqa: E402

from litetui import app as m  # noqa: E402

HOST_ENV = ("LITESUITE_CANVAS_AGENT", "LITEHARNESS_TIER",
            "LITEHARNESS_AGENT_NAME", "LITEHARNESS_SPAWNED_BY")


@pytest.fixture
def plain_terminal(monkeypatch):
    for name in HOST_ENV:
        monkeypatch.delenv(name, raising=False)


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


@pytest.mark.asyncio
async def test_an_idle_blink_repaints_only_the_cursor_cell(plain_terminal) -> None:
    a = make_app()
    async with a.run_test(size=(120, 34), headless=False) as pilot:
        box = a.query_one("#message-input")
        await pilot.pause(2.0)  # boot settles
        assert box.has_focus and box.cursor_blink
        a.writes.clear()
        await pilot.pause(10.0)
        out = "".join(a.writes)
        # The blink still runs: ~20 toggles in 10 s, each one a write.
        assert len(a.writes) >= 10, f"only {len(a.writes)} writes: the blink stopped"
        assert len(out) <= 2000, f"{len(out)} bytes in 10 s idle"
        # Nothing but the cursor cell: no border, no placeholder tail.
        assert "▔" not in out and "Ctrl+V" not in out


@pytest.mark.asyncio
async def test_the_cursor_is_drawn_after_typing_and_blinks_on(plain_terminal) -> None:
    a = make_app()
    async with a.run_test(size=(120, 34), headless=False) as pilot:
        box = a.query_one("#message-input")
        await pilot.pause(1.0)
        box.value = "hello"
        box.cursor_position = 5
        await pilot.pause(0.3)
        assert "hello" in "".join(a.writes)
        a.writes.clear()
        await pilot.pause(1.2)  # two or more blinks at the NEW cursor cell
        cells = [w for w in a.writes if "\x1b[29;" in w]
        assert len(cells) >= 2, a.writes


@pytest.mark.asyncio
async def test_a_hosted_seat_is_silent_and_keeps_a_steady_cursor(plain_terminal, monkeypatch) -> None:
    monkeypatch.setenv("LITESUITE_CANVAS_AGENT", "true")
    a = make_app()
    async with a.run_test(size=(120, 34), headless=False) as pilot:
        box = a.query_one("#message-input")
        await pilot.pause(2.0)
        a.writes.clear()
        await pilot.pause(10.0)
        out = "".join(a.writes)
        assert len(out) <= 300, f"{len(out)} bytes in 10 s idle under the host"
        assert box.has_focus and not box.cursor_blink and box._cursor_visible
