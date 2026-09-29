"""T0132 - a background task's row lives in ITS conversation's directory.

The store used to be one file, `<data root>/background-tasks.json`, shared by
every seat that ran from the same data root: six live seats, 1.6k rows, one
`.lock`. It now lives at `<data root>/.convos/<convo id>/background-tasks.json`.
Logs stay where they were (`<data root>/output/tasks/<id>.log`), so a row
copied out of the old file keeps tailing.

What this file pins, and why each arm exists:

    MIGRATION IS A COPY-ONLY TOP-UP ON EVERY BIND. The old file is READ, never
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
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from litetui import paths, router_record, row_store, tool_policy
from litetui import tasks as tasks_mod

# ── fixtures ────────────────────────────────────────────────────────────


def _born(cid: str) -> Path:
    d = paths.CONVO_DIR / cid
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


def test_topup_copies_only_this_conversations_rows_verbatim():
    a, b = [_row("c1"), _row("c1", tasks_mod.LOST), _row("c1", tasks_mod.KILLED)], [_row("c2")]
    legacy = _legacy(a + b)
    c1, c2 = _born("c1"), _born("c2")
    before = _fingerprint(legacy)

    copied = tasks_mod.topup(c1, paths.data_root())

    assert copied == 3
    assert row_store.rows_on_disk(c1 / tasks_mod.STORE) == a, "rows must be copied VERBATIM"
    assert not (c2 / tasks_mod.STORE).exists(), "another conversation's directory was touched"
    assert _fingerprint(legacy) == before, "the old file must be byte- and mtime-identical"
    assert not (paths.data_root() / (tasks_mod.STORE + ".lock")).exists(), "the old file's lock was touched"


def test_topup_is_idempotent():
    _legacy([_row("c1"), _row("c1")])
    c1 = _born("c1")
    tasks_mod.topup(c1, paths.data_root())
    store, marker = c1 / tasks_mod.STORE, c1 / tasks_mod.MIGRATED
    first = (store.read_bytes(), marker.read_bytes())

    assert tasks_mod.topup(c1, paths.data_root()) == 0
    assert (store.read_bytes(), marker.read_bytes()) == first, "a second run changed something"


def test_a_crash_before_the_marker_reruns_without_duplicating(monkeypatch):
    rows = [_row("c1"), _row("c1")]
    _legacy(rows)
    c1 = _born("c1")

    def boom(*_a, **_k):
        raise RuntimeError("crash between the copy and the marker")

    real = tasks_mod._write_marker
    monkeypatch.setattr(tasks_mod, "_write_marker", boom)
    with pytest.raises(RuntimeError):
        tasks_mod.topup(c1, paths.data_root())
    assert sorted(_ids(c1)) == sorted(r["id"] for r in rows), "the copy did not land before the crash"
    assert not (c1 / tasks_mod.MIGRATED).exists(), "the marker must be written LAST"

    monkeypatch.setattr(tasks_mod, "_write_marker", real)
    tasks_mod.topup(c1, paths.data_root())

    assert sorted(_ids(c1)) == sorted(r["id"] for r in rows), "rerun duplicated or lost rows"
    assert (c1 / tasks_mod.MIGRATED).exists()


def test_rows_an_old_writer_appends_later_are_picked_up():
    """The reason the top-up runs on EVERY bind and the marker is a signature."""
    first = [_row("c1")]
    legacy = _legacy(first)
    c1 = _born("c1")
    tasks_mod.topup(c1, paths.data_root())

    # A seat still on the old code resumes c1 and appends, via the real writer.
    late = [_row("c1"), _row("c1")]
    row_store.forget(legacy)
    row_store.write(legacy, first + late, prefix=".tasks-")
    before = _fingerprint(legacy)

    assert tasks_mod.topup(c1, paths.data_root()) == 2
    assert sorted(_ids(c1)) == sorted(r["id"] for r in first + late)
    assert _fingerprint(legacy) == before, "the top-up wrote the old file"


def test_the_same_id_in_both_places_keeps_the_conversations_copy():
    mine = _row("c1", tasks_mod.DONE)
    theirs = dict(mine, state=tasks_mod.RUNNING, label="the old file's stale copy")
    _legacy([theirs])
    c1 = _born("c1")
    (c1 / tasks_mod.STORE).write_text(json.dumps([mine]), encoding="utf-8")

    assert tasks_mod.topup(c1, paths.data_root()) == 0
    assert row_store.rows_on_disk(c1 / tasks_mod.STORE) == [mine]


def test_an_unborn_conversation_is_left_alone_and_nothing_is_created():
    legacy = _legacy([_row("gone")])
    before = _fingerprint(legacy)

    assert tasks_mod.topup(paths.CONVO_DIR / "gone", paths.data_root()) == 0

    assert not (paths.CONVO_DIR / "gone").exists(), "a top-up must never create a conversation"
    assert _fingerprint(legacy) == before


def test_an_absent_or_corrupt_old_file_is_not_an_error():
    c1 = _born("c1")
    assert tasks_mod.topup(c1, paths.data_root()) == 0
    (paths.data_root() / tasks_mod.STORE).write_text("{not json", encoding="utf-8")
    assert tasks_mod.topup(c1, paths.data_root()) == 0


def test_concurrent_writers_to_one_store_lose_nothing():
    """The file lock + id-keyed delta is what serialises writers, not the lease:
    a process leaves the lease when it rebinds while its tasks keep running."""
    c1 = _born("c1")
    store = c1 / tasks_mod.STORE
    per_thread = [[_row("c1") for _ in range(5)] for _ in range(6)]

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


def test_add_missing_never_overwrites_a_row_that_is_already_there():
    c1 = _born("c1")
    store = c1 / tasks_mod.STORE
    kept = _row("c1", tasks_mod.DONE)
    store.write_text(json.dumps([kept]), encoding="utf-8")

    added = row_store.add_missing(store, [dict(kept, state=tasks_mod.RUNNING), _row("c1")], prefix=".tasks-")

    assert added == 1
    assert row_store.rows_on_disk(store)[0] == kept


# ── the save: partitioned, and it never creates a conversation ─────────


def test_save_by_convo_writes_each_originating_store():
    c1, c2 = _born("c1"), _born("c2")
    t1 = tasks_mod.new_task("bash", {"command": "a"}, "c1")
    t2 = tasks_mod.new_task("bash", {"command": "b"}, "c2")

    tasks_mod.save_by_convo([t1, t2])

    assert _ids(c1) == [t1.id] and _ids(c2) == [t2.id]
    assert not (paths.data_root() / tasks_mod.STORE).exists(), "the shared file was written"


def test_saving_for_an_unborn_conversation_raises_and_creates_nothing():
    for bad in ("never-born", "", "..", "a/b"):
        t = tasks_mod.new_task("bash", {"command": "a"}, bad)
        before = sorted(p.name for p in paths.data_root().rglob("*"))
        with pytest.raises(tasks_mod.StoreNotBorn):
            tasks_mod.save_by_convo([t])
        assert sorted(p.name for p in paths.data_root().rglob("*")) == before, f"{bad!r} left a trace"


def test_one_unborn_conversation_does_not_stop_the_others_being_saved():
    c1 = _born("c1")
    ok = tasks_mod.new_task("bash", {"command": "a"}, "c1")
    orphan = tasks_mod.new_task("bash", {"command": "b"}, "c9")

    with pytest.raises(tasks_mod.StoreNotBorn):
        tasks_mod.save_by_convo([orphan, ok])

    assert _ids(c1) == [ok.id], "a refused conversation took the healthy one down with it"


# ── bind: load one conversation's rows, keep what this instance runs ───


def test_bind_loads_only_that_conversations_rows(monkeypatch):
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: False)
    mine, other = _row("c1"), _row("c2")
    _legacy([mine, other])
    c1 = _born("c1")
    _born("c2")
    held: dict = {}

    tasks_mod.bind(held, c1, paths.data_root())

    assert list(held) == [mine["id"]]


def test_bind_keeps_rows_of_other_conversations_this_instance_runs():
    c1 = _born("c1")
    running_elsewhere = tasks_mod.new_task("bash", {"command": "sleep 9"}, "c2")
    held = {running_elsewhere.id: running_elsewhere}

    tasks_mod.bind(held, c1, paths.data_root())

    assert held[running_elsewhere.id] is running_elsewhere


def test_a_rebind_keeps_this_instances_own_running_task_object():
    """Resume away and back: the in-memory Task carries the live child handle
    (`proc`), which no disk copy can. It must not be replaced by its own row."""
    c1 = _born("c1")
    t = tasks_mod.new_task("bash", {"command": "sleep 9"}, "c1")
    t.proc = object()
    tasks_mod.save([t], c1)
    held = {t.id: t}

    tasks_mod.bind(held, c1, paths.data_root())

    assert held[t.id] is t and t.state == tasks_mod.RUNNING and t.proc is not None


def test_a_rebind_does_not_mark_this_instances_own_task_lost(monkeypatch):
    """`load` used to run once at boot, when a row carrying OUR pid could only be
    a dead predecessor's. Bind loads mid-session, so the rule is the INSTANCE."""
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: True)
    c1 = _born("c1")
    t = tasks_mod.new_task("bash", {"command": "sleep 9"}, "c1")
    tasks_mod.save([t], c1)

    assert tasks_mod.load(c1)[t.id].state == tasks_mod.RUNNING


