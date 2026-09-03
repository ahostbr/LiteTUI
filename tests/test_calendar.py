"""The month view: the grid maths, and that the screen actually mounts.

The grid is pure and testable; the screen is not, so it gets driven for real
with `run_test`. compose() cannot run outside a live App, so a stubbed screen
would prove nothing about whether /calendar opens.
"""

from __future__ import annotations

import asyncio
import calendar as _calendar
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import calendar_view as cv
from litetui import scheduler as sched_mod
from litetui.plugins.scheduler_ui import CalendarScreen, DayScreen, JobScreen


def _cal(a):
    """The calendar's BODY — where the hit-map and the month state live.

    They moved out of `CalendarScreen` so a `SidePanel` can mount the same
    widget (T232). `a.screen.query_one` still reaches descendants; it is the
    DIRECT attribute access that had to follow the state.
    """
    from litetui.plugins.scheduler_ui import CalendarBody
    return a.screen.query_one(CalendarBody)



def job(prompt="p", schedule="@daily", **kw):
    return sched_mod.Job(prompt=prompt, schedule=schedule, **kw)


# --------------------------------------------------------------------------
# the grid
# --------------------------------------------------------------------------

def test_the_grid_is_always_six_rows_of_seven():
    """A grid that changes height between months makes every row jump."""
    for year, month in [(2026, 2), (2026, 8), (2024, 2), (2026, 5)]:
        weeks = cv.month_matrix(year, month)
        assert len(weeks) == cv.WEEK_ROWS
        assert all(len(w) == 7 for w in weeks)


def test_every_day_of_the_month_appears_exactly_once():
    days = _calendar.monthrange(2026, 8)[1]
    flat = [d for week in cv.month_matrix(2026, 8) for d in week if d]
    assert sorted(flat) == list(range(1, days + 1))


def test_a_month_needing_six_rows_is_not_truncated():
    """A 31-day month starting late in the week spans six rows. Clipping to
    five silently drops the last days, and only at the end of the month."""
    weeks = cv.month_matrix(2026, 8)
    flat = [d for week in weeks for d in week if d]
    assert 31 in flat


def test_the_first_column_is_monday():
    weeks = cv.month_matrix(2026, 8)
    for week in weeks:
        for col, day in enumerate(week):
            if day:
                assert date(2026, 8, day).weekday() == col
                break


def test_weekend_columns_are_saturday_and_sunday():
    assert not cv.is_weekend(0)
    assert not cv.is_weekend(4)
    assert cv.is_weekend(5)
    assert cv.is_weekend(6)


@pytest.mark.parametrize("start,delta,expect", [
    ((2026, 8), 1, (2026, 9)),
    ((2026, 12), 1, (2027, 1)),      # carries the year
    ((2026, 1), -1, (2025, 12)),     # and backwards
    ((2026, 8), 12, (2027, 8)),
    ((2026, 8), -20, (2024, 12)),
])
def test_step_month_carries_the_year(start, delta, expect):
    assert cv.step_month(*start, delta) == expect


# --------------------------------------------------------------------------
# what lands in a cell
# --------------------------------------------------------------------------

def test_a_daily_job_appears_on_every_day():
    j = job(schedule="@daily")
    for day in (1, 15, 31):
        assert cv.entries_for([j], date(2026, 8, day))


def test_a_weekday_job_does_not_appear_at_the_weekend():
    j = job(schedule="0 9 * * 1-5")
    assert cv.entries_for([j], date(2026, 8, 21))       # Friday
    assert not cv.entries_for([j], date(2026, 8, 22))   # Saturday


def test_a_job_for_another_month_is_absent():
    assert not cv.entries_for([job(schedule="0 0 1 12 *")], date(2026, 8, 1))


def test_a_broken_schedule_is_shown_not_hidden():
    """A job that can never fire is exactly the one worth seeing."""
    entries = cv.entries_for([job(schedule="nonsense")], date(2026, 8, 21))
    assert len(entries) == 1
    assert entries[0].broken
    assert entries[0].icon == cv.ICON_DEADLINE


def test_a_disabled_job_still_appears_but_reads_as_off():
    entries = cv.entries_for([job(schedule="@daily", enabled=False)], date(2026, 8, 21))
    assert entries[0].icon == cv.ICON_DONE
    assert entries[0].enabled is False


