"""T0132 - a background task's row lives in ITS conversation's directory.

The store used to be one file, `<data root>/background-tasks.json`, shared by
every seat that ran from the same data root: six live seats, 1.6k rows, one
`.lock`. It now lives at `<data root>/.convos/<convo id>/background-tasks.json`.
Logs stay where they were (`<data root>/output/tasks/<id>.log`), so a row
copied out of the old file keeps tailing.

What this file pins, and why each arm exists:

    MIGRATION IS EXPLICIT CAPABILITY-GATED COPY-ONLY TOP-UP, NEVER ON BIND. The old file is READ, never
    written: seats still running the old code keep appending to it until they
    are relaunched, so a one-shot copy would strand whatever they add after it.
    Same id in both places -> the conversation's copy wins (new code is its
    writer once migrated). The marker is written LAST, so a crash re-runs the
    copy instead of skipping it.

    THE SAVE NEVER CREATES A CONVERSATION DIRECTORY. `row_store.write` mkdirs
    its parent, so a save into a conversation that is only STAGED would have
    materialised a half-born `.convos/<id>/` and littered /resume. The creator
    (`_start_background`) materialises first; the save raises `StoreNotBorn` if
    that ever regresses, instead of skipping and losing the row.

    LOST IS A CLAIM ABOUT A PROCESS. Binding a conversation mid-session reloads
    its store, so a row owned by THIS instance must not be read as a dead
    predecessor's (it used to be, because `load` only ever ran at boot).

Uuid wording: task ids are "t-" + uuid4 hex. Uniqueness is probabilistic, so
nothing here claims a collision is impossible - the same-id arm exists because
it can, in principle, happen.
"""
from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import os
import threading
import time
import warnings
from pathlib import Path

import pytest

from litetui import paths, router_record, row_store, tool_policy
from litetui import tasks as tasks_mod

# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def owned_session(tmp_path):
    from litetui.agent_launch_context import ordinary
    from litetui import settings
    cfg = settings.load()
    cfg.default_model = 'fixture-model'
    session = ordinary(tmp_path, cfg)
    try:
        yield session
    finally:
        session.release()  # App pilot exits only after deferred tasks were awaited


def _born(cid: str, owned_session) -> Path:
    d = owned_session.conversation_directory(cid)
    d.mkdir(parents=True)
    return d


def _row(cid: str, state: str = tasks_mod.DONE, **fields) -> dict:
    t = tasks_mod.new_task("bash", {"command": f"echo {cid}"}, cid)
    t.state = state
    for name, value in fields.items():
        setattr(t, name, value)
    return t.to_row()


def _legacy(rows: list[dict]) -> Path:
    p = paths.data_root() / tasks_mod.STORE
    p.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return p


def _fingerprint(p: Path) -> tuple:
    raw = p.read_bytes()
    return hashlib.sha256(raw).hexdigest(), len(raw), p.stat().st_mtime_ns


def _ids(convo_dir: Path) -> list[str]:
    return [r["id"] for r in row_store.rows_on_disk(convo_dir / tasks_mod.STORE)]


# ── migration: copy-only top-up ─────────────────────────────────────────


def test_topup_copies_only_this_conversations_rows_verbatim(owned_session):
    a, b = [_row("11111111-1111-4111-8111-111111111111"), _row("11111111-1111-4111-8111-111111111111", tasks_mod.LOST), _row("11111111-1111-4111-8111-111111111111", tasks_mod.KILLED)], [_row("22222222-2222-4222-8222-222222222222")]
    legacy = _legacy(a + b)
    c1, c2 = _born("11111111-1111-4111-8111-111111111111", owned_session), _born("22222222-2222-4222-8222-222222222222", owned_session)
    before = _fingerprint(legacy)

    copied = tasks_mod.topup(c1, paths.data_root(), agent_session=owned_session)

    assert copied == 3
    assert row_store.rows_on_disk(c1 / tasks_mod.STORE) == a, "rows must be copied VERBATIM"
    assert not (c2 / tasks_mod.STORE).exists(), "another conversation's directory was touched"
    assert _fingerprint(legacy) == before, "the old file must be byte- and mtime-identical"
    assert not (paths.data_root() / (tasks_mod.STORE + ".lock")).exists(), "the old file's lock was touched"


