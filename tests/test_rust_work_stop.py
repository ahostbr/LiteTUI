"""Reviewed Task Stop waits for actual owned work and reads durable target state."""
import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_rust_background_input import app as _owned_app
from test_rust_background_input import armed as _armed
from test_rust_background_input import settle_inputs
from test_rust_background_input import switch_app as _switch_app

from litetui import background_input as work
from litetui import gui_rpc, native_work, tasks

app = _owned_app
armed = _armed
switch_app = _switch_app

pytestmark = pytest.mark.asyncio
PREFIX = "gui.work.native_"
REAL_POPEN = subprocess.Popen


def common(app):
    return {"version":1, **work.identity(app)}


async def call(app, operation, **fields):
    return await gui_rpc.async_dispatch(app, {"type":PREFIX+operation, "id":"stop-"+operation,
        **common(app), **fields})


def export(name, value):
    if output := os.environ.get("LITETUI_WORK_STOP_FIXTURE"):
        path = Path(output)
        document = json.loads(path.read_text()) if path.exists() else {}
        document[name] = value
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def start(armed, monkeypatch):
    release = asyncio.Event()
    async def pending():
        await release.wait()
        return "controlled pending-spawn completion"
    monkeypatch.setattr(armed, "notify", lambda *_args, **_kwargs: None)
    armed._start_background("powershell", {"command":"controlled pending spawn"}, pending())
    task = list(armed.bg_tasks.values())[-1]
    return task, release


async def review(armed, task):
    snapshot = await call(armed, "snapshot")
    page = await call(armed, "page", instance=snapshot["instance"], snapshot=snapshot["snapshot"], section="tasks")
    row = next(row for row in page["rows"] if row["row"]["id"] == task.id)
    result = await call(armed, "stop_validate", instance=snapshot["instance"], snapshot=snapshot["snapshot"],
        task_id=task.id, task_revision=row["revision"])
    return result, snapshot, row


async def apply(armed, reviewed):
    return await call(armed, "stop_apply", **{key:value for key,value in reviewed.items() if key not in ("kind", *common(armed))})


async def settled(armed, result):
    return await call(armed, "stop_settled", **{key:result[key] for key in ("instance","snapshot","review","action_id","receipt")})


async def test_pending_spawn_stop_is_pure_until_apply_and_waits_for_worker_then_independent_readback(armed, monkeypatch):
    task, release = start(armed, monkeypatch)
    before = armed.convo_path.read_bytes(), copy.deepcopy(task.to_row())
    reviewed, snapshot, row = await review(armed, task)
    assert (armed.convo_path.read_bytes(), task.to_row()) == before
    assert reviewed["stop_mode"] == "pending_spawn" and reviewed["process_id"] is None
    stopping = asyncio.create_task(apply(armed, reviewed))
    await asyncio.sleep(0.02)
    assert task.state == tasks.KILLED and not stopping.done()
    release.set()
    result = await asyncio.wait_for(stopping, 3)
    assert result["status"] == "stopped", result
    assert result["proof"]["process_state"] == "not_spawned" and result["proof"]["pending_spawn_settled"]
    assert result["row"]["row"] == task.to_row() and result["row"]["revision"] != row["revision"]
    proof = await settled(armed, result)
    assert proof["verified"] and proof["row"] == result["row"]
    await settle_inputs(armed, 1)
    assert len(armed._rewrite_test_requests) == 1
    with pytest.raises(ValueError, match="fresh unconsumed"):
        await apply(armed, reviewed)
    export("success", {"snapshot":snapshot,"row":row,"review":reviewed,"result":result,"settled":proof})


async def test_naturally_finished_task_refuses_stale_review_without_kill(armed, monkeypatch):
    task, release = start(armed, monkeypatch)
    reviewed, snapshot, row = await review(armed, task)
    release.set()
    await settle_inputs(armed, 1)
    calls = []
    monkeypatch.setattr(armed, "_kill_background", lambda ident: calls.append(ident))
    result = await apply(armed, reviewed)
    assert result["status"] == "refused" and not result["mutated"] and not calls
    export("refused", {"snapshot":snapshot,"row":row,"review":reviewed,"result":result})


