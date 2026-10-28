"""Self scheduling: real relay/cron store, and cross-process delivery confinement."""
# ruff: noqa: F811 - imported pytest fixture is injected into same-named arguments
import asyncio
from types import SimpleNamespace

import pytest
from test_approval_relay_t1049b import (
    SPAWNER,
    _registered_parent,
    _ryans,
    _seat,
    disposable_presence,  # noqa: F401 - relay fixtures
    wire,  # noqa: F401 - relay fixture
)

from litetui import app as app_mod
from litetui import approval_relay, paths, scheduler, seat_authority, shared_state
from litetui.plugins import scheduler_plugin


def request(**kw):
    return {"prompt": "check my build", "schedule": "* * * * *", **kw}


def ready():
    app = _seat()
    app._materialise_convo()
    app._append({"role": "user", "content": "Schedule my own build check"})
    # Current registry presence is required by the real approval authority.
    import json

    from litetui import harness
    (harness.AGENTS_DIR / f"{SPAWNER}.json").write_text(
        json.dumps({"agent_id": SPAWNER}), encoding="utf-8")
    app._chat_running = lambda: False
    app.settings.relay_approval_timeout_s = 2
    return app


async def _answer(app, state, verb):
    import re
    for _ in range(100):
        if state["sent"]:
            ident = re.search(r"appr-[0-9a-f]{12}", state["sent"][0][1]).group(0)
            assert approval_relay.take_answer(app, {
                "from": SPAWNER, "to": app.seat.agent_id, "body": f"{verb} {ident}"})
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no approval request received by transport")


def assert_empty(app):
    assert app.cron.jobs == []
    assert scheduler.load(paths.data_root()) == []
    assert not scheduler.jobs_path(paths.data_root()).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["strict", "interactive", "autonomous"])
async def test_approve_records_self_and_level(wire, profile):
    app = ready()
    app._cli_tool_profile = profile
    app._active_tool_profile = profile
    state = wire(app)
    answering = asyncio.create_task(_answer(app, state, "APPROVE"))
    try:
        result = await app.cron.schedule_self(request())
        await answering
    finally:
        answering.cancel()
        await asyncio.gather(answering, return_exceptions=True)
    [job] = scheduler.load(paths.data_root())
    assert result["id"] == job.id
    assert (job.owner_agent_id, job.owner_convo_id, job.tool_profile) == (
        app.seat.agent_id, app.convo_id, profile)
    assert state["sent"][0][0] == SPAWNER
    assert "schedule_self" in state["sent"][0][1]
    assert job.new_conversation is False


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["deny", "timeout", "no-parent", "cancel"])
async def test_refusal_writes_nothing(wire, outcome):
    app = ready()
    state = wire(app)
    app.settings.relay_approval_timeout_s = 0.2
    if outcome == "no-parent":
        _registered_parent(app, None)  # stale launch parent is not authority
    answering = asyncio.create_task(_answer(app, state, "DENY")) if outcome == "deny" else None
    try:
        if outcome == "cancel":
            task = asyncio.create_task(app.cron.schedule_self(request()))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ValueError):
                await app.cron.schedule_self(request())
        if answering:
            await answering
    finally:
        if answering:
            answering.cancel()
            await asyncio.gather(answering, return_exceptions=True)
    assert_empty(app)
    expected = {"deny": "denied", "timeout": "timeout", "no-parent": "absent", "cancel": "cancelled"}[outcome]
    assert [entry["status"] for event, entry in state["log"] if event == "approval_relay"] == [expected]


@pytest.mark.asyncio
async def test_ryans_own_direct_and_taint_rechecked(wire, monkeypatch):
    app = _ryans(ready())
    _registered_parent(app, None)
    state = wire(app)
    result = await app.cron.schedule_self(request(level="strict"))
    assert state["sent"] == []
    assert scheduler.load(paths.data_root())[0].id == result["id"]
    app._pty_term = "owner-terminal"
    monkeypatch.setattr(seat_authority, "pty_taint_clean", lambda term: False)
    with pytest.raises(ValueError):
        await app.cron.schedule_self(request())
    assert len(scheduler.load(paths.data_root())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"level": "autonomous"}, {"target": "another-seat"},
    {"owner_agent_id": "another-seat"}, {"owner_convo_id": "another-convo"},
    {"new_conversation": True}, {"level": "unknown"}, {"prompt": ""},
    {"schedule": "not cron"}, {"label": 42},
])
async def test_invalid_requests_never_relay_or_write(wire, extra):
    app = ready()
    state = wire(app)
    with pytest.raises(ValueError):
        await app.cron.schedule_self(request(**extra))
    assert state["sent"] == []
    assert_empty(app)


