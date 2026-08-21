"""The schedule builder in the live editor: widgets in, cron string out.

The pure inverse is proven in test_schedule_builder.py. What ONLY a live app
can prove is the wiring: that opening a job populates the widgets, that a
tick rewrites the same input the save path reads, that untouched jobs keep
their exact string, and that the keyboard routes (arrows, wrap) really
arrive. A remedy that exists and is never invoked looks identical to one
that works.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as m
import paths
import schedule_builder as sb
import scheduler as sched_mod
from ticker import NumberTicker


@pytest.fixture(autouse=True)
def _never_write_the_live_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)


def job(prompt="p", schedule="@daily", **kw):
    return sched_mod.Job(prompt=prompt, schedule=schedule, **kw)


def make_app(jobs):
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._jobs = jobs
    return a


def _run(coro):
    return asyncio.run(coro)


SIZE = (190, 48)


async def _open_editor(a, pilot, day=15):
    a._handle_command("/calendar")
    await pilot.pause()
    a.screen._year, a.screen._month = 2026, 8
    a.screen._paint()
    a.screen.open_day(day)
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    return a.screen


# --------------------------------------------------------------------------
# opening populates the widgets
# --------------------------------------------------------------------------

def test_a_recognized_job_opens_with_the_widgets_populated():
    async def body():
        a = make_app([job(schedule="30 17 * * 1-5", label="evening")])
        async with a.run_test(size=SIZE) as pilot:
            ed = await _open_editor(a, pilot, day=17)   # a Monday
            assert isinstance(ed, m.JobScreen)

            assert ed.query_one("#job-preset", m.Select).value == "weekdays"
            assert ed.query_one("#job-hour", NumberTicker).value == 17
            assert ed.query_one("#job-minute", NumberTicker).value == 30
            assert ed.query_one("#job-schedule", m.Input).display is False, (
                "the raw cron field should hide when a preset owns the schedule"
            )
    _run(body())


def test_a_broken_schedule_opens_in_custom_with_the_raw_field_shown():
    async def body():
        a = make_app([job(schedule="not a cron", label="bad")])
        async with a.run_test(size=SIZE) as pilot:
            ed = await _open_editor(a, pilot)
            assert ed.query_one("#job-preset", m.Select).value == "custom"
            assert ed.query_one("#job-schedule", m.Input).display is True
            assert ed.query_one("#job-schedule", m.Input).value == "not a cron"
    _run(body())


def test_creating_from_a_day_opens_as_a_legible_yearly():
    """'0 9 15 8 *' is developer-speak; 'every year on August 15 at 09:00'
    is the same fact. The prefill must open as the second."""
    async def body():
        a = make_app([])
        async with a.run_test(size=SIZE) as pilot:
            a._handle_command("/calendar")
            await pilot.pause()
            a.screen._year, a.screen._month = 2026, 8
            a.screen._paint()
            a.screen.open_day(15)
            await pilot.pause()
            a.screen.action_new_job()
            await pilot.pause()

            ed = a.screen
            assert ed.query_one("#job-preset", m.Select).value == "yearly"
            assert ed.query_one("#job-month", m.Select).value == 8
            assert ed.query_one("#job-day", NumberTicker).value == 15
            assert ed.query_one("#job-hour", NumberTicker).value == 9
    _run(body())


# --------------------------------------------------------------------------
# widgets rewrite the string the save path reads
# --------------------------------------------------------------------------

def test_ticking_a_ticker_rewrites_the_schedule_input():
    async def body():
        a = make_app([job(schedule="0 9 * * 1-5", label="standup")])
        async with a.run_test(size=SIZE) as pilot:
            ed = await _open_editor(a, pilot, day=17)

            ed.query_one("#job-minute", NumberTicker)._step(+1)
            await pilot.pause()
            assert ed.query_one("#job-schedule", m.Input).value == "1 9 * * 1-5"

            ed.query_one("#job-hour", NumberTicker)._step(+1)
            await pilot.pause()
            assert ed.query_one("#job-schedule", m.Input).value == "1 10 * * 1-5"
    _run(body())


def test_arrow_keys_on_a_focused_ticker_step_and_regenerate():
    """The keyboard route, end to end: focus the hour field, press up."""
    async def body():
        a = make_app([job(schedule="0 9 * * *", label="x")])
        async with a.run_test(size=SIZE) as pilot:
            ed = await _open_editor(a, pilot)

            ed.query_one("#job-hour", NumberTicker).query_one(m.Input).focus()
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()

            assert ed.query_one("#job-hour", NumberTicker).value == 10
            assert ed.query_one("#job-schedule", m.Input).value == "0 10 * * *"
    _run(body())


def test_the_hour_wraps_at_midnight():
    async def body():
        a = make_app([job(schedule="0 23 * * *", label="late")])
        async with a.run_test(size=SIZE) as pilot:
            ed = await _open_editor(a, pilot)
            ed.query_one("#job-hour", NumberTicker)._step(+1)
            await pilot.pause()
            assert ed.query_one("#job-hour", NumberTicker).value == 0
            assert ed.query_one("#job-schedule", m.Input).value == "0 0 * * *"
    _run(body())


def test_switching_preset_rebuilds_and_retargets_the_form():
    async def body():
        a = make_app([job(schedule="0 9 * * *", label="x")])
        async with a.run_test(size=SIZE) as pilot:
            ed = await _open_editor(a, pilot)

            ed.query_one("#job-preset", m.Select).value = "every_minutes"
            await pilot.pause()

            assert ed.query_one("#job-schedule", m.Input).value == "*/5 * * * *", (
                "the default n=5 should build immediately on preset switch"
            )
            assert ed.query_one("#job-n", NumberTicker).display is True
            assert ed.query_one("#job-hour", NumberTicker).display is False
    _run(body())


def test_switching_to_custom_reveals_what_the_builder_built():
    """Custom starts from the built string, not from blank — the builder is
    a cron teacher, and this is the lesson handover."""
    async def body():
        a = make_app([job(schedule="0 9 * * *", label="x")])
        async with a.run_test(size=SIZE) as pilot:
            ed = await _open_editor(a, pilot)
            ed.query_one("#job-minute", NumberTicker)._step(+1)
            await pilot.pause()

            ed.query_one("#job-preset", m.Select).value = "custom"
            await pilot.pause()
            raw = ed.query_one("#job-schedule", m.Input)
            assert raw.display is True
            assert raw.value == "1 9 * * *"
    _run(body())


# --------------------------------------------------------------------------
# saving
# --------------------------------------------------------------------------

def test_saving_a_ticked_schedule_persists_the_built_string(tmp_path):
    async def body():
        target = job(schedule="0 9 * * 1-5", label="standup", prompt="hi")
        a = make_app([target])
        async with a.run_test(size=SIZE) as pilot:
            ed = await _open_editor(a, pilot, day=17)
            ed.query_one("#job-hour", NumberTicker)._step(+1)
            await pilot.pause()
            await pilot.click("#job-save")
            await pilot.pause()

            assert target.schedule == "0 10 * * 1-5"
            assert [j.schedule for j in sched_mod.load(tmp_path)] == ["0 10 * * 1-5"]
    _run(body())


def test_an_untouched_alias_survives_open_and_save(tmp_path):
    """Recognition normalises @daily to widgets, but regeneration fires only
    on widget EVENTS — so open + save must keep the exact stored string."""
    async def body():
        target = job(schedule="@daily", label="x", prompt="hi")
        a = make_app([target])
        async with a.run_test(size=SIZE) as pilot:
            ed = await _open_editor(a, pilot)
            assert ed.query_one("#job-preset", m.Select).value == "daily"

            await pilot.click("#job-save")
            await pilot.pause()
            assert target.schedule == "@daily", (
                "opening a job untouched must not rewrite its schedule string"
            )
    _run(body())


def test_custom_mode_still_refuses_garbage(tmp_path):
    async def body():
        target = job(schedule="0 9 * * *", label="x", prompt="hi")
        a = make_app([target])
        async with a.run_test(size=SIZE) as pilot:
            ed = await _open_editor(a, pilot)
            ed.query_one("#job-preset", m.Select).value = "custom"
            await pilot.pause()
            ed.query_one("#job-schedule", m.Input).value = "* 25 * * *"
            await pilot.pause()

            await pilot.click("#job-save")
            await pilot.pause()
            assert isinstance(a.screen, m.JobScreen), "garbage was saved"
            assert "hour" in str(ed.query_one("#job-status", m.Static).render())
            assert target.schedule == "0 9 * * *"
    _run(body())
