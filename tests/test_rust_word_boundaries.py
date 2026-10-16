"""Validate the offline word oracle against real Textual Input actions."""
from __future__ import annotations

import asyncio
import bisect
import importlib.util
import re
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Input
from textual.widgets._input import Selection

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/export_rust_word_boundaries.py"
SPEC = importlib.util.spec_from_file_location("word_export", SCRIPT)
assert SPEC and SPEC.loader
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)


def test_exported_ranges_match_python_regex_for_every_unicode_scalar():
    ranges = export.word_ranges()
    starts = [start for start, _ in ranges]
    word = re.compile(r"\w")
    for code in range(0x110000):
        if 0xD800 <= code <= 0xDFFF:
            continue
        index = bisect.bisect_right(starts, code) - 1
        represented = index >= 0 and code <= ranges[index][1]
        assert represented == bool(word.fullmatch(chr(code))), hex(code)


def test_word_oracle_matches_mounted_textual_actions_and_selections():
    class InputApp(App):
        def compose(self) -> ComposeResult:
            yield Input(id="prompt")

    async def check():
        async with InputApp().run_test() as pilot:
            widget = pilot.app.query_one(Input)
            for case in export.oracle()["cases"]:
                text, cursor = case["text"], case["cursor"]
                for anchor in {0, cursor, len(text)}:
                    for direction in ("left", "right"):
                        target = case[direction]
                        for select in (False, True):
                            widget.value = text
                            widget.selection = Selection(anchor, cursor)
                            getattr(widget, f"action_cursor_{direction}_word")(select)
                            assert widget.selection == Selection(anchor if select else target, target), case
                        widget.value = text
                        widget.selection = Selection(anchor, cursor)
                        getattr(widget, f"action_delete_{direction}_word")()
                        start, end = sorted((anchor, cursor) if anchor != cursor else (cursor, target))
                        assert widget.value == text[:start] + text[end:], case
                        assert widget.cursor_position == start, case
    asyncio.run(check())


def test_committed_outputs_are_current():
    assert export.TABLE.read_text(encoding="utf-8") == export.render_table()
    import json
    assert json.loads(export.FIXTURE.read_text(encoding="utf-8")) == export.oracle()