@pytest.mark.asyncio
async def test_active_turn_caps_level(wire):
    app = _ryans(ready())
    wire(app)
    app._chat_running = lambda: True
    app._active_tool_profile = "strict"
    with pytest.raises(ValueError):
        await app.cron.schedule_self(request(level="interactive"))
    result = await app.cron.schedule_self(request())
    assert result["tool_profile"] == "strict"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["convo", "seat", "level", "ownership"])
async def test_changed_context_after_approval_refuses(wire, monkeypatch, change):
    app = ready()
    wire(app)

    async def approve(*args, **kwargs):
        if change == "convo":
            app.convo_id = "different"
        elif change == "seat":
            app.seat.agent_id = "different"
        elif change == "level":
            app._cli_tool_profile = "strict"
        else:
            app.store.release()
        return "approved"

    monkeypatch.setattr(approval_relay, "ask_spawner", approve)
    with pytest.raises(ValueError):
        await app.cron.schedule_self(request())
    assert_empty(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,spawned", [(True, True), (False, True), (False, False)])
async def test_unproven_human_launch_is_not_direct(wire, owner, spawned):
    app = ready()
    app._owner_seat = owner
    app._spawned_seat = spawned
    _registered_parent(app, None)
    wire(app)
    with pytest.raises(ValueError):
        await app.cron.schedule_self(request())
    assert_empty(app)


def test_direct_jobs_write_stays_refused():
    app = ready()
    why = seat_authority.jobs_file_refusal(app, {"path": str(paths.data_root() / "jobs.json")}, paths.data_root())
    assert why and "may not write" in why


def firing_seat(job, agent="seat-X", convo="convo-X", busy=False):
    return SimpleNamespace(
        seat=SimpleNamespace(agent_id=agent), convo_id=convo, jobs=[job],
        _chat_running=lambda: busy, _pending_input=[], _system=lambda text: None,
        _user_bubble=lambda *args, **kwargs: None, _cli_tool_profile=None,
    )


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("agent,convo", [("seat-Y", "convo-X"), ("seat-X", "convo-Y")])
def test_other_process_never_fires_or_takes_lease(monkeypatch, manual, agent, convo):
    job = scheduler.Job(prompt="mine", schedule="* * * * *",
                        owner_agent_id="seat-X", owner_convo_id="convo-X")
    scheduler.save([job], paths.data_root())
    app = firing_seat(job, agent, convo)
    def no_lease(*args, **kwargs):
        raise AssertionError("non-target took scheduler lease")
    monkeypatch.setattr(shared_state, "Lease", no_lease)
    app_mod.LiteTUI._fire_job(app, job, manual=manual)
    app_mod.LiteTUI._fire_job_owned(app, job, manual=manual)
    assert job.enabled and job.run_count == 0 and job.last_fired_slot is None
    assert scheduler.load(paths.data_root())[0].run_count == 0


def test_disk_target_is_rechecked(monkeypatch):
    job = scheduler.Job(prompt="mine", schedule="* * * * *",
                        owner_agent_id="seat-X", owner_convo_id="convo-X")
    scheduler.save([job], paths.data_root())
    stale = scheduler.Job(prompt="stale", schedule=job.schedule, id=job.id)
    app = firing_seat(stale, agent="seat-Y")
    app_mod.LiteTUI._fire_job(app, stale, manual=True)
    assert scheduler.load(paths.data_root())[0].run_count == 0


def test_own_job_fires_but_busy_self_job_does_not_queue(monkeypatch):
    job = scheduler.Job(prompt="mine", schedule="* * * * *",
                        owner_agent_id="seat-X", owner_convo_id="convo-X")
    scheduler.save([job], paths.data_root())
    app = firing_seat(job, busy=True)
    app_mod.LiteTUI._fire_job(app, job)
    assert app._pending_input == [] and job.run_count == 0
    app._chat_running = lambda: False
    delivered = []
    monkeypatch.setattr(app_mod.hook_host, "start_prompt", lambda a, item: delivered.append(item))
    app_mod.LiteTUI._fire_job(app, job)
    assert len(delivered) == 1 and delivered[0]["tool_profile"] == job.tool_profile
    assert scheduler.load(paths.data_root())[0].run_count == 1


@pytest.mark.parametrize("busy_minutes", [1, 61])
def test_busy_due_is_coalesced_and_fires_once_after_idle(monkeypatch, busy_minutes):
    from datetime import datetime, timedelta
    clock = [datetime(2026, 10, 28, 10, 0, 5)]  # noqa: DTZ001 - cron uses local wall time

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(app_mod, "datetime", Clock)
    job = scheduler.Job(prompt="mine", schedule="0 * * * *",
                        owner_agent_id="seat-X", owner_convo_id="convo-X")
    scheduler.save([job], paths.data_root())
    app = firing_seat(job, busy=True)
    delivered = []
    monkeypatch.setattr(app_mod.hook_host, "start_prompt", lambda a, item: delivered.append(item))
    start = clock[0]
    for minute in range(busy_minutes + 1):
        clock[0] = start + timedelta(minutes=minute)
        scheduler.refresh(app.jobs, paths.data_root())
        for candidate in scheduler.due(app.jobs, clock[0]):
            app_mod.LiteTUI._fire_job(app, candidate)
    assert delivered == [] and job.run_count == 0
    app._chat_running = lambda: False
    for tick in range(2):
        clock[0] += timedelta(seconds=20)
        scheduler.refresh(app.jobs, paths.data_root())
        for candidate in scheduler.due(app.jobs, clock[0]):
            app_mod.LiteTUI._fire_job(app, candidate)
    assert len(delivered) == 1 and job.run_count == 1
    assert app._pending_input == []


@pytest.mark.asyncio
async def test_conversation_switch_during_hooks_drops_prompt_but_keeps_job(wire, monkeypatch):
    from datetime import datetime, timedelta

    from litetui import hook_host
    app = _ryans(ready())
    state = wire(app)
    result = await app.cron.schedule_self(request())
    [job] = app.jobs
    original = app.convo_id
    app._user_bubble = lambda *a, **k: None
    prompts = []
    monkeypatch.setattr(hook_host, "start_prompt", lambda a, item: prompts.append(item))
    app_mod.LiteTUI._fire_job(app, job, manual=True)
    [item] = prompts
    assert item["owner_agent_id"] == app.seat.agent_id
    assert item["owner_convo_id"] == original
    assert result["id"] == job.id

    async def switch(*args, **kwargs):
        app.convo_id = "other-conversation"
        return SimpleNamespace(allowed=True)

    async def drain(*args):
        pass

    monkeypatch.setattr(hook_host, "dispatch", switch)
    monkeypatch.setattr(hook_host, "drain_lifecycle", drain)
    monkeypatch.setattr(hook_host, "refuse_below_floor", lambda *a: False)
    appended = []
    app._append = appended.append
    assert await hook_host.admit_prompt(app, item) is False
    assert appended == []
    assert len(state["said"]) == 1 and "self schedule" in state["said"][0].lower()
    [saved] = scheduler.load(paths.data_root())
    assert saved.enabled and saved.owner_convo_id == original and saved.owner_agent_id == app.seat.agent_id
    app.convo_id = original
    # The rejected occurrence stays stamped (at-most-once); the next scheduled
    # occurrence is still eligible when the correct conversation is active.
    assert scheduler.due([saved], datetime.now() + timedelta(minutes=1)) == [saved]  # noqa: DTZ005 - local cron


def test_tool_is_registered_with_schema():
    tools = {}
    ctx = SimpleNamespace(app=SimpleNamespace(), command=lambda *a, **k: None,
                          palette_row=lambda *a, **k: None,
                          tool=lambda spec, runner, **kw: tools.update({spec["function"]["name"]: (spec, runner, kw)}))
    scheduler_plugin._register(ctx)
    spec, runner, kw = tools["schedule_self"]
    assert spec["function"]["parameters"]["additionalProperties"] is False
    assert "target" not in spec["function"]["parameters"]["properties"]
    assert callable(runner) and kw["policy"]


@pytest.mark.asyncio
async def test_registered_tool_thread_reaches_real_approval_and_store(wire):
    import json
    app = ready()
    app._connect = lambda: None
    state = wire(app)
    tools = {}
    ctx = SimpleNamespace(app=app, command=lambda *a, **k: None, palette_row=lambda *a, **k: None,
                          tool=lambda spec, runner, **kw: tools.update({spec["function"]["name"]: runner}))
    scheduler_plugin._register(ctx)
    async with app.run_test(size=(100, 35)):
        answering = asyncio.create_task(_answer(app, state, "APPROVE"))
        try:
            result = json.loads(await asyncio.to_thread(tools["schedule_self"], request()))
            await answering
        finally:
            answering.cancel()
            await asyncio.gather(answering, return_exceptions=True)
    [job] = scheduler.load(paths.data_root())
    assert result["id"] == job.id and job.owner_agent_id == app.seat.agent_id


@pytest.mark.asyncio
async def test_save_failure_leaves_no_live_job(wire, monkeypatch):
    app = _ryans(ready())
    wire(app)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(scheduler, "save", fail)
    with pytest.raises(OSError, match="disk full"):
        await app.cron.schedule_self(request())
    assert_empty(app)