async def test_replaced_task_object_cannot_borrow_reviewed_identity(armed, monkeypatch):
    task, release = start(armed, monkeypatch)
    reviewed, _snapshot, _row = await review(armed, task)
    armed.bg_tasks[task.id] = copy.copy(task)
    result = await apply(armed, reviewed)
    assert result["status"] == "refused" and not result["mutated"]
    assert task.state == tasks.RUNNING
    armed.bg_tasks[task.id] = task
    release.set()
    await settle_inputs(armed, 1)


async def test_row_persistence_failure_is_uncertain_and_latches_source_admission(armed, monkeypatch):
    task, release = start(armed, monkeypatch)
    reviewed, snapshot, row = await review(armed, task)
    original = native_work._read
    def failed(path):
        if Path(path).name == tasks.STORE:
            return b"[]", "a"*64
        return original(path)
    monkeypatch.setattr(native_work, "_read", failed)
    stopping = asyncio.create_task(apply(armed, reviewed))
    await asyncio.sleep(0.02)
    release.set()
    result = await asyncio.wait_for(stopping, 3)
    assert result["status"] == "uncertain" and result["close_required"] and result["mutated"]
    assert result["proof"] is None and work.prompt_refusal(armed)
    with pytest.raises(ValueError, match="successful receipt"):
        await settled(armed, result)
    export("uncertain", {"snapshot":snapshot,"row":row,"review":reviewed,"result":result})


async def test_independent_readback_rejects_replaced_receipt_and_changed_target_row(armed, monkeypatch):
    task, release = start(armed, monkeypatch)
    reviewed, _snapshot, _row = await review(armed, task)
    stopping = asyncio.create_task(apply(armed, reviewed))
    await asyncio.sleep(0.02)
    release.set()
    result = await asyncio.wait_for(stopping, 3)
    assert result["status"] == "stopped", result
    changed = {**result, "receipt":"f"*64}
    with pytest.raises(ValueError, match="exact successful"):
        await settled(armed, changed)
    task.label = "changed after retirement"
    with pytest.raises(ValueError, match="persist exactly"):
        await settled(armed, result)


