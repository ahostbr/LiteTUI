"""The clickable calendar: day cells open a day popup, jobs open an editor.

Everything here drives the REAL app — real screens, real clicks at real
offsets, the real dispatcher. The feature IS the wiring: a hit-map, three
screens, and a persistence choke point, and none of those can be proven by
looking at the code that defines them.

The load-bearing test is the end-to-end click. It closes a triangle:
  1. the MAP says day 15 lives at (x, y)
  2. the PAINT — checked independently — shows the characters "15" at (x, y)
  3. a real click at (x, y) opens day 15
A shifted map would fail leg 2; a dead handler would fail leg 3. Testing only
leg 1 + 3 would let a map that disagrees with the drawing pass, because both
legs would read from the same wrong structure.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from textual.widgets import OptionList
from litetui.plugins.scheduler_ui import CalendarScreen, DayScreen, JobScreen, _apply_job_edit
from litetui import paths
from litetui import calendar_view as cv
from litetui import scheduler as sched_mod


@pytest.fixture(autouse=True)
def _never_write_the_live_jobs_file(tmp_path, monkeypatch):
    """Redirect the job store away from the repo.

    _apply_job_edit persists through sched_mod.save(jobs, ROOT), and ROOT is
    the live checkout. A TEST MUST NEVER WRITE A PATH THE RUNNING APP OWNS.
    """
    monkeypatch.setattr(paths, "ROOT", tmp_path)


def job(prompt="p", schedule="@daily", **kw):
    return sched_mod.Job(prompt=prompt, schedule=schedule, **kw)


def make_app(jobs):
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.jobs[:] = jobs
    return a


def _run(coro):
    return asyncio.run(coro)


SIZE = (190, 48)        # a real terminal, not pytest's 80x24 default


def _locate_day(scr, day: int) -> tuple[int, int]:
    """(x, y) of a day's number, from the screen's own map."""
    for wk, week in enumerate(scr._matrix):
        if day in week:
            col = week.index(day)
            y = scr._click_rows.index(wk)       # first mapped line = numbers row
            return col * scr._cell_w, y
    raise AssertionError(f"day {day} not in the painted matrix")


# --------------------------------------------------------------------------
# the map, checked against the paint
# --------------------------------------------------------------------------

def test_the_map_and_the_paint_agree_on_every_day():
    """Leg 1 + 2 of the triangle, for the WHOLE month, not a sample."""
    async def body():
        a = make_app([job(schedule="0 9 * * 1-5", label="standup")])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            scr = a.screen
            plain = str(scr.query_one("#cal-grid", m.Static).render())
            lines = plain.split("\n")

            days = [d for wk in scr._matrix for d in wk if d]
            for day in days:
                x, y = _locate_day(scr, day)
                assert scr._day_at(x, y) == day, f"map broken for day {day}"
                painted = lines[y][x : x + len(str(day))]
                assert painted == str(day), (
                    f"day {day}: map points at (x={x}, y={y}) but the paint "
                    f"there reads {painted!r}"
                )
    _run(body())


def test_dead_space_resolves_to_no_day():
    async def body():
        a = make_app([])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            scr = a.screen

            assert scr._day_at(0, 0) is None, "the title row is not a day"
            assert scr._day_at(0, 1) is None, "the day-name row is not a day"
            assert scr._day_at(10_000, 5) is None, "past Sunday is dead space"
            assert scr._day_at(0, 10_000) is None, "below the grid is dead space"

            # A leading cell before day 1 belongs to the previous month.
            first_col = scr._matrix[0].index(
                next(d for d in scr._matrix[0] if d)
            )
            if first_col > 0:
                y = scr._click_rows.index(0)
                assert scr._day_at(0, y) is None
    _run(body())


# --------------------------------------------------------------------------
# the end-to-end click
# --------------------------------------------------------------------------

def test_clicking_a_day_opens_that_day():
    """Leg 3: a REAL mouse click, at map coordinates the paint has vouched
    for, must arrive as an open DayScreen for that exact date."""
    async def body():
        a = make_app([job(schedule="0 9 * * 1-5", label="standup")])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            scr = a.screen
            x, y = _locate_day(scr, 15)

            plain = str(scr.query_one("#cal-grid", m.Static).render())
            assert plain.split("\n")[y][x : x + 2] == "15"   # paint vouches

            await pilot.click("#cal-grid", offset=(x, y))
            await pilot.pause()

            assert isinstance(a.screen, DayScreen), (
                "the click did not open a DayScreen — the handler chain is dead"
            )
            assert a.screen.day == date(scr._year, scr._month, 15)
    _run(body())


def test_clicking_the_title_row_does_nothing():
    async def body():
        a = make_app([])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            before = a.screen
            await pilot.click("#cal-grid", offset=(0, 0))
            await pilot.pause()
            assert a.screen is before, "dead space opened something"
    _run(body())


def test_clicking_a_side_pane_job_opens_its_editor():
    async def body():
        target = job(schedule="0 9 * * *", label="standup")
        a = make_app([target])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            scr = a.screen

            y = scr._side_rows.index(target.id)
            assert scr._job_at(y) == target.id
            assert scr._job_at(0) is None, "the JOBS header is not a job"

            await pilot.click("#cal-side", offset=(4, y))
            await pilot.pause()
            assert isinstance(a.screen, JobScreen)
            assert a.screen._job is target
    _run(body())


# --------------------------------------------------------------------------
# the day popup
# --------------------------------------------------------------------------

def _open_day(a, pilot):
    """Open the current month's day 21 popup through the real command path."""
    a._handle_command("/calendar")
    return a.screen


