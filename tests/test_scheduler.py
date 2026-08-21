"""The scheduler is pure, so all of it is testable without a terminal.

Every interesting scheduling bug lives at a boundary -- the same minute visited
three times by a 20s poll, a slot that passed while the app was closed, Sunday
spelled 7 instead of 0 -- and none of them are visible from a UI test. That is
the reason scheduler.py holds no Textual.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import scheduler as s


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def test_every_minute_matches_any_moment():
    cron = s.Cron.parse("* * * * *")
    assert cron.matches(datetime(2026, 8, 21, 3, 17))
    assert cron.matches(datetime(2026, 1, 1, 0, 0))


def test_fixed_time_matches_only_that_minute():
    cron = s.Cron.parse("30 9 * * *")
    assert cron.matches(datetime(2026, 8, 21, 9, 30))
    assert not cron.matches(datetime(2026, 8, 21, 9, 31))
    assert not cron.matches(datetime(2026, 8, 21, 10, 30))


def test_step_expands_to_every_nth():
    cron = s.Cron.parse("*/15 * * * *")
    assert cron.minute == frozenset({0, 15, 30, 45})


def test_step_on_a_range_is_bounded_by_the_range():
    cron = s.Cron.parse("0-30/10 * * * *")
    assert cron.minute == frozenset({0, 10, 20, 30})


def test_list_and_range_combine():
    cron = s.Cron.parse("0 9-11,17 * * *")
    assert cron.hour == frozenset({9, 10, 11, 17})


def test_seven_is_sunday_like_zero():
    """Cron accepts both spellings. Rejecting 7 as out-of-range is a classic."""
    assert s.Cron.parse("0 0 * * 7").dow == s.Cron.parse("0 0 * * 0").dow


def test_weekday_mapping_is_not_off_by_one():
    """2026-08-21 is a Friday. Python's Monday=0 is not cron's Sunday=0."""
    friday = datetime(2026, 8, 21, 12, 0)
    assert friday.weekday() == 4
    assert s.Cron.parse("0 12 * * 5").matches(friday)
    assert not s.Cron.parse("0 12 * * 4").matches(friday)


def test_aliases_expand():
    assert s.Cron.parse("@daily").minute == frozenset({0})
    assert s.Cron.parse("@hourly").hour == frozenset(range(24))


@pytest.mark.parametrize("expr", ["", "* * * *", "* * * * * *", "61 * * * *",
                                  "* 25 * * *", "* * * * 9", "a * * * *",
                                  "5-1 * * * *", "*/0 * * * *"])
def test_malformed_expressions_raise(expr):
    with pytest.raises(s.CronError):
        s.Cron.parse(expr)


def test_the_error_names_the_field_that_is_wrong():
    """'invalid cron expression' tells the person nothing about which of five."""
    with pytest.raises(s.CronError, match="hour"):
        s.Cron.parse("* 25 * * *")


def test_dom_and_dow_are_ORed_when_both_restricted():
    """Documented cron behaviour: `0 0 13 * 5` is the 13th OR any Friday."""
    cron = s.Cron.parse("0 0 13 * 5")
    assert cron.matches(datetime(2026, 8, 13, 0, 0))          # a Thursday, the 13th
    assert cron.matches(datetime(2026, 8, 21, 0, 0))          # a Friday, not the 13th
    assert not cron.matches(datetime(2026, 8, 20, 0, 0))      # Thursday the 20th


def test_dom_and_dow_are_ANDed_when_only_one_is_restricted():
    cron = s.Cron.parse("0 0 13 * *")
    assert cron.matches(datetime(2026, 8, 13, 0, 0))
    assert not cron.matches(datetime(2026, 8, 14, 0, 0))


# --------------------------------------------------------------------------
# next_after
# --------------------------------------------------------------------------

def test_next_after_is_strictly_after():
    cron = s.Cron.parse("0 * * * *")
    on_the_hour = datetime(2026, 8, 21, 9, 0)
    assert cron.next_after(on_the_hour) == datetime(2026, 8, 21, 10, 0)


def test_next_after_crosses_midnight():
    cron = s.Cron.parse("30 0 * * *")
    assert cron.next_after(datetime(2026, 8, 21, 23, 59)) == datetime(2026, 8, 22, 0, 30)


def test_next_after_returns_none_for_a_date_that_never_comes():
    """February 30th is a real answer of 'never', not a failure to compute."""
    assert s.Cron.parse("0 0 30 2 *").next_after(datetime(2026, 8, 21, 0, 0)) is None


# --------------------------------------------------------------------------
# due() -- the poll-loop contract
# --------------------------------------------------------------------------

def _job(**kw):
    kw.setdefault("prompt", "check the build")
    kw.setdefault("schedule", "* * * * *")
    return s.Job(**kw)


def test_a_due_job_is_returned():
    now = datetime(2026, 8, 21, 9, 30, 5)
    assert s.due([_job(schedule="30 9 * * *")], now)


def test_a_disabled_job_never_fires():
    now = datetime(2026, 8, 21, 9, 30, 5)
    assert s.due([_job(schedule="30 9 * * *", enabled=False)], now) == []


def test_a_job_does_not_fire_twice_in_the_same_minute():
    """THE poll-loop bug: a 20s tick visits one minute about three times."""
    now = datetime(2026, 8, 21, 9, 30, 5)
    job = _job(schedule="30 9 * * *")
    fired = s.due([job], now)
    assert fired == [job]

    job.last_fired_slot = s.slot_of(now)
    assert s.due([job], now) == []
    assert s.due([job], now + timedelta(seconds=20)) == []
    assert s.due([job], now + timedelta(seconds=40)) == []


def test_the_same_job_fires_again_the_next_time_round():
    job = _job(schedule="30 9 * * *")
    job.last_fired_slot = s.slot_of(datetime(2026, 8, 21, 9, 30))
    assert s.due([job], datetime(2026, 8, 22, 9, 30, 2)) == [job]


def test_a_slot_missed_while_the_app_was_closed_does_not_fire_late():
    """Waking to a burst of overdue prompts is worse than missing them: the
    burst arrives with nothing marking it as late and gets answered as now."""
    slot = datetime(2026, 8, 21, 9, 30)
    late = slot + s.LATE_TOLERANCE + timedelta(seconds=1)
    assert s.due([_job(schedule="30 9 * * *")], late) == []


def test_a_tick_that_drifts_a_little_still_counts_as_on_time():
    slot = datetime(2026, 8, 21, 9, 30)
    assert s.due([_job(schedule="30 9 * * *")], slot + timedelta(seconds=30))


def test_a_malformed_schedule_is_skipped_not_raised():
    """One bad job must not take down the poll loop and with it every good one."""
    good = _job(schedule="* * * * *")
    bad = _job(schedule="not a cron")
    assert s.due([bad, good], datetime(2026, 8, 21, 9, 30, 1)) == [good]


# --------------------------------------------------------------------------
# the job store
# --------------------------------------------------------------------------

def test_round_trip_preserves_every_field(tmp_path):
    jobs = [
        s.Job(prompt="summarise my inbox", schedule="@daily", label="morning"),
        s.Job(prompt="ping", schedule="*/5 * * * *", enabled=False,
              new_conversation=True, run_count=7),
    ]
    s.save(jobs, tmp_path)
    back = s.load(tmp_path)

    assert len(back) == 2
    for before, after in zip(jobs, back):
        assert before.id == after.id
        assert before.prompt == after.prompt
        assert before.schedule == after.schedule
        assert before.enabled == after.enabled
        assert before.new_conversation == after.new_conversation
        assert before.run_count == after.run_count
        assert before.label == after.label


def test_load_of_a_missing_file_is_empty_not_an_error(tmp_path):
    assert s.load(tmp_path) == []


def test_a_corrupt_job_file_does_not_stop_the_app(tmp_path):
    """The scheduler is a convenience; the chat is the product."""
    s.jobs_path(tmp_path).write_text("{ this is not json", encoding="utf-8")
    assert s.load(tmp_path) == []


def test_a_file_holding_the_wrong_shape_is_ignored(tmp_path):
    s.jobs_path(tmp_path).write_text('{"jobs": []}', encoding="utf-8")
    assert s.load(tmp_path) == []


def test_entries_missing_required_keys_are_dropped_not_fatal(tmp_path):
    s.jobs_path(tmp_path).write_text(
        json.dumps([{"prompt": "ok", "schedule": "@daily"}, {"prompt": "no schedule"}]),
        encoding="utf-8")
    assert [j.prompt for j in s.load(tmp_path)] == ["ok"]


def test_unknown_keys_from_a_future_version_are_dropped(tmp_path):
    """Forward compatibility: a newer LiteTUI's field must not crash an older one."""
    s.jobs_path(tmp_path).write_text(
        json.dumps([{"prompt": "p", "schedule": "@daily", "invented_later": 1}]),
        encoding="utf-8")
    assert len(s.load(tmp_path)) == 1


