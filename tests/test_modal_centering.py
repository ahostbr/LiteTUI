"""Every modal box sits centred in the terminal, both axes.

The clickable-calendar screens shipped docked to the TOP-LEFT: the house
centering rule is one CSS selector list (`ConfirmStop, PickerScreen, ... {
align: center middle; }`), and the three new screens were never added to it.
CalendarScreen HID the defect by being 96% x 92% -- near-fullscreen makes
docked and centred look identical -- and the render pass printed every
widget's CONTENT without once asking where the widget SAT. Ryan's screenshot
was the instrument, again.

This asserts POSITION, not content: the box's gap to the left edge equals its
gap to the right, top equals bottom, inside a real running app. Driven over
every modal that claims the centering rule, so the NEXT screen that forgets
to join the selector list fails here instead of in a screenshot.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui import paths
from litetui import scheduler as sched_mod


def _cal(a):
    """The calendar's BODY — where the hit-map and the month state live.

    They moved out of `CalendarScreen` so a `SidePanel` can mount the same
    widget (T232). Reaching through `a.screen` still works for `query_one`,
    which searches descendants; it is the direct attribute access that had to
    follow the state. Written as a helper rather than repeated, so the day the
    calendar is DOCKED in these tests there is one line to change.
    """
    from litetui.plugins.scheduler_ui import CalendarBody
    return a.screen.query_one(CalendarBody)



@pytest.fixture(autouse=True)
def _never_write_the_live_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.jobs[:] = [sched_mod.Job(prompt="p", schedule="@daily", label="x")]
    return a


def _run(coro):
    return asyncio.run(coro)


SIZE = (190, 48)


def _assert_centred(app, box_id: str, what: str) -> None:
    box = app.screen.query_one(box_id)
    region = box.region
    screen_w, screen_h = app.screen.size

    left = region.x
    right = screen_w - (region.x + region.width)
    top = region.y
    bottom = screen_h - (region.y + region.height)

    # A centred box may be off by one when the leftover space is odd; more
    # than that is docking. And a box at 0,0 with slack on the other side is
    # THE defect this file exists for -- name it when it happens.
    assert abs(left - right) <= 1, (
        f"{what}: {box_id} is horizontally docked -- {left} cols left, "
        f"{right} right (region={region})"
    )
    assert abs(top - bottom) <= 1, (
        f"{what}: {box_id} is vertically docked -- {top} rows above, "
        f"{bottom} below (region={region})"
    )


def test_the_day_popup_is_centred():
    """The one Ryan's screenshot caught docked in the top-left corner."""
    async def body():
        a = make_app()
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            _cal(a).open_day(15)
            await pilot.pause()
            _assert_centred(a, "#day-box", "DayScreen")
    _run(body())


def test_the_job_editor_is_centred():
    async def body():
        a = make_app()
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            _cal(a).open_day(15)
            await pilot.pause()
            a.screen.action_new_job()
            await pilot.pause()
            _assert_centred(a, "#job-box", "JobScreen")
    _run(body())


def test_the_calendar_itself_is_centred():
    """At 96% x 92% docked and centred are two cells apart -- which is still
    the difference between joined-the-rule and forgot-the-rule, and the next
    resize would widen it."""
    async def body():
        a = make_app()
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            _assert_centred(a, "#cal-box", "CalendarScreen")
    _run(body())


def test_the_help_screen_is_centred_too():
    """The pre-existing modals, as the control arm: if these fail, the rule
    itself broke, not a screen's membership of it."""
    async def body():
        a = make_app()
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/help")
            await pilot.pause()
            _assert_centred(a, "#help-box", "HelpScreen")
    _run(body())
