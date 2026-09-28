"""T1082 (T1049 phase C) — a schedule's level is set when it is created.

Ryan (liteask a-a203e2c0, lock_cron), asked whether cron and /loop turns in an agent
seat stay hardcoded autonomous: "we need new settings to set this at the time u
create the schedule. in litetui and the sidecar. loops inherit the setting they were
created on ... loops should only be set manually during a live litetui instance never
scheduled directly. if a scheduled prompt has a /loop command in it so be it ... it
runs at the scheduled level."

Rulings: Sentinel 8cc9ea00 (no /loop dispatch; a loop records the effective level of
the turn it was created in), f4d49382 (C5 SKIP, not downgrade), e9576f7f (R1 skip
above a flag ceiling, R2 "scheduled" -> interactive, R3 a locked seat's delete of an
autonomous job is refused, R4 the sidecar creates through the parent), Dijkstra
f0ae21c1 (P1 the file ceiling, S1 skip before the lease).

conftest clears the owner mark, so an app or double built here is LOCKED unless an
arm marks it Ryan's own with _ryans().
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from litetui import app as m
from litetui import cron, goal_loop, gui_rpc, paths, rpc, scheduler, seat_authority, shared_state
from litetui.plugins.scheduler_ui import JobScreen, _apply_job_edit
from litetui.tool_policy import AUTONOMOUS, INTERACTIVE, STRICT


def _app(profile=AUTONOMOUS, **kw):
    a = m.LiteTUI(**kw)
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.settings.tool_policy_profile = profile
    a._active_tool_profile = profile
    a.convo_id = "c1"
    a._materialise_convo = lambda: None
    return a


def _ryans(a):
    """Ryan's own instance: owner-marked by his launcher, not spawned."""
    a._spawned_seat = False
    a._owner_seat = True
    a._pty_term = None
    return a


def _said(a):
    lines: list[str] = []
    a._system = lines.append
    return lines


def _seat(jobs, *, own=False, flag=None, running=True):
    """A firing seat as a double: `_fire_job` runs on it unmounted."""
    said, pending, commands = [], [], []
    seat = SimpleNamespace(
        jobs=jobs, convo_id="c1", settings=SimpleNamespace(tool_policy_profile=INTERACTIVE),
        _pending_input=pending, _chat_running=lambda: running,
        _user_bubble=lambda *a, **k: None, _system=said.append,
        _handle_command=commands.append, _cli_tool_profile=flag)
    if own:
        _ryans(seat)
    return seat, said, pending, commands


def _on_disk():
    return scheduler.load(paths.data_root())


# ── (a) the level is recorded when the schedule is created ──────────────────

def test_C1_cron_add_records_the_seats_level_when_none_is_given():
    a = _ryans(_app(INTERACTIVE))
    said = _said(a)
    a.cron.add("@daily summarise yesterday")
    [job] = a.jobs
    assert job.tool_profile == INTERACTIVE, "the dataclass default (autonomous) was recorded"
    assert [j.tool_profile for j in _on_disk()] == [INTERACTIVE]
    assert "runs interactive" in said[-1], f"the creation line does not name the level: {said[-1]!r}"


def test_C2_cron_add_level_records_the_creators_pick():
    a = _ryans(_app(AUTONOMOUS))
    _said(a)
    a.cron.add("--level strict 0 9 * * 1-5 what is on today")
    [job] = a.jobs
    assert (job.tool_profile, job.schedule, job.prompt) == (STRICT, "0 9 * * 1-5", "what is on today")


def test_C3_spawned_seat_records_autonomous_by_choice_and_default():
    a = _app(AUTONOMOUS)
    _said(a)
    a.cron.add("--level autonomous @daily summarise yesterday")
    a.cron.add("@daily summarise tomorrow")
    assert [j.tool_profile for j in a.jobs] == [AUTONOMOUS, AUTONOMOUS]


@pytest.mark.asyncio
@pytest.mark.parametrize("own, stored, want, offered_auto", [
    (False, AUTONOMOUS, AUTONOMOUS, True),
    (True, INTERACTIVE, INTERACTIVE, True),    # CONTROL: Ryan's own, his level, all offered
])
async def test_C5_the_job_form_starts_at_the_seats_level(own, stored, want, offered_auto):
    from textual.widgets import Select
    a = _app(stored)
    if own:
        _ryans(a)
    async with a.run_test(size=(120, 45)) as pilot:
        a.push_screen(JobScreen(None, "0 9 * * *"))
        await pilot.pause()
        select = a.screen.query_one("#job-tool-profile", Select)
        assert select.value == want
        assert (AUTONOMOUS in select._legal_values) is offered_auto


def _rpc(a, cmd):
    wire = []
    rpc.rpc_emit, saved = wire.append, rpc.rpc_emit
    try:
        rpc._handle_jobs(a, cmd["type"], cmd, "request")
    finally:
        rpc.rpc_emit = saved
    return wire[-1]