def test_topup_is_idempotent(owned_session):
    _legacy([_row("11111111-1111-4111-8111-111111111111"), _row("11111111-1111-4111-8111-111111111111")])
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    tasks_mod.topup(c1, paths.data_root(), agent_session=owned_session)
    store, marker = c1 / tasks_mod.STORE, c1 / tasks_mod.MIGRATED
    first = (store.read_bytes(), marker.read_bytes())

    assert tasks_mod.topup(c1, paths.data_root(), agent_session=owned_session) == 0
    assert (store.read_bytes(), marker.read_bytes()) == first, "a second run changed something"


def test_a_crash_before_the_marker_reruns_without_duplicating(monkeypatch, owned_session):
    rows = [_row("11111111-1111-4111-8111-111111111111"), _row("11111111-1111-4111-8111-111111111111")]
    _legacy(rows)
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)

    def boom(*_a, **_k):
        raise RuntimeError("crash between the copy and the marker")

    real = tasks_mod._write_marker
    monkeypatch.setattr(tasks_mod, "_write_marker", boom)
    with pytest.raises(RuntimeError):
        tasks_mod.topup(c1, paths.data_root(), agent_session=owned_session)
    assert sorted(_ids(c1)) == sorted(r["id"] for r in rows), "the copy did not land before the crash"
    assert not (c1 / tasks_mod.MIGRATED).exists(), "the marker must be written LAST"

    monkeypatch.setattr(tasks_mod, "_write_marker", real)
    tasks_mod.topup(c1, paths.data_root(), agent_session=owned_session)

    assert sorted(_ids(c1)) == sorted(r["id"] for r in rows), "rerun duplicated or lost rows"
    assert (c1 / tasks_mod.MIGRATED).exists()


def test_rows_an_old_writer_appends_later_are_picked_up(owned_session):
    """Explicit repeated topup copies late legacy rows; normal bind never calls it."""
    first = [_row("11111111-1111-4111-8111-111111111111")]
    legacy = _legacy(first)
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    tasks_mod.topup(c1, paths.data_root(), agent_session=owned_session)

    # A seat still on the old code resumes c1 and appends, via the real writer.
    late = [_row("11111111-1111-4111-8111-111111111111"), _row("11111111-1111-4111-8111-111111111111")]
    row_store.forget(legacy)
    row_store.write(legacy, first + late, prefix=".tasks-")
    before = _fingerprint(legacy)

    assert tasks_mod.topup(c1, paths.data_root(), agent_session=owned_session) == 2
    assert sorted(_ids(c1)) == sorted(r["id"] for r in first + late)
    assert _fingerprint(legacy) == before, "the top-up wrote the old file"


def test_the_same_id_in_both_places_keeps_the_conversations_copy(owned_session):
    mine = _row("11111111-1111-4111-8111-111111111111", tasks_mod.DONE)
    theirs = dict(mine, state=tasks_mod.RUNNING, label="the old file's stale copy")
    _legacy([theirs])
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    (c1 / tasks_mod.STORE).write_text(json.dumps([mine]), encoding="utf-8")

    assert tasks_mod.topup(c1, paths.data_root(), agent_session=owned_session) == 0
    assert row_store.rows_on_disk(c1 / tasks_mod.STORE) == [mine]


def test_an_unborn_conversation_is_left_alone_and_nothing_is_created(owned_session):
    legacy = _legacy([_row("44444444-4444-4444-8444-444444444444")])
    before = _fingerprint(legacy)

    assert tasks_mod.topup(owned_session.conversation_directory("44444444-4444-4444-8444-444444444444"), paths.data_root(), agent_session=owned_session) == 0

    assert not owned_session.conversation_directory("44444444-4444-4444-8444-444444444444").exists(), "a top-up must never create a conversation"
    assert _fingerprint(legacy) == before


