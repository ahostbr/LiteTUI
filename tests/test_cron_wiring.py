"""The scheduler must be WIRED, not merely present.

scheduler.py is unit-tested on its own. None of that proves the app calls it.
A remedy that exists and is never invoked looks exactly like one that works —
this repo has shipped that shape more than once — so these tests drive the REAL
dispatcher and a REAL running app, and assert the edges between the two.

The load-bearing one is `test_a_job_that_fires_mid_turn_queues_...`: a tick that
starts a turn from inside a turn would CANCEL the turn, because `_stream` and
`_compact` share `@work(exclusive=True, group="chat")`. That bug has been made
here before, and it is invisible to any test of scheduler.py alone.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as m
import scheduler as sched_mod


@pytest.fixture(autouse=True)
def _never_write_the_live_jobs_file(tmp_path, monkeypatch):
    """Redirect the job store away from the repo.

    `_fire_job` and `_cron_add` both call `sched_mod.save(self._jobs, ROOT)`,
    and ROOT is the live checkout. A TEST MUST NEVER WRITE A PATH THE RUNNING
    APP OWNS — three live stores in this repo were wrecked by their own suite
    before that rule was written down.
    """
    monkeypatch.setattr(m, "ROOT", tmp_path)


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._jobs = []
    return a


def _run(coro):
    """Drive an async body from a sync test so the file stays collectable."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# the dispatcher edge
# --------------------------------------------------------------------------

def test_slash_cron_reaches_the_handler():
    """The command must be routed. A method nobody calls is not a feature."""
    seen = []
    a = make_app()
    a._cron_command = lambda arg: seen.append(arg)
    a._handle_command("/cron")
    assert seen == [""]

    a._handle_command("/cron list")
    assert seen == ["", "list"]


def test_cron_appears_in_help():
    """An undiscoverable command is one nobody will ever type."""
    src = (Path(__file__).resolve().parent.parent / "src" / "app.py").read_text(
        encoding="utf-8", errors="ignore")
    assert '"/cron' in src, "/cron is not listed in the help text"


# --------------------------------------------------------------------------
# add / list / remove through the real command path
# --------------------------------------------------------------------------

def test_add_creates_and_persists_a_job(tmp_path):
    said = []
    a = make_app()
    a._system = lambda t: said.append(t)

    a._handle_command("/cron add @daily summarise what I did yesterday")

    assert len(a._jobs) == 1
    job = a._jobs[0]
    assert job.schedule == "@daily"
    assert job.prompt == "summarise what I did yesterday"
    # persisted, not just held in memory
    assert [j.prompt for j in sched_mod.load(tmp_path)] == [job.prompt]
    assert "next fire" in said[-1]


def test_add_with_a_five_field_schedule_splits_schedule_from_prompt():
    a = make_app()
    a._system = lambda t: None
    a._handle_command("/cron add 0 9 * * 1-5 what is on for today?")

    assert a._jobs[0].schedule == "0 9 * * 1-5"
    assert a._jobs[0].prompt == "what is on for today?"


def test_a_bad_schedule_is_refused_and_names_the_field():
    said = []
    a = make_app()
    a._system = lambda t: said.append(t)

    a._handle_command("/cron add * 25 * * * this should not be accepted")

    assert a._jobs == [], "a job with an unparseable schedule was stored anyway"
    assert "hour" in said[-1], f"the error did not name the bad field: {said[-1]!r}"


def test_a_schedule_with_no_prompt_is_refused():
    a = make_app()
    a._system = lambda t: None
    a._handle_command("/cron add @daily")
    assert a._jobs == []


def test_remove_takes_an_id_prefix():
    a = make_app()
    a._system = lambda t: None
    a._handle_command("/cron add @daily one")
    jid = a._jobs[0].id

    a._handle_command(f"/cron rm {jid[:4]}")
    assert a._jobs == []


def test_an_ambiguous_id_removes_nothing():
    """Resolving ambiguity to the first match is how the wrong job gets deleted."""
    said = []
    a = make_app()
    a._system = lambda t: said.append(t)
    a._jobs = [sched_mod.Job(prompt="a", schedule="@daily", id="ab11"),
               sched_mod.Job(prompt="b", schedule="@daily", id="ab22")]

    a._handle_command("/cron rm ab")

    assert len(a._jobs) == 2
    assert "matches 2" in said[-1]