def test_C6_rpc_create_takes_a_level_only_through_the_one_helper():
    spawned = _app(AUTONOMOUS)
    own = _ryans(_app(INTERACTIVE))
    reply = _rpc(own, {"type": "jobs.create", "prompt": "p", "schedule": "@daily"})
    assert reply["ok"] and reply["result"]["tool_profile"] == INTERACTIVE, reply
    reply = _rpc(spawned, {"type": "jobs.create", "prompt": "p", "schedule": "@daily",
                           "tool_profile": AUTONOMOUS})
    assert reply["ok"] and reply["result"]["tool_profile"] == AUTONOMOUS
    for bad in ({"tool_profile": "scheduled"}, {"kind": "loop"}):
        reply = _rpc(spawned, {"type": "jobs.create", "prompt": "p", "schedule": "@daily", **bad})
        assert reply["ok"] is False
    assert "never scheduled directly" in reply["error"]
    assert [j.tool_profile for j in _on_disk()] == [AUTONOMOUS]


def test_C7_gui_create_records_the_seats_level_and_a_loop_takes_none():
    a = _ryans(_app(INTERACTIVE))
    gui_rpc._jobs(a, "create", {"job": {"prompt": "p", "schedule": "@daily"}})
    gui_rpc._jobs(a, "create", {"job": {"kind": "loop", "prompt": "l", "interval_minutes": 5}})
    assert [(j.kind, j.tool_profile) for j in a.jobs] == [("cron", INTERACTIVE), ("loop", INTERACTIVE)]
    with pytest.raises(ValueError, match="never scheduled directly"):
        gui_rpc._jobs(a, "create", {"job": {"kind": "loop", "prompt": "l", "interval_minutes": 5,
                                            "tool_profile": AUTONOMOUS}})
    assert len(_on_disk()) == 2


# ── Job writes follow normal ownership-independent APIs ─────────────────────

def _ryans_job_on_disk(**kw):
    job = scheduler.Job(prompt="nightly", schedule="@daily", tool_profile=AUTONOMOUS, **kw)
    scheduler.save([job], paths.data_root())
    return job


def test_C8_spawned_seat_can_edit_and_remove_autonomous_jobs():
    job = _ryans_job_on_disk()
    a = _app()
    _said(a)
    a.cron.command(f"off {job.id}")
    assert _on_disk()[0].enabled is False
    gui_rpc._jobs(a, "update", {"job_id": job.id, "patch": {"prompt": "revised"}})
    assert _on_disk()[0].prompt == "revised"
    assert _rpc(a, {"type": "jobs.delete", "job_id": job.id})["ok"] is True
    assert _on_disk() == []


def test_C8_spawned_seat_can_pause_and_remove_autonomous_loop():
    loop = scheduler.Job.loop(prompt="watch", interval_minutes=5, owner_convo_id="c1",
                              tool_profile=AUTONOMOUS)
    scheduler.save([loop], paths.data_root())
    a = _app()
    _said(a)
    goal_loop.loop_command(a, f"pause {loop.id}")
    assert _on_disk()[0].enabled is False
    goal_loop.loop_command(a, f"rm {loop.id}")
    assert _on_disk() == []


def test_C8_CONTROL_ryans_own_seat_and_a_non_autonomous_job_are_untouched_by_it():
    _ryans_job_on_disk()
    own = _ryans(_app())
    gui_rpc._jobs(own, "delete", {"job_id": own.jobs[0].id})
    assert _on_disk() == []
    scheduler.save([scheduler.Job(prompt="p", schedule="@daily", tool_profile=INTERACTIVE)],
                   paths.data_root())
    locked = _app()
    gui_rpc._jobs(locked, "delete", {"job_id": locked.jobs[0].id})
    assert _on_disk() == []


# ── (b) a loop inherits the level of the turn it was created in ─────────────

def test_L1_CONTROL_a_loop_typed_in_a_live_instance_records_that_instances_level():
    a = _ryans(_app(INTERACTIVE))
    _said(a)
    goal_loop.loop_command(a, "15m check the deploy")
    assert [j.tool_profile for j in a.jobs] == [INTERACTIVE]


def test_L2_spawned_seats_loop_keeps_autonomous():
    a = _app(AUTONOMOUS)
    _said(a)
    goal_loop.loop_command(a, "15m check the deploy")
    assert [j.tool_profile for j in a.jobs] == [AUTONOMOUS]


def test_L3_a_loop_created_DURING_a_scheduled_fire_records_the_jobs_level():
    """Sentinel 8cc9ea00 (ii): the effective level of the turn it was created in."""
    a = _ryans(_app(AUTONOMOUS))
    _said(a)
    a._chat_running = lambda: True
    a._active_tool_profile = STRICT      # the running scheduled turn's level
    goal_loop.loop_command(a, "15m check the deploy")
    assert [j.tool_profile for j in a.jobs] == [STRICT]


