"""build <-> recognize is a proven inverse, not a hopeful one.

The builder rewrites a job's schedule string on save. If recognize mis-reads
a string, the widgets open showing a DIFFERENT schedule than the job has,
and the next save silently commits the misreading. The round-trip sweep is
what makes that impossible for every string the builder can produce; the
refusal tests are what keep it honest about everything else.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import schedule_builder as sb
import scheduler as sched_mod


# --------------------------------------------------------------------------
# the inverse property, swept
# --------------------------------------------------------------------------

def _param_samples():
    """Every preset with edge-of-range and mid-range params."""
    times = [(0, 0), (9, 30), (23, 59), (13, 5)]
    for h, m in times:
        at = {"hour": h, "minute": m}
        yield "daily", at
        yield "weekdays", at
        yield "weekends", at
        for wd in (0, 1, 4, 6):
            yield "weekly", {**at, "weekday": wd}
        for day in (1, 15, 31):
            yield "monthly", {**at, "day": day}
        yield "yearly", {**at, "day": 21, "month": 8}
        yield "yearly", {**at, "day": 1, "month": 1}
        yield "yearly", {**at, "day": 31, "month": 12}
    for n in (1, 2, 5, 30, 59):
        yield "every_minutes", {"n": n}
    for n in (1, 2, 6, 23):
        yield "every_hours", {"n": n}


@pytest.mark.parametrize("preset,params", list(_param_samples()))
def test_recognize_inverts_build(preset, params):
    built = sb.build(preset, params)
    back = sb.recognize(built)
    assert back is not None, f"builder emitted {built!r} and cannot read it back"
    got_preset, got_params = back
    assert got_preset == preset
    assert got_params == params


@pytest.mark.parametrize("preset,params", list(_param_samples()))
def test_everything_built_is_valid_cron(preset, params):
    """The builder must never emit a string the scheduler refuses."""
    sched_mod.Cron.parse(sb.build(preset, params))   # raises on failure


@pytest.mark.parametrize("preset,params", list(_param_samples()))
def test_build_recognize_build_is_stable(preset, params):
    """One full lap changes nothing — opening and saving untouched is a no-op
    at the string level for canonical strings."""
    once = sb.build(preset, params)
    assert sb.build(*sb.recognize(once)) == once


# --------------------------------------------------------------------------
# one-way trips the builder accepts but never emits
# --------------------------------------------------------------------------

def test_aliases_are_recognized():
    assert sb.recognize("@daily") == ("daily", {"hour": 0, "minute": 0})
    assert sb.recognize("@hourly") == ("every_hours", {"n": 1})
    assert sb.recognize("@weekly") == ("weekly", {"hour": 0, "minute": 0, "weekday": 0})


def test_weekday_seven_reads_as_sunday():
    assert sb.recognize("0 9 * * 7") == ("weekly", {"hour": 9, "minute": 0, "weekday": 0})


def test_zero_padded_fields_are_read():
    """'00 09 * * *' means 09:00 — a leading zero is not a different number."""
    assert sb.recognize("00 09 * * *") == ("daily", {"hour": 9, "minute": 0})


# --------------------------------------------------------------------------
# refusals: everything else is custom, shown raw
# --------------------------------------------------------------------------

@pytest.mark.parametrize("schedule", [
    "not a cron",
    "",
    "0 9 * *",                 # four fields
    "0 9 * * 1-5 extra",       # six
    "*/5 9 * * *",             # stepped minute with a fixed hour: not a preset
    "5 */2 * * *",             # every-2-hours but not at :00
    "0 9 * * 6,0",             # weekends spelled backwards — build emits 0,6
    "0 9 * * 1,3,5",           # a dow list is not a preset
    "0 9 13 * 5",              # dom+dow — cron's OR trap stays custom
    "0 9 1-5 * *",             # a dom range is not a preset
    "99 9 * * *",              # minute out of range
    "0 25 * * *",              # hour out of range
    "0 9 0 * *",               # day 0 does not exist
    "0 9 32 * *",              # nor day 32
    "0 9 1 13 *",              # nor month 13
    "*/0 * * * *",             # zero step
    "*/60 * * * *",            # step past the field
])
def test_unrecognizable_strings_route_to_custom(schedule):
    assert sb.recognize(schedule) is None, (
        f"{schedule!r} was recognized — the widgets would show a schedule "
        f"the job does not have, and the next save would commit the misreading"
    )


def test_build_refuses_custom():
    with pytest.raises(ValueError):
        sb.build("custom", {})


def test_the_day_prefill_recognizes_as_yearly():
    """The calendar's create-from-day prefill must open as a legible
    'every year on <month> <day>' — that is the whole QoL point."""
    assert sb.recognize("0 9 21 8 *") == (
        "yearly", {"hour": 9, "minute": 0, "day": 21, "month": 8}
    )