def test_an_absent_or_corrupt_old_file_is_not_an_error(owned_session):
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    assert tasks_mod.topup(c1, paths.data_root(), agent_session=owned_session) == 0
    (paths.data_root() / tasks_mod.STORE).write_text("{not json", encoding="utf-8")
    assert tasks_mod.topup(c1, paths.data_root(), agent_session=owned_session) == 0


def test_concurrent_writers_to_one_store_lose_nothing(owned_session):
    """The file lock + id-keyed delta is what serialises writers, not the lease:
    a process leaves the lease when it rebinds while its tasks keep running."""
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    store = c1 / tasks_mod.STORE
    per_thread = [[_row("11111111-1111-4111-8111-111111111111") for _ in range(5)] for _ in range(6)]

    def add(rows):
        for r in rows:
            row_store.add_missing(store, [r], prefix=".tasks-")

    threads = [threading.Thread(target=add, args=(rows,)) for rows in per_thread]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    want = sorted(r["id"] for rows in per_thread for r in rows)
    assert sorted(_ids(c1)) == want, "a concurrent writer's rows were lost or duplicated"


def test_add_missing_never_overwrites_a_row_that_is_already_there(owned_session):
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    store = c1 / tasks_mod.STORE
    kept = _row("11111111-1111-4111-8111-111111111111", tasks_mod.DONE)
    store.write_text(json.dumps([kept]), encoding="utf-8")

    added = row_store.add_missing(store, [dict(kept, state=tasks_mod.RUNNING), _row("11111111-1111-4111-8111-111111111111")], prefix=".tasks-")

    assert added == 1
    assert row_store.rows_on_disk(store)[0] == kept


# ── the save: partitioned, and it never creates a conversation ─────────


def test_save_by_convo_writes_each_originating_store(owned_session):
    c1, c2 = _born("11111111-1111-4111-8111-111111111111", owned_session), _born("22222222-2222-4222-8222-222222222222", owned_session)
    t1 = tasks_mod.new_task("bash", {"command": "a"}, "11111111-1111-4111-8111-111111111111")
    t2 = tasks_mod.new_task("bash", {"command": "b"}, "22222222-2222-4222-8222-222222222222")

    tasks_mod.save_by_convo([t1, t2], agent_session=owned_session)

    assert _ids(c1) == [t1.id] and _ids(c2) == [t2.id]
    assert not (paths.data_root() / tasks_mod.STORE).exists(), "the shared file was written"


def test_saving_for_an_unborn_conversation_raises_and_creates_nothing(owned_session):
    for bad in ("never-born", "", "..", "a/b"):
        t = tasks_mod.new_task("bash", {"command": "a"}, bad)
        before = sorted(p.name for p in paths.data_root().rglob("*"))
        with pytest.raises(tasks_mod.StoreNotBorn):
            tasks_mod.save_by_convo([t], agent_session=owned_session)
        assert sorted(p.name for p in paths.data_root().rglob("*")) == before, f"{bad!r} left a trace"


def test_one_unborn_conversation_does_not_stop_the_others_being_saved(owned_session):
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    ok = tasks_mod.new_task("bash", {"command": "a"}, "11111111-1111-4111-8111-111111111111")
    orphan = tasks_mod.new_task("bash", {"command": "b"}, "99999999-9999-4999-8999-999999999999")

    with pytest.raises(tasks_mod.StoreNotBorn):
        tasks_mod.save_by_convo([orphan, ok], agent_session=owned_session)

    assert _ids(c1) == [ok.id], "a refused conversation took the healthy one down with it"


# ── bind: load one conversation's rows, keep what this instance runs ───