def test_our_pid_on_a_row_from_another_instance_is_still_a_reused_pid(monkeypatch):
    """The Sentinel 94cedaec rule survives: same pid, different instance = a dead
    predecessor that happened to hold this number."""
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: True)
    c1 = _born("c1")
    t = tasks_mod.new_task("bash", {"command": "sleep 9"}, "c1")
    t.owner_instance = "a-predecessor-that-held-this-pid"
    assert t.owner_pid == os.getpid()
    tasks_mod.save([t], c1)

    assert tasks_mod.load(c1)[t.id].state == tasks_mod.LOST


def test_a_dead_owners_running_row_is_lost_after_bind(monkeypatch):
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: False)
    r = _row("c1", tasks_mod.RUNNING, owner_pid=4_000_000, owner_instance="dead", owner_created=None)
    _legacy([r])
    c1 = _born("c1")
    held: dict = {}

    tasks_mod.bind(held, c1, paths.data_root())

    assert held[r["id"]].state == tasks_mod.LOST
    # ...and the stamping reached the conversation's store, not just memory.
    tasks_mod.save_by_convo(held.values())
    assert row_store.rows_on_disk(c1 / tasks_mod.STORE)[0]["state"] == tasks_mod.LOST


def test_bind_never_writes_the_old_file(monkeypatch):
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: False)
    legacy = _legacy([_row("c1", tasks_mod.RUNNING, owner_pid=None), _row("c1")])
    c1 = _born("c1")
    before = _fingerprint(legacy)

    held: dict = {}
    tasks_mod.bind(held, c1, paths.data_root())
    tasks_mod.save_by_convo(held.values())

    assert _fingerprint(legacy) == before