def test_a_bom_written_by_powershell_is_read(tmp_path):
    """Set-Content -Encoding UTF8 writes a BOM and plain utf-8 json rejects it.
    Already cost a live debugging session once in this repo."""
    s.jobs_path(tmp_path).write_text(
        '﻿' + json.dumps([{"prompt": "p", "schedule": "@daily"}]), encoding="utf-8")
    assert len(s.load(tmp_path)) == 1


def test_save_leaves_no_temp_files_behind(tmp_path):
    s.save([s.Job(prompt="p", schedule="@daily")], tmp_path)
    assert list(tmp_path.glob(".jobs-*")) == []


def test_save_is_atomic_so_a_reader_never_sees_half_a_file(tmp_path):
    """os.replace is the point: the real path is never open for writing, so a
    failure mid-write cannot truncate the jobs that were already there."""
    first = [s.Job(prompt="original", schedule="@daily")]
    s.save(first, tmp_path)
    before = s.jobs_path(tmp_path).read_text(encoding="utf-8")

    with pytest.raises(TypeError):
        s.save([s.Job(prompt=object(), schedule="@daily")], tmp_path)  # unserialisable

    assert s.jobs_path(tmp_path).read_text(encoding="utf-8") == before
    assert list(tmp_path.glob(".jobs-*")) == []


def test_ids_are_unique():
    assert len({s.Job(prompt="p", schedule="@daily").id for _ in range(200)}) == 200


def test_describe_is_one_line_and_shows_state():
    job = s.Job(prompt="x" * 200, schedule="@daily", enabled=False)
    line = job.describe()
    assert "\n" not in line
    assert "off" in line
    assert len(line) < 100