def test_bind_loads_only_that_conversations_rows(monkeypatch, owned_session):
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: False)
    mine, other = _row("11111111-1111-4111-8111-111111111111"), _row("22222222-2222-4222-8222-222222222222")
    legacy_only = _row("11111111-1111-4111-8111-111111111111")
    legacy = _legacy([legacy_only, other])
    before = _fingerprint(legacy)
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    (c1 / tasks_mod.STORE).write_text(json.dumps([mine]), encoding='utf-8')
    _born("22222222-2222-4222-8222-222222222222", owned_session)
    held: dict = {}

    tasks_mod.bind(held, c1, paths.data_root(), agent_session=owned_session)

    assert list(held) == [mine["id"]]
    assert legacy_only['id'] not in held
    assert _fingerprint(legacy) == before
    assert not (c1 / tasks_mod.MIGRATED).exists()


def test_bind_keeps_rows_of_other_conversations_this_instance_runs(owned_session):
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    running_elsewhere = tasks_mod.new_task("bash", {"command": "sleep 9"}, "22222222-2222-4222-8222-222222222222")
    held = {running_elsewhere.id: running_elsewhere}

    tasks_mod.bind(held, c1, paths.data_root(), agent_session=owned_session)

    assert held[running_elsewhere.id] is running_elsewhere


def test_a_rebind_keeps_this_instances_own_running_task_object(owned_session):
    """Resume away and back: the in-memory Task carries the live child handle
    (`proc`), which no disk copy can. It must not be replaced by its own row."""
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    t = tasks_mod.new_task("bash", {"command": "sleep 9"}, "11111111-1111-4111-8111-111111111111")
    t.proc = object()
    tasks_mod.save([t], c1, agent_session=owned_session)
    held = {t.id: t}

    tasks_mod.bind(held, c1, paths.data_root(), agent_session=owned_session)

    assert held[t.id] is t and t.state == tasks_mod.RUNNING and t.proc is not None


def test_a_rebind_does_not_mark_this_instances_own_task_lost(monkeypatch, owned_session):
    """`load` used to run once at boot, when a row carrying OUR pid could only be
    a dead predecessor's. Bind loads mid-session, so the rule is the INSTANCE."""
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: True)
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    t = tasks_mod.new_task("bash", {"command": "sleep 9"}, "11111111-1111-4111-8111-111111111111")
    tasks_mod.save([t], c1, agent_session=owned_session)

    assert tasks_mod.load(c1)[t.id].state == tasks_mod.RUNNING


def test_our_pid_on_a_row_from_another_instance_is_still_a_reused_pid(monkeypatch, owned_session):
    """The Orchestrator 94cedaec rule survives: same pid, different instance = a dead
    predecessor that happened to hold this number."""
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: True)
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    t = tasks_mod.new_task("bash", {"command": "sleep 9"}, "11111111-1111-4111-8111-111111111111")
    t.owner_instance = "a-predecessor-that-held-this-pid"
    assert t.owner_pid == os.getpid()
    tasks_mod.save([t], c1, agent_session=owned_session)

    assert tasks_mod.load(c1)[t.id].state == tasks_mod.LOST


def test_a_dead_owners_running_row_is_lost_after_bind(monkeypatch, owned_session):
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: False)
    r = _row("11111111-1111-4111-8111-111111111111", tasks_mod.RUNNING, owner_pid=4_000_000, owner_instance="dead", owner_created=None)
    legacy_only = _row("11111111-1111-4111-8111-111111111111")
    legacy = _legacy([legacy_only])
    before = _fingerprint(legacy)
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    (c1 / tasks_mod.STORE).write_text(json.dumps([r]), encoding='utf-8')
    held: dict = {}

    tasks_mod.bind(held, c1, paths.data_root(), agent_session=owned_session)

    assert held[r["id"]].state == tasks_mod.LOST
    assert legacy_only['id'] not in held
    assert _fingerprint(legacy) == before
    assert not (c1 / tasks_mod.MIGRATED).exists()
    # ...and the stamping reached the conversation's store, not just memory.
    tasks_mod.save_by_convo(held.values(), agent_session=owned_session)
    assert row_store.rows_on_disk(c1 / tasks_mod.STORE)[0]["state"] == tasks_mod.LOST


