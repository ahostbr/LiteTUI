"""A month calendar in the calcure style, showing when cron jobs fire.

calcure (github.com/anufrievroman/calcure) is curses-based, and Windows Python
ships no `curses` -- which is exactly why this is a rebuild of the LOOK in
Textual rather than a port. What is borrowed is the visual vocabulary, taken
from the source rather than from the screenshot:

    icons        • todo/event/today   ✔ done   ‣ important   ⚑ deadline   │ separator
    day colour   weekends and the title in red, today in green, day names in
                 blue, done/unimportant dimmed, ordinary days plain

Those are calcure's own defaults (`configuration.py` fallbacks, `colors.py`
Color enum). The hues here resolve through Textual's theme variables instead of
fixed ANSI indices, so the calendar follows whichever SHADES theme is active
rather than fighting it -- the semantic mapping is calcure's, the palette is
ours.

WHAT IT SHOWS
-------------
The cron jobs. A list answers "what is scheduled"; a month answers "what does
my week actually look like", which is a different question and the one that
catches a job you set to `*/5` and forgot.

The layout maths mirror calcure's: cell width is the pane divided by seven,
six week rows, day number at the top-left of its cell.
"""

from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass
from datetime import date, datetime

MONTHS = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
          "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]

ICON_EVENT = "•"
ICON_DONE = "✔"
ICON_IMPORTANT = "‣"
ICON_DEADLINE = "⚑"
ICON_TODAY = "•"
SEPARATOR = "│"

#: Weeks drawn per month. Six is calcure's grid and it is the true maximum:
#: a 31-day month starting on the last day of the week spans six rows.
WEEK_ROWS = 6


def month_matrix(year: int, month: int, start_monday: bool = True) -> list[list[int]]:
    """Six rows of seven day numbers; 0 marks a cell outside the month.

    Always six rows, even when five would do. A grid that changes height
    between months makes every row jump when you page through them.
    """
    cal = _calendar.Calendar(firstweekday=0 if start_monday else 6)
    weeks = cal.monthdayscalendar(year, month)
    while len(weeks) < WEEK_ROWS:
        weeks.append([0] * 7)
    return weeks[:WEEK_ROWS]


def step_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """Move by whole months, carrying the year. December + 1 is next January."""
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def is_weekend(column: int, start_monday: bool = True) -> bool:
    """Saturday and Sunday, by grid column rather than by date."""
    return column >= 5 if start_monday else column in (0, 6)


@dataclass(frozen=True)
class DayEntry:
    """One job's appearance on one day."""

    job_id: str
    label: str
    times: list[str]
    total: int
    enabled: bool
    broken: bool = False

    @property
    def icon(self) -> str:
        if self.broken:
            return ICON_DEADLINE
        if not self.enabled:
            return ICON_DONE          # dimmed + struck: present, not running
        return ICON_EVENT

    def summary(self) -> str:
        """`09:00 nightly` — and `+n` when the day holds more fires than shown.

        A job that fires 288 times must not render as though it fires twice.
        Showing a fraction as if it were the whole is the failure this exists
        to avoid.

        `extra` counts against what is actually DISPLAYED, which is one time.
        It used to subtract len(self.times) — the number FETCHED — so a job
        firing 48 times a day showed "+46" while 47 were unshown. A count that
        is wrong in the direction of "more is visible than really is" is the
        worst direction for it to be wrong in.
        """
        if not self.times:
            return self.label
        shown = 1
        extra = self.total - shown
        more = f" +{extra}" if extra > 0 else ""
        return f"{self.times[0]} {self.label}{more}".strip()


def entries_for(jobs, when: date, cap: int = 2) -> list[DayEntry]:
    """What appears in one day's cell.

    A job whose schedule does not parse is INCLUDED, flagged broken. Dropping
    it would make the calendar agree with a `/cron list` that also hides it,
    and a job that can never fire is exactly the one worth seeing.
    """
    probe = datetime(when.year, when.month, when.day)
    out: list[DayEntry] = []
    for job in jobs:
        try:
            cron = job.cron()
        except Exception:
            out.append(DayEntry(job.id, job.label or job.prompt[:18], [], 0,
                                job.enabled, broken=True))
            continue
        total = cron.count_on(probe)
        if not total:
            continue
        out.append(DayEntry(
            job_id=job.id,
            label=(job.label or job.prompt)[:18],
            times=cron.times_on(probe, cap=cap),
            total=total,
            enabled=job.enabled,
        ))
    return out


#: Event lines drawn under a day number. Two keeps a month on one screen.
CELL_LINES = 2


def cell_lines(entries: list, limit: int = CELL_LINES) -> list[str]:
    """The text lines for one day's cell, overflow declared rather than dropped.

    With more entries than fit, the LAST line becomes `+n more` instead of an
    entry. That costs one visible job and buys the guarantee that the calendar
    never lies about being empty -- and "nothing scheduled today" is precisely
    the wrong thing for it to say wrongly.
    """
    if not entries:
        return []
    if len(entries) <= limit:
        return [f"{e.icon} {e.summary()}" for e in entries]
    shown = entries[: limit - 1]
    hidden = len(entries) - len(shown)
    return [f"{e.icon} {e.summary()}" for e in shown] + [f"{ICON_IMPORTANT} +{hidden} more"]


def month_totals(jobs, year: int, month: int) -> int:
    """Every firing in the month, across every job.

    The number that makes a runaway schedule obvious: `*/5` reads as harmless
    in a list and as 8,928 fires here.
    """
    days = _calendar.monthrange(year, month)[1]
    total = 0
    for day in range(1, days + 1):
        probe = datetime(year, month, day)
        for job in jobs:
            if not job.enabled:
                continue
            try:
                total += job.cron().count_on(probe)
            except Exception:
                continue
    return total
