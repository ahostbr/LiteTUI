"""The `/loop` panel. T074.

WHAT ALREADY EXISTED: `/loop` has listed, created, paused, resumed and removed
loops as text since goal_loop.py was written. This row adds a SURFACE over
those verbs and no behaviour of its own — so the tests that matter are the ones
proving the panel goes through the same verbs rather than reimplementing them.

🔴 THE DISCRIMINATOR IS `test_resuming_from_the_panel_REARMS_the_timer`.
Resuming is not `enabled = True`: it also re-arms `next_run_at`, or a loop
resumed after a long pause fires IMMEDIATELY instead of at its cadence. A panel
that flipped the flag itself would look right, persist correctly, and change
the behaviour — and every other test here would still pass.

📌 AND THE WIRING TEST DRIVES `loop_command`, NOT THE BODY. A test that builds
`LoopListBody()` itself proves the body works and says nothing about whether
anything opens it — the exact green-that-cannot-run this session already paid
for once (see project_test_that_constructs_its_own_input).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from litetui import goal_loop, paths, scheduler
from litetui.app import LiteTUI
from litetui.loop_list import LoopListBody, loop_rows

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-looplist-"))


def _app(monkeypatch, *loops):
    app = LiteTUI()
    app._connect = lambda: None
    # `jobs` is a PROPERTY onto the cron manager's live list — shared
    # mutable state by design, so mutate it in place rather than
    # rebinding, which the property has no setter for anyway.
    app.jobs.clear()
    app.jobs.extend(loops)
    app.said = []
    app._system = lambda m, *a, **k: app.said.append(str(m))
    # never write the real schedule file from a test
    monkeypatch.setattr(scheduler, "save", lambda jobs, root=None: None)
    monkeypatch.setattr(goal_loop.scheduler, "save", lambda jobs, root=None: None)
    return app


def _loop(prompt="check the deploy", minutes=15, enabled=True):
    return scheduler.Job.loop(prompt=prompt, interval_minutes=minutes,
                              owner_convo_id="c1", tool_profile="scheduled")


# ── the rows come from the live job list ───────────────────────────────────

def test_only_loops_appear_not_cron_jobs(monkeypatch):
    """`app.jobs` holds both kinds. The calendar shows cron; this shows loops.
    Neither should show the other's."""
    loop = _loop()
    cron = scheduler.Job(prompt="nightly", schedule="@daily")
    app = _app(monkeypatch, loop, cron)
    ids = [r["id"] for r in loop_rows(app)]
    assert loop.id in ids
    assert cron.id not in ids, "a cron job leaked into the loop panel"


def test_a_row_carries_what_the_text_form_shows(monkeypatch):
    job = _loop(prompt="watch CI", minutes=20)
    app = _app(monkeypatch, job)
    row = loop_rows(app)[0]
    assert row["every"] == "every 20m"
    assert row["prompt"] == "watch CI"
    assert row["enabled"] is True


def test_rows_are_read_live_not_snapshotted(monkeypatch):
    app = _app(monkeypatch)
    assert loop_rows(app) == []
    app.jobs.append(_loop())
    assert len(loop_rows(app)) == 1, "the panel would describe a world that moved"


# ── THE DISCRIMINATOR ──────────────────────────────────────────────────────

def test_resuming_from_the_panel_REARMS_the_timer(monkeypatch):
    """A hand-rolled `enabled = True` passes every other test in this file.

    It also makes a loop resumed after a long pause fire at once instead of
    waiting its interval — a behaviour change that looks like a working toggle.
    """
    job = _loop(minutes=30)
    job.enabled = False
    job.next_run_at = "2020-01-01T00:00:00"          # long overdue
    app = _app(monkeypatch, job)

    goal_loop.set_loop_enabled(app, job, True)

    assert job.enabled is True
    assert job.next_run_at != "2020-01-01T00:00:00", (
        "the timer was not re-armed — a resumed loop will fire immediately"
    )