def test_bind_never_writes_the_old_file(monkeypatch, owned_session):
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: False)
    legacy = _legacy([_row("11111111-1111-4111-8111-111111111111", tasks_mod.RUNNING, owner_pid=None), _row("11111111-1111-4111-8111-111111111111")])
    c1 = _born("11111111-1111-4111-8111-111111111111", owned_session)
    before = _fingerprint(legacy)

    held: dict = {}
    tasks_mod.bind(held, c1, paths.data_root(), agent_session=owned_session)
    tasks_mod.save_by_convo(held.values(), agent_session=owned_session)

    assert _fingerprint(legacy) == before


# ── the app: the creator materialises, the finish writes the origin ────


def _app(monkeypatch, owned_session):
    from litetui import app as app_mod

    monkeypatch.setattr(app_mod.LiteTUI, "connect", lambda self: None)
    a = app_mod.LiteTUI(agent_session=owned_session)
    a._fetch_ctx_window = lambda: None
    a._active_tool_profile = tool_policy.AUTONOMOUS
    a._deliver_inbox = lambda message: None
    return a


_QUICK = {"command": "Write-Output hi"}


async def _wait(pilot, task, seconds=20):
    for _ in range(int(seconds / 0.05)):
        if task.ended is not None:
            return
        await pilot.pause(0.05)
    raise AssertionError(f"{task.id} never finished")


@pytest.mark.asyncio
@pytest.mark.parametrize("promoted", [False, True])
async def test_a_task_started_on_a_staged_conversation_is_kept(monkeypatch, promoted, owned_session):
    """E1: `gui.tools.execute` reaches `_execute_tool` with no turn, so the
    conversation is still STAGED. Explicit `background` and auto-promotion both
    end in `_start_background`, the single creator."""
    a = _app(monkeypatch, owned_session)
    a.settings.tool_auto_background_s = 1 if promoted else 0
    async with a.run_test(size=(100, 30)) as pilot:
        assert a.store.pending and not (a.convo_dir / "convo.jsonl").exists() and not (a.convo_dir / tasks_mod.STORE).exists(), "the fixture is not staged"
        args = {"command": "Start-Sleep -Seconds 2"} if promoted else dict(_QUICK, background=True)
        _, ok = await a._execute_tool("powershell", args)
        assert ok
        (task,) = a.bg_tasks.values()
        await _wait(pilot, task)

        assert not a.store.pending and a.convo_dir.is_dir(), "the creator did not materialise"
        rows = row_store.rows_on_disk(a.convo_dir / tasks_mod.STORE)
        assert [r["id"] for r in rows] == [task.id] and rows[0]["state"] == tasks_mod.DONE
        assert not (paths.data_root() / tasks_mod.STORE).exists(), "the shared file was written"
        assert len(list((owned_session.memory_root / "conversations").iterdir())) == 1, "more than one conversation was minted"


@pytest.mark.asyncio
async def test_the_creator_itself_materialises_so_no_route_can_skip_it(monkeypatch, owned_session):
    """E2 (`/skill` streams without materialising) and any route added later:
    the guarantee lives at `_start_background`, not at its callers."""
    a = _app(monkeypatch, owned_session)
    async with a.run_test(size=(100, 30)) as pilot:
        async def work():
            return "done"

        a._start_background("powershell", _QUICK, work())
        (task,) = a.bg_tasks.values()
        await _wait(pilot, task)

        assert a.convo_dir.is_dir() and _ids(a.convo_dir) == [task.id]


