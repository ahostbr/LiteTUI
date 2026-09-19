"""A second `chrome shot` of an unchanged page must NOT read like the first.

Measured 2026-09-19 (convo b4c93292, "the agents got lost"): a stuck model
called `shot` 16+ times and got the same string back every time, so the
(call, result) pair carried zero new information and the loop had no exit.
The page rarely changes between two shots seconds apart, so the honest second
answer is a different one: fingerprint each PNG and say plainly when it is
unchanged — and name the way out (scroll), because scroll did not exist as a
verb at the time and "I can't see the rest of the page" had no answer.

These tests exercise the pure half (`_shot_result`) only; the run() branch
that calls it is the two-line wiring the autostage tests cover from the app
side.
"""
from __future__ import annotations

import litetui.chrome_tool as chrome_tool


def _reset():
    """Module state is per-process; tests must not inherit each other's shots."""
    chrome_tool.last_shot_identical = False
    chrome_tool._LAST_SHOT_HASH = None


def test_first_shot_reports_saved(tmp_path):
    _reset()
    shot = tmp_path / "chrome-shot.png"
    shot.write_bytes(b"png-bytes-1")
    out = chrome_tool._shot_result(shot)
    assert "Saved" in out
    assert chrome_tool.last_shot_identical is False


def test_identical_second_shot_says_the_page_did_not_change(tmp_path):
    _reset()
    shot = tmp_path / "chrome-shot.png"
    shot.write_bytes(b"png-bytes-1")
    chrome_tool._shot_result(shot)
    out = chrome_tool._shot_result(shot)
    assert chrome_tool.last_shot_identical is True
    assert "NOT changed" in out
    # The loop-exit must be NAMED, not just implied: the verb that changes
    # what the next shot sees.
    assert "scroll" in out.lower()


def test_changed_shot_resets_the_flag(tmp_path):
    _reset()
    shot = tmp_path / "chrome-shot.png"
    shot.write_bytes(b"png-bytes-1")
    chrome_tool._shot_result(shot)
    assert chrome_tool.last_shot_identical is False
    shot.write_bytes(b"png-bytes-2")
    out = chrome_tool._shot_result(shot)
    assert chrome_tool.last_shot_identical is False
    assert "Saved" in out


def test_a_missing_file_is_never_identical(tmp_path):
    _reset()
    missing = tmp_path / "chrome-shot.png"
    out = chrome_tool._shot_result(missing)
    assert chrome_tool.last_shot_identical is False
    assert "Saved" in out