def test_off_and_on_toggle_without_deleting():
    a = make_app()
    a._system = lambda t: None
    a._handle_command("/cron add @daily one")
    jid = a._jobs[0].id

    a._handle_command(f"/cron off {jid}")
    assert a._jobs[0].enabled is False
    a._handle_command(f"/cron on {jid}")
    assert a._jobs[0].enabled is True


def test_list_reports_a_broken_schedule_instead_of_hiding_it():
    """A job that can never fire must say so, or it sits dead for weeks."""
    said = []
    a = make_app()
    a._system = lambda t: said.append(t)
    a._jobs = [sched_mod.Job(prompt="p", schedule="not a cron")]

    a._handle_command("/cron list")
    assert "BROKEN" in said[-1]


# --------------------------------------------------------------------------
# delivery — the part that can cancel a turn
# --------------------------------------------------------------------------

def test_a_job_that_fires_mid_turn_queues_and_never_starts_a_turn():
    """THE bug this design exists to avoid.

    `_stream` is `@work(exclusive=True, group="chat")`. Starting it while a
    turn runs cancels the turn in flight. A scheduled prompt is the least
    urgent input there is — nobody is waiting on it — so it must always yield.
    """
    async def body():
        a = make_app()
        streamed = []
        async with a.run_test():
            a._stream = lambda *_, **__: streamed.append(1)
            a._chat_running = lambda: True          # a turn is in progress
            job = sched_mod.Job(prompt="scheduled work", schedule="* * * * *")
            a._jobs = [job]

            a._fire_job(job)

            assert streamed == [], "a cron job started a turn while one was running"
            assert len(a._pending_input) == 1
            assert a._pending_input[0]["content"] == "scheduled work"
    _run(body())


def test_a_job_that_fires_while_idle_starts_the_turn():
    """The other arm. Without it, 'never starts a turn' passes trivially."""
    async def body():
        a = make_app()
        streamed = []
        async with a.run_test():
            a._stream = lambda *_, **__: streamed.append(1)
            a._chat_running = lambda: False
            job = sched_mod.Job(prompt="scheduled work", schedule="* * * * *")
            a._jobs = [job]

            a._fire_job(job)

            assert streamed == [1], "an idle app did not run the scheduled prompt"
            assert a._pending_input == []
            assert a.conversation[-1]["content"] == "scheduled work"
    _run(body())


def test_firing_stamps_the_slot_before_delivery_so_it_cannot_double_fire():
    """Stamped BEFORE the turn, not after: a crash mid-turn would otherwise
    leave the job looking unfired and it would run again in the same minute."""
    async def body():
        a = make_app()
        async with a.run_test():
            a._stream = lambda *_, **__: None
            a._chat_running = lambda: False
            job = sched_mod.Job(prompt="p", schedule="* * * * *")
            a._jobs = [job]

            a._fire_job(job)

            assert job.last_fired_slot == sched_mod.slot_of(datetime.now())
            assert job.run_count == 1
            # and the tick that follows inside the same minute finds nothing
            assert sched_mod.due(a._jobs, datetime.now()) == []
    _run(body())


def test_the_banner_names_the_job_but_the_model_receives_the_bare_prompt():
    """The human needs to know it was scheduled; the model needs the prompt.
    Feeding the banner to the model puts our formatting inside its input."""
    async def body():
        a = make_app()
        async with a.run_test():
            a._stream = lambda *_, **__: None
            a._chat_running = lambda: False
            job = sched_mod.Job(prompt="do the thing", schedule="@daily", label="nightly")
            a._jobs = [job]

            a._fire_job(job)

            assert a.conversation[-1]["content"] == "do the thing"
    _run(body())


# --------------------------------------------------------------------------
# the loop itself
# --------------------------------------------------------------------------

def test_the_cron_worker_starts_and_is_not_in_the_chat_group():
    """Its own group, deliberately. In group 'chat' every tick would cancel
    the turn — which is precisely how the autocompact bug worked."""
    async def body():
        a = make_app()
        async with a.run_test():
            groups = {w.group for w in a.workers}
            assert "cron" in groups, f"the cron worker never started; groups={groups}"
    _run(body())


def test_a_scheduling_error_cannot_take_the_chat_down(monkeypatch):
    """One malformed job must not kill the poll loop and with it every good one."""
    def boom(*_a, **_k):
        raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(sched_mod, "due", boom)
    monkeypatch.setattr(sched_mod, "TICK_SECONDS", 0.01)

    async def body():
        a = make_app()
        async with a.run_test():
            await asyncio.sleep(0.08)   # several ticks, each one raising
            assert a.is_running, "the app died because a scheduler tick raised"
    _run(body())