@pytest.mark.asyncio
async def test_a_task_that_finishes_after_a_resume_writes_its_originating_store(monkeypatch, owned_session):
    """R2: the finish lands on the loop long after the user may have moved on.
    Its row belongs to the conversation that STARTED it."""
    a = _app(monkeypatch, owned_session)
    started, release = asyncio.Event(), asyncio.Event()
    async with a.run_test(size=(100, 30)) as pilot:
        async def work():
            started.set()
            await release.wait()
            return "the output"

        a._start_background("powershell", _QUICK, work())
        (task,) = a.bg_tasks.values()
        origin = a.convo_dir
        await asyncio.wait_for(started.wait(), 5)

        # Move to another (already born) conversation.
        other = _born("55555555-5555-4555-8555-555555555555", owned_session)
        (other / "convo.jsonl").write_text(
            json.dumps({"type": "meta", "id": "55555555-5555-4555-8555-555555555555"}) + "\n"
            + json.dumps({"type": "msg", "message": {"role": "user", "content": "hi"}}) + "\n",
            encoding="utf-8")
        assert a._resume(other / "convo.jsonl")
        assert a.convo_id == "55555555-5555-4555-8555-555555555555"

        release.set()
        await _wait(pilot, task)

        assert task.convo_id == origin.name
        finished = row_store.rows_on_disk(origin / tasks_mod.STORE)
        assert [(r["id"], r["state"]) for r in finished] == [(task.id, tasks_mod.DONE)]
        assert not (other / tasks_mod.STORE).exists(), "the CURRENT conversation's store got the row"
        assert tasks_mod.log_path(task, paths.data_root()).is_file(), "the log moved"
        assert task.log.startswith(paths.data_root().as_posix())