# ── (c)/(d) never scheduled directly; a scheduled "/loop" runs at the job's level ──

def test_D1_a_scheduled_prompt_starting_with_loop_is_never_dispatched():
    job = scheduler.Job(prompt="/loop 5m check the build", schedule="@daily", tool_profile=INTERACTIVE)
    seat, _said_, pending, commands = _seat([job], own=True)
    m.LiteTUI._fire_job(seat, job)
    assert commands == [] and seat.jobs == [job], "the /loop was dispatched"
    assert pending == [{"content": job.prompt, "text": job.prompt,
                        "tool_profile": INTERACTIVE, "source": "scheduled"}]


# ── fire time: the recorded level, or not at all ────────────────────────────

@pytest.mark.parametrize("level", [STRICT, INTERACTIVE, AUTONOMOUS])
def test_F1_F2_a_job_runs_at_its_recorded_level_in_ryans_seat(level):
    job = scheduler.Job(prompt="p", schedule="@daily", tool_profile=level)
    seat, _s, pending, _c = _seat([job], own=True)
    m.LiteTUI._fire_job(seat, job)
    assert pending[0]["tool_profile"] == level


def test_F3_the_removed_scheduled_level_runs_interactive_and_is_named_once():
    scheduler.save([scheduler.Job(prompt="p", schedule="@daily", tool_profile="scheduled")],
                   paths.data_root())
    [job] = _on_disk()
    seat, said, pending, _c = _seat([job], own=True)
    # _fire_job_owned, the delivery where the level is read: through _fire_job this
    # on-disk @daily row would first meet the due check (84ec114's arm did, and
    # delivered nothing).
    m.LiteTUI._fire_job_owned(seat, job)
    assert pending[0]["tool_profile"] == INTERACTIVE
    seat.cron = SimpleNamespace(jobs=[job])
    cron.say_retired_levels(seat)
    assert said == [f"/cron: {job.id} recorded the removed level 'scheduled'; it now runs interactive (T1082)."]


def test_S1_a_skipping_seat_never_takes_the_scheduler_lease(monkeypatch):
    """Dijkstra S1: a skipping seat holding the lease could cost Ryan's instance its
    last attempt in the slot."""
    taken = []
    real = shared_state.Lease

    class Recording(real):
        def __init__(self, path, *a, **k):
            taken.append(str(path))
            super().__init__(path, *a, **k)

    # ONLY the scheduler's lease: a fire's save takes its own jobs.json.lock (84ec114's
    # arm counted both and failed its CONTROL on 2 == 1).
    scheduler_leases = lambda: [p for p in taken if p.endswith(".scheduler.lease")]  # noqa: E731
    monkeypatch.setattr(shared_state, "Lease", Recording)
    job = scheduler.Job(prompt="p", schedule="@daily", tool_profile=AUTONOMOUS)
    seat, _s, pending, _c = _seat([job], flag=INTERACTIVE)
    m.LiteTUI._fire_job(seat, job)
    assert scheduler_leases() == [] and pending == []
    # CONTROL: a job this seat can grant takes the lease and fires.
    job.tool_profile = INTERACTIVE
    m.LiteTUI._fire_job(seat, job)
    assert len(scheduler_leases()) == 1 and pending[0]["tool_profile"] == INTERACTIVE


def test_F5_a_manual_run_of_a_job_this_seat_cannot_grant_is_refused():
    job = scheduler.Job(prompt="p", schedule="@daily", tool_profile=AUTONOMOUS)
    seat, said, pending, _c = _seat([job], flag=INTERACTIVE)
    assert m.LiteTUI._fire_job(seat, job, manual=True) is False
    assert pending == [] and job.run_count == 0
    assert "--tool-profile interactive" in said[-1]


def test_F6_R1_a_narrower_launch_flag_skips_the_job_too():
    job = scheduler.Job(prompt="p", schedule="@daily", tool_profile=AUTONOMOUS)
    seat, said, pending, _c = _seat([job], own=True, flag=INTERACTIVE)
    m.LiteTUI._fire_job(seat, job)
    assert pending == [] and job.run_count == 0
    assert "--tool-profile interactive" in said[-1], said


def test_F7_a_spawner_seats_interactive_job_runs_and_its_confirms_go_to_the_spawner():
    """The composition with T1049-B: the level decides WHICH actions confirm, the
    route decides WHO answers."""
    job = scheduler.Job(prompt="p", schedule="@daily", tool_profile=INTERACTIVE)
    seat, _s, pending, _c = _seat([job])
    seat._spawner_id = "0123456789abcdef"
    m.LiteTUI._fire_job(seat, job)
    assert pending[0]["tool_profile"] == INTERACTIVE
    assert seat_authority.confirm_route(seat) == "spawner"
    assert "launching agent 01234567" in seat_authority.schedule_note(seat, INTERACTIVE)