# ── the app: the creator materialises, the finish writes the origin ────


def _app(monkeypatch):
    from litetui import app as app_mod

    monkeypatch.setattr(app_mod.LiteTUI, "connect", lambda self: None)
    a = app_mod.LiteTUI()
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
async def test_a_task_started_on_a_staged_conversation_is_kept(monkeypatch, promoted):
    """E1: `gui.tools.execute` reaches `_execute_tool` with no turn, so the
    conversation is still STAGED. Explicit `background` and auto-promotion both
    end in `_start_background`, the single creator."""
    a = _app(monkeypatch)
    a.settings.tool_auto_background_s = 1 if promoted else 0
    async with a.run_test(size=(100, 30)) as pilot:
        assert a.store.pending and not a.convo_dir.exists(), "the fixture is not staged"
        args = {"command": "Start-Sleep -Seconds 2"} if promoted else dict(_QUICK, background=True)
        _, ok = await a._execute_tool("powershell", args)
        assert ok
        (task,) = a.bg_tasks.values()
        await _wait(pilot, task)

        assert not a.store.pending and a.convo_dir.is_dir(), "the creator did not materialise"
        rows = row_store.rows_on_disk(a.convo_dir / tasks_mod.STORE)
        assert [r["id"] for r in rows] == [task.id] and rows[0]["state"] == tasks_mod.DONE
        assert not (paths.data_root() / tasks_mod.STORE).exists(), "the shared file was written"
        assert len(list(paths.CONVO_DIR.iterdir())) == 1, "more than one conversation was minted"