@pytest.mark.asyncio
async def test_resume_loads_the_conversations_rows_and_tops_up_from_the_old_file(monkeypatch, owned_session):
    """Retained node ID: owned rows load WITHOUT implicit legacy copying."""
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: False)
    old = _row("66666666-6666-4666-8666-666666666666", tasks_mod.DONE)
    legacy_only = _row("66666666-6666-4666-8666-666666666666")
    legacy = _legacy([legacy_only, _row("77777777-7777-4777-8777-777777777777")])
    before = _fingerprint(legacy)
    d = _born("66666666-6666-4666-8666-666666666666", owned_session)
    (d / tasks_mod.STORE).write_text(json.dumps([old]), encoding="utf-8")
    (d / "convo.jsonl").write_text(
        json.dumps({"type": "meta", "id": "66666666-6666-4666-8666-666666666666"}) + "\n"
        + json.dumps({"type": "msg", "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8")
    a = _app(monkeypatch, owned_session)
    async with a.run_test(size=(100, 30)):
        assert a.bg_tasks == {}, "boot must not load a store: no conversation is bound yet"
        assert a._resume(d / "convo.jsonl")

        assert list(a.bg_tasks) == [old["id"]]
        assert _ids(d) == [old["id"]]
        assert legacy_only['id'] not in a.bg_tasks
        assert not (d / tasks_mod.MIGRATED).exists()
        assert _fingerprint(legacy) == before


@pytest.mark.asyncio
async def test_an_unwritable_store_is_reported_once_and_the_task_continues(monkeypatch, owned_session):
    """P3: `except OSError: pass` made an accepted row vanish silently. It is
    tolerated (the task must run) but SURFACED - once, not on every transition."""
    a = _app(monkeypatch, owned_session)
    shown: list[str] = []
    a._system = lambda text, *args, **kwargs: shown.append(str(text))
    async with a.run_test(size=(100, 30)) as pilot:
        def deny(*_a, **_k):
            raise PermissionError("store is read-only")

        monkeypatch.setattr(tasks_mod, "save_by_convo", deny)

        async def work():
            return "ok"

        a._start_background("powershell", _QUICK, work())
        (task,) = a.bg_tasks.values()
        await _wait(pilot, task)

        assert task.state == tasks_mod.DONE, "an unwritable store stopped the task"
        assert len([s for s in shown if "task store" in s.lower()]) == 1, shown


@pytest.mark.asyncio
async def test_an_unborn_conversation_at_save_time_is_surfaced_not_swallowed(monkeypatch, owned_session):
    """The tripwire's app half: StoreNotBorn is the regression signal for a
    creator that stopped materialising. It must reach a human."""
    a = _app(monkeypatch, owned_session)
    shown: list[str] = []
    a._system = lambda text, *args, **kwargs: shown.append(str(text))
    async with a.run_test(size=(100, 30)):
        ghost = tasks_mod.new_task("powershell", {"command": "x"}, "never-born")
        a.bg_tasks[ghost.id] = ghost

        a._save_background()

        assert any("never-born" in s or "task store" in s.lower() for s in shown), shown
        assert not (paths.CONVO_DIR / "never-born").exists(), "the tripwire created the directory"


# ── a materialise failure must start NOTHING (already-started work is never orphaned) ──


def _spy_tool(monkeypatch, app, seconds=0.0):
    """Replace the dispatched tool body; the Event says whether it ever ran."""
    started = threading.Event()

    def body(_args):
        started.set()
        time.sleep(seconds)
        return "ran"

    monkeypatch.setattr(app, "_dispatch_for", lambda _name: body)
    return started


def _boom():
    raise OSError("disk full")


@pytest.mark.asyncio
@pytest.mark.parametrize("promotable", [False, True], ids=["explicit-background", "slow-auto-promotable"])
async def test_a_materialise_failure_starts_nothing_and_the_caller_sees_it(monkeypatch, promotable, owned_session):
    """Auto-promotion hands over a call that is ALREADY RUNNING, so the home has
    to exist (or the call has to fail) BEFORE anything starts. The slow producer
    would have outlived the failure untracked: no row, no wake, no kill."""
    a = _app(monkeypatch, owned_session)
    a.settings.tool_auto_background_s = 1 if promotable else 0
    started = _spy_tool(monkeypatch, a, seconds=2 if promotable else 0)
    monkeypatch.setattr(a, "_materialise_convo", _boom)
    async with a.run_test(size=(100, 30)):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            text, ok = await a._execute_tool(
                "powershell", dict(_QUICK, background=not promotable))
            gc.collect()

        assert ok is False and "not started" in text and "disk full" in text, text
        assert not await asyncio.to_thread(started.wait, 0.5), "the tool ran despite the failure"
        assert a.bg_tasks == {}, "a row was accepted for work that never started"
        assert a.store.pending and not (a.convo_dir / "convo.jsonl").exists() and not (a.convo_dir / tasks_mod.STORE).exists(), "phantom transcript/task file"
        assert not [w for w in caught if "never awaited" in str(w.message)], "an awaitable was orphaned"


@pytest.mark.asyncio
@pytest.mark.parametrize("promotable", [False, True], ids=["explicit-background", "slow-auto-promotable"])
async def test_CONTROL_the_same_call_runs_when_the_conversation_can_be_born(monkeypatch, promotable, owned_session):
    """Positive control: without the failure the spy IS reached and a row is
    kept, so the arms above are not green merely because the tool never runs."""
    a = _app(monkeypatch, owned_session)
    a.settings.tool_auto_background_s = 1 if promotable else 0
    started = _spy_tool(monkeypatch, a, seconds=2 if promotable else 0)
    async with a.run_test(size=(100, 30)) as pilot:
        _, ok = await a._execute_tool("powershell", dict(_QUICK, background=not promotable))
        assert ok and await asyncio.to_thread(started.wait, 5)
        (task,) = a.bg_tasks.values()
        await _wait(pilot, task)

        assert not a.store.pending and _ids(a.convo_dir) == [task.id]


@pytest.mark.asyncio
async def test_a_tool_that_cannot_background_does_not_birth_the_conversation(monkeypatch, owned_session):
    """The scope is `backgroundable` tools only: a read on a staged seat leaves it
    staged, so idle traffic (reads, MCP) still mints nothing."""
    a = _app(monkeypatch, owned_session)
    started = _spy_tool(monkeypatch, a)
    async with a.run_test(size=(100, 30)):
        assert not tasks_mod.backgroundable("read"), "the fixture tool became backgroundable"
        await a._execute_tool("read", {"path": "x"})

        assert await asyncio.to_thread(started.wait, 5), "the tool did not run"
        assert a.store.pending and not (a.convo_dir / "convo.jsonl").exists() and not (a.convo_dir / tasks_mod.STORE).exists()