def test_the_day_popup_lists_the_right_jobs_in_time_order():
    async def body():
        weekday = job(schedule="0 9 * * 1-5", label="standup")
        friday17 = job(schedule="0 17 * * 5", label="review")
        weekend = job(schedule="0 8 * * 6", label="saturday-only")
        # The discriminating case the first fixture lacked: a DISABLED job at
        # 00:00, whose time beats every working job. Sorted by time alone it
        # led the popup as a struck-out row -- what the popup ranks is what
        # WILL happen, so it must sort after the jobs that will.
        off = job(schedule="0 0 * * *", label="retired", enabled=False)
        a = make_app([off, friday17, weekday, weekend])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            # 2026-08-21 is a Friday; navigate the screen to August 2026 so
            # the test does not depend on the wall clock's month.
            scr = a.screen
            scr._year, scr._month = 2026, 8
            scr._paint()
            scr.open_day(21)
            await pilot.pause()

            day = a.screen
            assert isinstance(day, DayScreen)
            ol = day.query_one("#day-list", OptionList)
            ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]

            assert ids == [weekday.id, friday17.id, off.id, "new"], (
                f"expected 09:00 standup, 17:00 review, THEN the disabled job, "
                f"then the create row — got {ids}"
            )
    _run(body())


def test_an_empty_day_offers_creation_not_a_dead_end():
    async def body():
        a = make_app([])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            a.screen.open_day(15)
            await pilot.pause()

            day = a.screen
            ol = day.query_one("#day-list", OptionList)
            ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
            assert "new" in ids
            texts = str(ol.get_option_at_index(0).prompt)
            assert "nothing scheduled" in texts
    _run(body())


def test_a_broken_job_appears_in_the_day_popup_and_is_reachable():
    """A job that can never fire is exactly the one that needs the editor."""
    async def body():
        bad = job(schedule="not a cron", label="bad one")
        a = make_app([bad])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            a.screen.open_day(15)
            await pilot.pause()

            ol = a.screen.query_one("#day-list", OptionList)
            ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
            assert bad.id in ids
    _run(body())


# --------------------------------------------------------------------------
# the editor: edit, validate, create, delete — through real widgets
# --------------------------------------------------------------------------

def test_editing_a_job_saves_persists_and_repaints_the_month(tmp_path):
    async def body():
        target = job(schedule="0 9 * * 1-5", label="standup", prompt="say hi")
        a = make_app([target])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            cal = a.screen
            # Pinned, and to a MONDAY. The first draft opened day 15 of the
            # wall-clock month -- and 2026-08-15 is a Saturday, where a
            # weekday-only job correctly does not appear, so Enter landed on
            # "new job" and the test failed against correct behaviour. A test
            # date must satisfy the schedule it tests.
            cal._year, cal._month = 2026, 8
            cal._paint()
            cal.open_day(17)
            await pilot.pause()

            await pilot.press("enter")          # first row = the job
            await pilot.pause()
            ed = a.screen
            assert isinstance(ed, JobScreen)
            assert ed.query_one("#job-prompt", m.Input).value == "say hi"
            assert ed.query_one("#job-schedule", m.Input).value == "0 9 * * 1-5"

            ed.query_one("#job-label", m.Input).value = "morning"
            await pilot.click("#job-save")
            await pilot.pause()

            assert target.label == "morning", "the edit never reached the job"
            on_disk = sched_mod.load(tmp_path)
            assert [j.label for j in on_disk] == ["morning"], "the edit never persisted"

            # close the day; the month must repaint with the new label
            assert isinstance(a.screen, DayScreen)
            a.screen.action_close()
            await pilot.pause()
            assert a.screen is cal
            plain = str(cal.query_one("#cal-grid", m.Static).render())
            assert "morning" in plain, "the month kept painting the old label"
    _run(body())


def test_an_invalid_schedule_cannot_be_saved_and_names_the_field(tmp_path):
    async def body():
        target = job(schedule="0 9 * * *", label="ok", prompt="hi")
        a = make_app([target])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            a.screen.open_day(15)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            ed = a.screen

            ed.query_one("#job-schedule", m.Input).value = "* 25 * * *"
            await pilot.pause()
            status = str(ed.query_one("#job-status", m.Static).render())
            assert "hour" in status, (
                f"the live preview must name the broken FIELD, got {status!r}"
            )

            await pilot.click("#job-save")
            await pilot.pause()
            assert isinstance(a.screen, JobScreen), "an invalid schedule was saved"
            assert target.schedule == "0 9 * * *", "the job was mutated anyway"
            assert sched_mod.load(tmp_path) == [], "something persisted on refusal"
    _run(body())