@pytest.mark.asyncio
async def test_the_creator_itself_materialises_so_no_route_can_skip_it(monkeypatch):
    """E2 (`/skill` streams without materialising) and any route added later:
    the guarantee lives at `_start_background`, not at its callers."""
    a = _app(monkeypatch)
    async with a.run_test(size=(100, 30)) as pilot:
        async def work():
            return "done"

        a._start_background("powershell", _QUICK, work())
        (task,) = a.bg_tasks.values()
        await _wait(pilot, task)

        assert a.convo_dir.is_dir() and _ids(a.convo_dir) == [task.id]


@pytest.mark.asyncio
async def test_a_task_that_finishes_after_a_resume_writes_its_originating_store(monkeypatch):
    """R2: the finish lands on the loop long after the user may have moved on.
    Its row belongs to the conversation that STARTED it."""
    a = _app(monkeypatch)
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
        other = _born("resumed-elsewhere")
        (other / "convo.jsonl").write_text(
            json.dumps({"type": "meta", "id": "resumed-elsewhere"}) + "\n"
            + json.dumps({"type": "msg", "message": {"role": "user", "content": "hi"}}) + "\n",
            encoding="utf-8")
        assert a._resume(other / "convo.jsonl")
        assert a.convo_id == "resumed-elsewhere"

        release.set()
        await _wait(pilot, task)

        assert task.convo_id == origin.name
        finished = row_store.rows_on_disk(origin / tasks_mod.STORE)
        assert [(r["id"], r["state"]) for r in finished] == [(task.id, tasks_mod.DONE)]
        assert not (other / tasks_mod.STORE).exists(), "the CURRENT conversation's store got the row"
        assert tasks_mod.log_path(task, paths.data_root()).is_file(), "the log moved"
        assert task.log.startswith(paths.data_root().as_posix())


@pytest.mark.asyncio
async def test_resume_loads_the_conversations_rows_and_tops_up_from_the_old_file(monkeypatch):
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: False)
    old = _row("resumed", tasks_mod.DONE)
    legacy = _legacy([old, _row("someone-else")])
    before = _fingerprint(legacy)
    d = _born("resumed")
    (d / "convo.jsonl").write_text(
        json.dumps({"type": "meta", "id": "resumed"}) + "\n"
        + json.dumps({"type": "msg", "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8")
    a = _app(monkeypatch)
    async with a.run_test(size=(100, 30)):
        assert a.bg_tasks == {}, "boot must not load a store: no conversation is bound yet"
        assert a._resume(d / "convo.jsonl")

        assert list(a.bg_tasks) == [old["id"]]
        assert _ids(d) == [old["id"]]
        assert _fingerprint(legacy) == before


@pytest.mark.asyncio
async def test_an_unwritable_store_is_reported_once_and_the_task_continues(monkeypatch):
    """P3: `except OSError: pass` made an accepted row vanish silently. It is
    tolerated (the task must run) but SURFACED - once, not on every transition."""
    a = _app(monkeypatch)
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
async def test_an_unborn_conversation_at_save_time_is_surfaced_not_swallowed(monkeypatch):
    """The tripwire's app half: StoreNotBorn is the regression signal for a
    creator that stopped materialising. It must reach a human."""
    a = _app(monkeypatch)
    shown: list[str] = []
    a._system = lambda text, *args, **kwargs: shown.append(str(text))
    async with a.run_test(size=(100, 30)):
        ghost = tasks_mod.new_task("powershell", {"command": "x"}, "never-born")
        a.bg_tasks[ghost.id] = ghost

        a._save_background()

        assert any("never-born" in s or "task store" in s.lower() for s in shown), shown
        assert not (paths.CONVO_DIR / "never-born").exists(), "the tripwire created the directory"