def test_a_high_frequency_job_reports_the_true_total_not_the_shown_slice():
    """`*/5` is 288 fires a day and a cell has room for two. Showing the slice
    as if it were the whole is the failure this guards."""
    entries = cv.entries_for([job(schedule="*/5 * * * *")], date(2026, 8, 21), cap=2)
    entry = entries[0]
    assert entry.total == 288
    # 288 fires, exactly ONE displayed, so 287 hidden. This assertion used to
    # say +286 -- it encoded the off-by-one rather than catching it, which is
    # why the bug survived until the render was looked at.
    assert "+287" in entry.summary()


def test_a_single_fire_says_no_more():
    entry = cv.entries_for([job(schedule="0 9 * * *")], date(2026, 8, 21))[0]
    assert entry.total == 1
    assert "+" not in entry.summary()
    assert entry.summary().startswith("09:00")


def test_times_are_sorted():
    entry = cv.entries_for([job(schedule="0 17,9 * * *")], date(2026, 8, 21), cap=6)[0]
    assert entry.times == ["09:00", "17:00"]


def test_summary_is_one_line():
    entry = cv.entries_for([job(prompt="x" * 200, schedule="@daily")], date(2026, 8, 21))[0]
    assert "\n" not in entry.summary()


# --------------------------------------------------------------------------
# cell overflow -- found by LOOKING at the render, not by these tests
# --------------------------------------------------------------------------
#
# Both of the below shipped green. The Friday 17:00 job was simply absent from
# the calendar and the suite was entirely happy. A guard written for one defect
# ("+286 more", at the JOB level) did not cover the same defect one layer up,
# at the CELL. Layout is what a source gate cannot see.

def test_a_cell_that_cannot_fit_everything_says_so():
    """Silently dropping an entry makes the calendar claim the day is free."""
    entries = [
        cv.DayEntry("1", "alpha", ["09:00"], 1, True),
        cv.DayEntry("2", "beta", ["10:00"], 1, True),
        cv.DayEntry("3", "gamma", ["11:00"], 1, True),
        cv.DayEntry("4", "delta", ["12:00"], 1, True),
    ]
    lines = cv.cell_lines(entries, limit=2)

    assert len(lines) == 2
    assert "alpha" in lines[0]
    assert "+3 more" in lines[1], f"overflow not declared: {lines!r}"


def test_a_cell_that_fits_shows_every_entry_and_no_marker():
    """The other arm: without it, 'always says +n' would pass too."""
    entries = [cv.DayEntry("1", "alpha", ["09:00"], 1, True),
               cv.DayEntry("2", "beta", ["10:00"], 1, True)]
    lines = cv.cell_lines(entries, limit=2)

    assert len(lines) == 2
    assert "more" not in lines[1]
    assert "beta" in lines[1]


def test_an_empty_day_produces_no_lines():
    assert cv.cell_lines([], limit=2) == []


def test_a_taller_cell_hides_less():
    entries = [cv.DayEntry(str(i), f"j{i}", ["09:00"], 1, True) for i in range(5)]
    assert "more" in cv.cell_lines(entries, limit=2)[-1]
    assert "more" not in cv.cell_lines(entries, limit=5)[-1]


def test_the_extra_count_matches_what_is_actually_shown():
    """A job firing 48 times a day read '+46' while 47 were unshown, because
    the count subtracted the times FETCHED rather than the one DISPLAYED.
    Wrong in the direction of claiming more was visible than was."""
    entry = cv.entries_for([job(schedule="*/30 * * * *")], date(2026, 8, 21), cap=2)[0]
    summary = entry.summary()

    assert entry.total == 48
    assert summary.count(":") == 1, f"one time is displayed, so: {summary!r}"
    assert "+47" in summary, f"48 fires, 1 shown, so 47 hidden: {summary!r}"


# --------------------------------------------------------------------------
# month totals -- the runaway-schedule signal
# --------------------------------------------------------------------------

def test_month_totals_counts_every_firing():
    assert cv.month_totals([job(schedule="0 9 * * *")], 2026, 8) == 31


def test_month_totals_makes_a_runaway_schedule_obvious():
    """`*/5` reads as harmless in a list and as 8,928 fires here."""
    assert cv.month_totals([job(schedule="*/5 * * * *")], 2026, 8) == 288 * 31


def test_month_totals_ignores_disabled_jobs():
    assert cv.month_totals([job(schedule="@daily", enabled=False)], 2026, 8) == 0