@pytest.mark.parametrize("mode", ["process", "late_spawn", "unconfirmed_kill"])
async def test_controlled_process_and_pending_handoff_require_exact_retirement(armed, monkeypatch, mode):
    spawn = asyncio.Event()
    attached = asyncio.Event()
    processes = []
    monkeypatch.setattr(armed, "notify", lambda *_args, **_kwargs: None)
    async def process():
        await spawn.wait()
        proc = REAL_POPEN([sys.executable, "-u", "-c", "import time; print('owned child',flush=True); time.sleep(20)"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(proc)
        task = tasks.CURRENT.get()
        stop_won = tasks.attach_process(task, proc)
        attached.set()
        if stop_won:
            proc.kill()
            tasks.record_process_kill(proc, True)
        stdout, stderr = await asyncio.to_thread(proc.communicate)
        tasks.record_process_retirement(proc, pipes_settled=True)
        return stdout + stderr
    async def kill(task):
        proc = task.proc
        proc.kill()
        await asyncio.to_thread(proc.wait)
        tasks.record_process_kill(proc, mode != "unconfirmed_kill")
    monkeypatch.setattr(armed, "_kill_background_tree", lambda task: armed.run_worker(kill(task), group="cancel", exit_on_error=False))
    armed._start_background("powershell", {"command":"controlled Python child"}, process())
    task = list(armed.bg_tasks.values())[-1]
    try:
        if mode != "late_spawn":
            spawn.set()
            await asyncio.wait_for(attached.wait(), 3)
        reviewed, snapshot, row = await review(armed, task)
        assert reviewed["stop_mode"] == ("pending_spawn" if mode == "late_spawn" else "process")
        stopping = asyncio.create_task(apply(armed, reviewed))
        if mode == "late_spawn":
            await asyncio.sleep(0.02)
            assert task.state == tasks.KILLED and not stopping.done() and not processes
            spawn.set()
        result = await asyncio.wait_for(stopping, 5)
        assert processes and processes[0].poll() is not None
        if mode == "unconfirmed_kill":
            assert result["status"] == "uncertain" and result["close_required"] and result["proof"] is None
            assert not tasks.process_retired(processes[0]) and work.prompt_refusal(armed)
        else:
            assert result["status"] == "stopped", result
            assert result["proof"]["process_state"] == "retired"
            assert result["proof"]["process_id"] == processes[0].pid and tasks.process_retired(processes[0])
            proof = await settled(armed, result)
            assert proof["verified"]
            await settle_inputs(armed, 1)
        export(mode, {"snapshot":snapshot,"row":row,"review":reviewed,"result":result,
                      **({"settled":proof} if mode != "unconfirmed_kill" else {})})
    finally:
        spawn.set()
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


async def test_quit_during_stop_joins_pending_spawn_and_retains_late_input(armed, monkeypatch):
    task, release = start(armed, monkeypatch)
    reviewed, snapshot, row = await review(armed, task)
    stopping = asyncio.create_task(apply(armed, reviewed))
    await asyncio.sleep(0.02)
    assert task.state == tasks.KILLED and not stopping.done()
    armed._gui_quitting = True
    work.begin_close(armed)
    closing = asyncio.create_task(work.settle_before_shutdown(armed))
    try:
        await asyncio.sleep(0.02)
        assert not closing.done() and not stopping.done()
    finally:
        release.set()  # the exact pending spawn/callback must really return
    shutdown = await asyncio.wait_for(closing, 5)
    armed._work_test_shutdown = shutdown
    result = await asyncio.wait_for(stopping, 3)
    assert result["status"] == "stopped", result
    assert shutdown["worker_settled"] and shutdown["process_settled"] and shutdown["callback_settled"]
    assert task.proc is None and not armed._rewrite_test_requests
    assert all(entry.state in {"held", "uncertain"} for entry in work.bridge(armed).inputs.values())
    export("quit", {"snapshot":snapshot,"row":row,"review":reviewed,"result":result,"shutdown":shutdown})


async def test_foreign_owner_and_forged_review_refuse_without_kill(armed, monkeypatch):
    task, release = start(armed, monkeypatch)
    owner = task.owner_instance
    try:
        task.owner_instance = "foreign-runtime"
        with pytest.raises(ValueError, match="exact current owner"):
            await review(armed, task)
        task.owner_instance = owner
        reviewed, _snapshot, _row = await review(armed, task)
        forged = {**reviewed, "producer_id":"00000000-0000-4000-8000-000000000099"}
        with pytest.raises(ValueError, match="exactly echo"):
            await apply(armed, forged)
        assert task.state == tasks.RUNNING
        with pytest.raises(ValueError, match="fresh unconsumed"):
            await apply(armed, reviewed)
    finally:
        task.owner_instance = owner
        release.set()
    await settle_inputs(armed, 1)


async def test_independent_readback_allows_other_task_store_changes(armed, monkeypatch):
    task, release = start(armed, monkeypatch)
    reviewed, _snapshot, _row = await review(armed, task)
    stopping = asyncio.create_task(apply(armed, reviewed))
    await asyncio.sleep(0.02)
    release.set()
    result = await asyncio.wait_for(stopping, 3)
    assert result["status"] == "stopped", result
    other = tasks.new_task("powershell", {"command":"other completed task"}, armed.convo_id)
    other.state, other.ended, other.ok = tasks.DONE, other.started + 1, True
    armed.bg_tasks[other.id] = other
    assert armed._save_background()
    readback = await settled(armed, result)
    assert readback["verified"] and readback["row"] == result["row"]
    assert readback["store_revision"] != result["store_revision"]


async def test_changed_logical_worker_identity_refuses_before_stop_effect(armed, monkeypatch):
    task, release = start(armed, monkeypatch)
    reviewed, _snapshot, _row = await review(armed, task)
    producer = next(record for record in work.bridge(armed).producers.values() if record.task is task)
    original = producer.worker_id
    producer.worker_id = "00000000-0000-4000-8000-000000000098"
    try:
        result = await apply(armed, reviewed)
        assert result["status"] == "refused" and not result["mutated"] and task.state == tasks.RUNNING
    finally:
        producer.worker_id = original
        release.set()
    await settle_inputs(armed, 1)