def test_an_empty_prompt_cannot_be_saved():
    async def body():
        a = make_app([])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            a.screen.open_day(15)
            await pilot.pause()
            a.screen.action_new_job()
            await pilot.pause()
            ed = a.screen

            await pilot.click("#job-save")      # prompt is empty
            await pilot.pause()
            assert isinstance(a.screen, JobScreen)
            status = str(ed.query_one("#job-status", m.Static).render())
            assert "prompt is empty" in status
    _run(body())


def test_creating_from_a_day_prefills_that_day_and_lands_everywhere(tmp_path):
    async def body():
        a = make_app([])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            cal = a.screen
            cal._year, cal._month = 2026, 8
            cal._paint()
            cal.open_day(15)
            await pilot.pause()
            a.screen.action_new_job()
            await pilot.pause()

            ed = a.screen
            assert ed.query_one("#job-schedule", m.Input).value == "0 9 15 8 *", (
                "creating from a day should prefill that day's date"
            )
            ed.query_one("#job-prompt", m.Input).value = "water the plants"
            ed.query_one("#job-label", m.Input).value = "plants"
            await pilot.click("#job-save")
            await pilot.pause()

            assert len(a.jobs) == 1
            made = a.jobs[0]
            assert made.prompt == "water the plants"
            assert made.enabled is True
            assert made.run_count == 0

            on_disk = sched_mod.load(tmp_path)
            assert [j.prompt for j in on_disk] == ["water the plants"]

            # the day list refreshed to include it
            ol = a.screen.query_one("#day-list", OptionList)
            ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
            assert made.id in ids

            # and the month grid shows it after the popup closes
            a.screen.action_close()
            await pilot.pause()
            plain = str(cal.query_one("#cal-grid", m.Static).render())
            assert "plants" in plain
    _run(body())


def test_deleting_takes_two_clicks_and_then_really_deletes(tmp_path):
    async def body():
        target = job(schedule="@daily", label="doomed", prompt="x")
        a = make_app([target])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            a.screen.open_day(15)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            ed = a.screen

            await pilot.click("#job-delete")
            await pilot.pause()
            assert isinstance(a.screen, JobScreen), "one click must not delete"
            assert target in a.jobs
            assert "Really" in str(ed.query_one("#job-delete", m.Button).label)

            # Textual's Button IGNORES clicks while its -active pressed effect
            # is showing (~0.3s) -- a real debounce, hit here because two
            # programmatic clicks land microseconds apart, which no human
            # double-click does. Waiting it out models the person, and it is
            # also load-bearing for the FEATURE: the arm-then-confirm design
            # inherits a free guarantee that a hardware double-click cannot
            # blow through both stages.
            await pilot.pause(0.4)
            await pilot.click("#job-delete")
            await pilot.pause()
            assert target not in a.jobs
            assert sched_mod.load(tmp_path) == [], "the delete never persisted"
    _run(body())


def test_a_new_job_editor_has_no_delete_button():
    async def body():
        a = make_app([])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            a.screen.open_day(15)
            await pilot.pause()
            a.screen.action_new_job()
            await pilot.pause()
            assert not a.screen.query("#job-delete"), (
                "deleting a job that does not exist yet is not an action"
            )
    _run(body())


def test_escape_cancels_without_touching_anything(tmp_path):
    async def body():
        target = job(schedule="@daily", label="safe", prompt="x")
        a = make_app([target])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            a.screen.open_day(15)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            a.screen.query_one("#job-label", m.Input).value = "typed then regretted"
            await pilot.press("escape")
            await pilot.pause()

            assert target.label == "safe", "cancel must discard the edit"
            assert sched_mod.load(tmp_path) == [], "cancel must persist nothing"
    _run(body())


# --------------------------------------------------------------------------
# the choke point, as a unit: what a concurrent fire must survive
# --------------------------------------------------------------------------

def test_apply_only_writes_the_edited_fields():
    """The cron monitor can fire a job WHILE its editor is open. The fire
    stamps last_fired_slot and bumps run_count on the live object; applying
    the form must not clobber them, or the double-fire guard is re-armed."""
    target = job(schedule="@daily", label="x", prompt="p")
    target.run_count = 7
    target.last_fired_slot = "2026-08-21T09:30"
    jobs = [target]

    changed = _apply_job_edit(jobs, target, ("save", {
        "prompt": "new prompt", "schedule": "@hourly", "label": "y",
        "enabled": False, "new_conversation": True,
    }))

    assert changed
    assert target.prompt == "new prompt"
    assert target.run_count == 7, "a mid-edit fire's count was clobbered"
    assert target.last_fired_slot == "2026-08-21T09:30", (
        "the double-fire stamp was erased by the form's stale copy"
    )


def test_apply_with_no_result_changes_nothing(tmp_path):
    target = job()
    assert _apply_job_edit([target], target, None) is False
    assert not sched_mod.jobs_path(tmp_path).exists(), "a cancel wrote the store"