def test_month_totals_survives_a_broken_job():
    good = job(schedule="0 9 * * *")
    assert cv.month_totals([job(schedule="bad"), good], 2026, 8) == 31


# --------------------------------------------------------------------------
# the screen -- it must actually open
# --------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def test_slash_calendar_opens_the_screen():
    """A screen class nobody can reach is not a feature."""
    from litetui import app as m

    async def body():
        a = m.LiteTUI()
        a._connect = lambda: None
        a._fetch_ctx_window = lambda: None
        a.jobs[:] = [job(schedule="0 9 * * 1-5", label="standup")]
        async with a.run_test() as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            assert isinstance(a.screen, CalendarScreen)

            a.screen.dismiss(None)
            await pilot.pause()
            a._handle_command("/cal")           # the alias must work too
            await pilot.pause()
            assert isinstance(a.screen, CalendarScreen)
    _run(body())


def _paint_at(width, height, jobs):
    """Open the calendar at a given console size and return what it drew."""
    from litetui import app as m

    async def body():
        a = m.LiteTUI()
        a._connect = lambda: None
        a._fetch_ctx_window = lambda: None
        a.jobs[:] = jobs
        async with a.run_test(size=(width, height)) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            grid = a.screen.query_one("#cal-grid", m.Static)
            return str(grid.render())

    return _run(body())


def test_the_month_paints_at_a_real_terminal_width():
    """What the calendar is supposed to look like on a terminal someone owns."""
    painted = _paint_at(200, 50, [job(schedule="0 9 * * 1-5", label="standup")])
    today = date.today()

    assert cv.MONTHS[today.month - 1] in painted
    assert str(today.year) in painted
    assert "MONDAY" in painted, "day names should be full words when they fit"
    assert "SUNDAY" in painted
    assert "standup" in painted, "the job label should be readable, not clipped"
    assert "09:00" in painted


def test_a_cramped_terminal_degrades_instead_of_crashing():
    """Seven columns in 80 characters is 11 per day. Names go short and labels
    clip -- that is the correct answer at that width, and it must not throw."""
    painted = _paint_at(80, 24, [job(schedule="0 9 * * 1-5", label="standup")])

    assert "MON" in painted
    assert "MONDAY" not in painted, "the full name cannot fit in 11 columns"
    assert cv.MONTHS[date.today().month - 1] in painted


def test_a_very_narrow_terminal_still_paints():
    """The floor. A month grid at 30 columns is unusable, but unusable and
    rendered beats an exception in the middle of someone's session."""
    painted = _paint_at(30, 20, [job(schedule="@daily", label="x")])
    assert painted


def test_paging_months_changes_what_is_drawn_and_returns():
    from litetui import app as m

    async def body():
        a = m.LiteTUI()
        a._connect = lambda: None
        a._fetch_ctx_window = lambda: None
        a.jobs[:] = []
        async with a.run_test() as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            screen = _cal(a)
            start = (screen._year, screen._month)

            screen.action_next_month()
            assert (screen._year, screen._month) == cv.step_month(*start, 1)

            screen.action_prev_month()
            assert (screen._year, screen._month) == start

            screen.action_next_month()
            screen.action_today()
            today = date.today()
            assert (screen._year, screen._month) == (today.year, today.month)
    _run(body())


def test_the_calendar_opens_with_no_jobs_at_all():
    """The empty state is the FIRST state every user sees. A view that only
    renders once data exists is broken for everyone on day one."""
    from litetui import app as m

    async def body():
        a = m.LiteTUI()
        a._connect = lambda: None
        a._fetch_ctx_window = lambda: None
        a.jobs[:] = []
        async with a.run_test() as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            assert isinstance(a.screen, CalendarScreen)
            side = a.screen.query_one("#cal-side", m.Static)
            painted = str(side.render())
            assert "nothing scheduled" in painted
    _run(body())


def test_a_broken_job_does_not_stop_the_calendar_from_painting():
    from litetui import app as m

    async def body():
        a = m.LiteTUI()
        a._connect = lambda: None
        a._fetch_ctx_window = lambda: None
        a.jobs[:] = [job(schedule="not a cron", label="bad one")]
        async with a.run_test() as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            assert isinstance(a.screen, CalendarScreen)
            side = str(a.screen.query_one("#cal-side", m.Static).render())
            assert "unparseable" in side
    _run(body())
