"""T070 O4-b — ElapsedState's own behaviour, which NOTHING previously tested.

🔴 WHY THIS FILE EXISTS: a positive control found the gap. With
`ElapsedState.start` deliberately gutted — body never recorded, t0 never
stamped — the ENTIRE suite stayed green: 1,115 tests, run_all EXIT 0. The
in-flight clock's core state transitions were covered by nothing, so a green
gate on the O4-b move was evidence only that the move broke nothing the suite
COULD see. A green that cannot go red is not a pass, it is an absence.

These tests are written against the state machine, not against the
implementation: what `start`/`stop_body`/`cancel`/`ensure_running` must be TRUE
of, so they still hold if the internals are rewritten. `_spawn` is exercised
through its no-running-loop path (RuntimeError -> task stays None), which is
exactly how the app is constructed under the unit tests.
"""
import asyncio

import pytest

from litetui.turnstats import ElapsedState


class _FakeApp:
    """Enough app for ElapsedState: it only ever calls _elapsed_repaint()."""

    def __init__(self):
        self.repaints = 0

    async def _elapsed_repaint(self):
        self.repaints += 1


class _Body:
    def __init__(self):
        self.content = None


def test_start_records_the_body_and_stamps_t0():
    """The whole point of the clock: it knows WHICH bubble and SINCE WHEN.

    This is the assertion the gutted version failed and the suite did not."""
    e = ElapsedState(_FakeApp())
    body = _Body()
    e.start(body)
    assert e.body is body
    assert e.body_t0 > 0.0


def test_stop_body_clears_the_body_and_not_the_task():
    """Tool calls and the compaction card keep needing the loop after the
    bubble stops being pre-token, so stop_body must not retire it."""
    e = ElapsedState(_FakeApp())
    e.start(_Body())
    e.task = "sentinel-task"          # stand-in: no loop is running here
    e.stop_body()
    assert e.body is None
    assert e.task == "sentinel-task"


def test_cancel_clears_the_task():
    e = ElapsedState(_FakeApp())

    class _T:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    t = _T()
    e.task = t
    e.cancel()
    assert t.cancelled is True
    assert e.task is None


def test_start_cancels_a_lingering_task_first():
    """A turn must never be repainted by its predecessor's loop."""
    e = ElapsedState(_FakeApp())
    seen = []

    class _T:
        def done(self):
            return False

        def cancel(self):
            seen.append("cancelled")

    e.task = _T()
    e.start(_Body())
    assert seen == ["cancelled"]


def test_ensure_running_does_not_respawn_a_live_task():
    e = ElapsedState(_FakeApp())

    class _Live:
        def done(self):
            return False

    live = _Live()
    e.task = live
    e.ensure_running()
    assert e.task is live


def test_ensure_running_respawns_a_retired_task():
    """The loop self-retires after ~1s idle; a tool call or a compaction can
    begin with nothing else in flight, and without this the clock never ticks.
    Outside a running loop the respawn yields None -- which is the SAME value
    as 'never spawned', so this asserts the CALL happened, not the result."""
    app = _FakeApp()
    e = ElapsedState(app)
    spawned = []
    e._spawn = lambda: spawned.append(1)

    class _Done:
        def done(self):
            return True

    e.task = _Done()
    e.ensure_running()
    assert spawned == [1]

    e.task = None
    e.ensure_running()
    assert spawned == [1, 1]


def test_spawn_survives_having_no_running_loop():
    """RuntimeError from create_task means no event loop -- how the app is
    built under the unit tests. A missing clock must never take down a turn."""
    e = ElapsedState(_FakeApp())
    e.start(_Body())              # no loop running in this test
    assert e.task is None
    assert e.body is not None     # ...and the state was still recorded


@pytest.mark.asyncio
async def test_spawn_creates_a_real_task_when_a_loop_is_running():
    """The positive control for the test above: inside a loop, the same call
    must actually produce a task and actually reach the app's repaint."""
    app = _FakeApp()
    e = ElapsedState(app)
    e.start(_Body())
    assert e.task is not None
    await asyncio.sleep(0)
    assert app.repaints == 1
    e.cancel()