def test_pausing_does_not_touch_the_timer(monkeypatch):
    """The other direction: pausing must not silently reschedule."""
    job = _loop()
    job.next_run_at = "2030-06-01T12:00:00"
    app = _app(monkeypatch, job)
    goal_loop.set_loop_enabled(app, job, False)
    assert job.enabled is False
    assert job.next_run_at == "2030-06-01T12:00:00"


def test_remove_takes_it_out_of_the_live_list(monkeypatch):
    job = _loop()
    app = _app(monkeypatch, job)
    goal_loop.remove_loop(app, job)
    assert job not in app.jobs
    assert loop_rows(app) == []


def test_the_extracted_verbs_still_persist(monkeypatch):
    """They were pulled out of loop_command; the save must have come with
    them, or a change survives the panel and dies at restart."""
    saved = []
    job = _loop()
    app = _app(monkeypatch, job)
    monkeypatch.setattr(goal_loop.scheduler, "save",
                        lambda jobs, root=None: saved.append(len(jobs)))
    goal_loop.set_loop_enabled(app, job, False)
    goal_loop.remove_loop(app, job)
    assert saved == [1, 0], f"verbs did not persist: {saved}"


# ── the text form is UNCHANGED ─────────────────────────────────────────────

def test_loop_list_still_prints_text(monkeypatch):
    """`/loop list` is the scriptable form and keeps working — only the BARE
    invocation became a panel."""
    app = _app(monkeypatch, _loop(prompt="still text", minutes=45))
    goal_loop.loop_command(app, "list")
    assert app.said and "still text" in app.said[-1]
    assert "every 45m" in app.said[-1]


def test_the_text_verbs_still_work(monkeypatch):
    job = _loop()
    app = _app(monkeypatch, job)
    goal_loop.loop_command(app, f"pause {job.id}")
    assert job.enabled is False
    goal_loop.loop_command(app, f"resume {job.id}")
    assert job.enabled is True
    goal_loop.loop_command(app, f"clear {job.id}")
    assert job not in app.jobs


# ── THE WIRING: driven through loop_command, never by building the body ────

@pytest.mark.asyncio
async def test_BARE_loop_opens_the_panel(monkeypatch):
    """Drives the real command. Constructing LoopListBody here would prove the
    body works and say nothing about whether anything opens it."""
    app = _app(monkeypatch, _loop())
    async with app.run_test(size=(120, 45)) as pilot:
        # Cleared HERE, not at construction: run_test emits the boot banner, so
        # an empty-`said` assertion would be measuring "the app never spoke"
        # rather than "the command printed nothing".
        app.said.clear()
        goal_loop.loop_command(app, "")
        for _ in range(20):
            await pilot.pause()
            if app.screen.query(LoopListBody):
                break
        assert app.screen.query(LoopListBody), "bare /loop did not open the panel"
        assert not app.said, f"bare /loop also printed text: {app.said}"


@pytest.mark.asyncio
async def test_the_panel_opens_on_an_empty_list_and_says_how_to_start_one(monkeypatch):
    """A panel that only reports nothing teaches nothing."""
    app = _app(monkeypatch)
    async with app.run_test(size=(120, 45)) as pilot:
        goal_loop.loop_command(app, "")
        for _ in range(20):
            await pilot.pause()
            if app.screen.query(LoopListBody):
                break
        body = app.screen.query_one(LoopListBody)
        text = " ".join(str(w.content) for w in body.query("Static"))
        assert "No loops" in text
        assert "/loop 15m" in text, "the empty state does not say how to leave it"


@pytest.mark.asyncio
async def test_the_panel_switch_goes_through_the_shared_verb(monkeypatch):
    """End to end: flip the switch, the job pauses AND persists."""
    saved = []
    job = _loop()
    app = _app(monkeypatch, job)
    monkeypatch.setattr(goal_loop.scheduler, "save",
                        lambda jobs, root=None: saved.append(len(jobs)))
    async with app.run_test(size=(120, 45)) as pilot:
        goal_loop.loop_command(app, "")
        for _ in range(20):
            await pilot.pause()
            if app.screen.query(LoopListBody):
                break
        app.screen.query_one(f"#ll-on-{job.id}").value = False
        await pilot.pause()

    assert job.enabled is False, "the switch did not reach the job"
    assert saved, "the change was never persisted"
