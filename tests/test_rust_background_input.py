"""Captured background completion owns queue admission and exact lifetimes."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_canonical_safe_boundary_refusal_retains_original_fifo_item(monkeypatch):
    from litetui import backend_transition, claude_turn, hook_host
    from litetui.app import LiteTUI

    first = {"content": "Completed task", "source": "harness", "tool_profile": "strict"}
    second = {"content": "Next completion", "source": "harness", "tool_profile": "strict"}
    app = SimpleNamespace(_pending_input=[first, second], _stop_requested=False)
    monkeypatch.setattr(backend_transition, "submission_refusal", lambda _app: None)
    monkeypatch.setattr(claude_turn, "queue_ready", lambda _app, _item: True)
    monkeypatch.setattr(hook_host, "accept_prompt", lambda _app, _item: False)

    assert LiteTUI._deliver_queued_input(app) is False
    assert app._pending_input == [first, second]
    assert app._pending_input[0] is first

import asyncio
import copy
import json

import pytest
import pytest_asyncio
from test_rust_conversation_rewrite import app as _owned_app
from test_rust_conversation_rewrite import switch_app as _switch_app

from litetui import background_input as work
from litetui import hook_host
from litetui.event_origin import EventOrigin

switch_app = _switch_app
app = _owned_app


def command(app, **fields):
    return {"version": 1, "id": "work-test", **work.identity(app), **fields}


@pytest_asyncio.fixture
async def armed(app, monkeypatch, request):
    import os

    from litetui import rpc
    from litetui.app import LiteTUI
    events, wire, authorities = [], [], []
    monkeypatch.setattr(rpc, "rpc_emit", lambda row: wire.append(copy.deepcopy(row)))
    def emit(event, *, origin=None):
        from litetui import conversation_action
        current = work.current_origin(app)
        attempt = conversation_action._attempt(app)
        action_id = None
        if attempt is not None and attempt.pending:
            action_id = json.loads(attempt.review.wire)["action_id"]
            if current is None and attempt.segments:
                current = work.bridge(app).executions[attempt.segments[-1]][1]
            elif (current is None and attempt.kind == "compact" and attempt.root_started
                  and attempt.execution is not None and attempt.execution.worker.is_finished
                  and attempt.execution.origin.execution_id == json.loads(attempt.review.wire)["root_execution_id"]):
                # Independent exact source root, never borrowed from the event.
                current = attempt.execution.origin
        if event.get("type", "").startswith("work_"):
            authorities.append({"instance": event["instance"], "sequence": event["sequence"], "current": None if current is None else {
                "origin": current.envelope_fields(), "action_id": action_id}})
        events.append((copy.deepcopy(event), origin))
        LiteTUI._rpc_emit(app, event, origin=origin)
    monkeypatch.setattr(app, "_rpc_emit", emit)
    monkeypatch.setattr(app, "_user_bubble", lambda *_args, **_kwargs: SimpleNamespace(border_title="You"))
    monkeypatch.setattr(app, "_chat_running", lambda: any(w.group == "chat" and not w.is_finished for w in app.workers))
    if getattr(request, "param", None) == "codex":
        monkeypatch.setattr(app.backend, "name", "codex")
        app._agent_session.update_execution(backend="codex", model=app.model_id, thinking_level="default")
        monkeypatch.setattr(app.backend, "app_server", object(), raising=False)
        from litetui import seat_authority
        # The transport is controlled and old-model is intentionally fictional;
        # canonical fleet/model-floor behavior has its independent source suite.
        monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: None)
    if getattr(request, "param", None) == "claude":
        from litetui import seat_authority
        from litetui.claude_backend import ClaudeBackend
        app.backend = ClaudeBackend(app.settings)
        app._agent_session.update_execution(backend="claude", model=app.model_id, thinking_level="default")
        monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: None)
    descriptor = work.capture(app, command(app))
    assert events == [] and not work.enabled(app)
    active = work.activate(app, command(app, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    assert events == [] and active["sequence"] == 0
    app._work_test_events = events
    epochs = {descriptor["instance"]: [descriptor, active, work.bridge(app)]}
    original_capture, original_activate = work.capture, work.activate
    def capture(app, cmd):
        result = original_capture(app, cmd)
        epochs.setdefault(result["instance"], [result, None, work.bridge(app)])
        return result
    def activate(app, cmd):
        result = original_activate(app, cmd)
        epochs[result["instance"]][1] = result
        return result
    monkeypatch.setattr(work, "capture", capture)
    monkeypatch.setattr(work, "activate", activate)
    yield app
    value = work.bridge(app)
    if os.environ.get("LITETUI_WORK_FIXTURES"):
        root = Path(os.environ["LITETUI_WORK_FIXTURES"])
        root.mkdir(parents=True, exist_ok=True)
        for ordinal, (capture, activation, instance) in enumerate(epochs.values()):
            if activation is None:
                continue
            shutdown = getattr(app, "_work_test_shutdown", None)
            if shutdown is None or shutdown["instance"] != instance.instance:
                shutdown = getattr(instance, "transition_proof", None)
            if shutdown is None and instance.shutdown_task is not None and instance.shutdown_task.done() and not instance.shutdown_task.cancelled():
                try:
                    shutdown = instance.shutdown_task.result()
                except Exception:  # noqa: BLE001 - failed proof remains explicitly absent in export
                    shutdown = None
            (root / (request.node.name + ("" if ordinal == 0 else f".epoch-{ordinal}") + ".json")).write_text(json.dumps({
                "capture": capture, "active": activation,
                "events": [row for row in wire if row.get("instance", row.get("event", {}).get("instance")) == instance.instance],
                "authority": [{key: val for key, val in row.items() if key != "instance"} for row in authorities if row["instance"] == instance.instance],
                "shutdown": shutdown,
                "reconciliations": [row for row in getattr(app, "_work_test_reconciliations", []) if row["instance"] == instance.instance],
            }, ensure_ascii=False), encoding="utf-8")
    for worker in list(app.workers):
        if not worker.is_finished:
            worker.cancel()
    pending = [i.observer for i in value.inputs.values() if i.observer is not None]
    pending += [p.observer for p in value.producers.values() if p.observer is not None]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)



async def settle_inputs(app, count):
    async def completed():
        while len(work.bridge(app).inputs) < count:
            await asyncio.sleep(0.01)
        while any(entry.observer is None for entry in work.bridge(app).inputs.values()):
            await asyncio.sleep(0.01)
        await asyncio.gather(*(entry.observer for entry in work.bridge(app).inputs.values()))
    await asyncio.wait_for(completed(), 5)


@pytest.mark.asyncio
async def test_two_phase_capture_does_not_write_or_wake_and_rejects_reactivation(app, monkeypatch):
    events = []
    monkeypatch.setattr(app, "_rpc_emit", lambda *args, **kwargs: events.append(args))
    before = app.convo_path.read_bytes()
    schedules = getattr(app, "_gui_schedules_paused", False)
    descriptor = work.capture(app, command(app))
    assert work.capture(app, command(app)) == descriptor
    assert not (app.convo_dir / work.FILENAME).exists()
    assert not events and not app._rewrite_test_requests
    with pytest.raises(ValueError):
        work.activate(app, command(app, instance=descriptor["instance"], activation_token="wrong"))
    work.activate(app, command(app, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    with pytest.raises(ValueError):
        work.activate(app, command(app, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    assert app.convo_path.read_bytes() == before and not events
    assert getattr(app, "_gui_schedules_paused", False) == schedules


@pytest.mark.asyncio
async def test_actual_background_completion_wakes_one_owned_new_turn_after_durable_claim(armed):
    gate = asyncio.Event()
    async def output():
        await gate.wait()
        return "Canonical task output"
    armed._start_background("bash", {"command": "controlled fixture"}, output())
    value = work.bridge(armed)
    producer = next(iter(value.producers.values()))
    assert producer.phase == "running" and producer.worker is not None
    gate.set()
    await settle_inputs(armed, 1)
    entry = next(iter(value.inputs.values()))
    assert entry.state == "settled" and entry.admission["mode"] == "new_turn"
    assert entry.ref["source"] == entry.admission["origin"]["source"] == "harness"
    assert len(armed._rewrite_test_requests) == 1
    origin, history = armed._rewrite_test_requests[0]
    assert origin.envelope_fields() == entry.admission["origin"]
    assert history[-1]["work_input_id"] == entry.ref["input_id"]
    raw = json.loads((armed.convo_dir / work.FILENAME).read_text(encoding="utf-8"))
    assert raw["inputs"][0]["state"] == "settled"
    phases = [row[0]["type"] for row in armed._work_test_events]
    assert phases.index("work_admission") < phases.index("work_settled")
    assert producer.phase == "retired" and producer.worker.is_finished


@pytest.mark.asyncio
async def test_actual_mid_turn_completion_is_admitted_at_safe_boundary_without_new_worker(armed, monkeypatch):
    first = asyncio.Event()
    completed = asyncio.Event()
    original = armed._stream_worker
    async def stream(origin):
        first.set()
        await completed.wait()
        assert await hook_host.queued_prompt(armed)
        await original(origin)
    monkeypatch.setattr(armed, "_stream_worker", stream)
    parent = armed._stream(origin=EventOrigin(session_id=armed.convo_id, operation_id="current-turn", source="rpc"))
    await first.wait()
    async def output():
        return "Mid-turn completion"
    armed._start_background("bash", {"command": "controlled fixture"}, output())
    producer = next(iter(work.bridge(armed).producers.values()))
    await producer.worker.wait()
    assert len(armed._pending_input) == 1 and not armed._rewrite_test_requests
    completed.set()
    await parent.wait()
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.admission["mode"] == "in_turn" and entry.worker is parent
    assert entry.admission["boundary"] == 1
    assert entry.admission["parent_execution_id"] == entry.admission["origin"]["execution_id"]
    assert entry.admission["origin"]["source"] == "rpc" and entry.ref["source"] == "harness"
    assert len(armed._rewrite_test_requests) == 1 and not armed._pending_input


@pytest.mark.asyncio
async def test_required_append_failure_retains_uncertain_input_without_replay(armed, monkeypatch):
    original = armed.store.write_record
    def fail(record):
        if record.get("message", {}).get("work_input_id") or record.get("work_input_id"):
            armed.store.persist_error = "controlled write failure"
            return
        return original(record)
    monkeypatch.setattr(armed.store, "write_record", fail)
    async def output():
        return "Never replay this completion"
    armed._start_background("bash", {"command": "fixture"}, output())
    producer = next(iter(work.bridge(armed).producers.values()))
    from textual.worker import WorkerFailed
    with pytest.raises(WorkerFailed):
        await producer.worker.wait()
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.state == "uncertain" and not armed._rewrite_test_requests
    assert armed._pending_input[0] is entry.item
    assert work.admit_sync(armed, entry.item) is False
    assert not armed._rewrite_test_requests


@pytest.mark.asyncio
async def test_exact_action_background_aftercare_keeps_root_and_new_segment(armed, monkeypatch):
    from test_rust_conversation_rewrite import apply, reviewed, settle

    from litetui import conversation_action as action
    original = armed._stream_worker
    calls = []
    async def stream(origin):
        calls.append(origin)
        action.segment(armed, origin)
        if len(calls) == 1:
            async def output():
                await asyncio.sleep(0)
                return "Action completion"
            armed._start_background("bash", {"command": "fixture"}, output())
        await original(origin)
    monkeypatch.setattr(armed, "_stream_worker", stream)
    review = reviewed(armed)
    accepted = await apply(armed, review)
    assert accepted["turn"] == "accepted"
    proof = await asyncio.wait_for(settle(armed, review), 5)
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.admission["mode"] == "action_segment"
    assert entry.admission["origin"]["source"] == "rpc"
    assert entry.ref["source"] == "harness"
    assert entry.admission["operation_id"] == "rewrite-execution"
    assert entry.admission["parent_execution_id"] == calls[0].execution_id
    assert len(calls) == 2 and proof["execution"]["segments"] == 2
    assert not action._attempt(armed).pending


@pytest.mark.asyncio
async def test_simultaneous_completions_remain_fifo_one_per_safe_boundary(armed, monkeypatch):
    entered, ready = asyncio.Event(), asyncio.Event()
    original = armed._stream_worker
    async def stream(origin):
        entered.set()
        await ready.wait()
        assert len(armed._pending_input) == 2
        _first, second = armed._pending_input
        assert await hook_host.queued_prompt(armed)
        assert armed._pending_input == [second]
        assert await hook_host.queued_prompt(armed)
        assert not armed._pending_input
        await original(origin)
    monkeypatch.setattr(armed, "_stream_worker", stream)
    parent = armed._stream(origin=EventOrigin(session_id=armed.convo_id, operation_id="parent", source="rpc"))
    await entered.wait()
    async def output(value):
        return value
    armed._start_background("bash", {"command": "one"}, output("One"))
    armed._start_background("bash", {"command": "two"}, output("Two"))
    await asyncio.gather(*(p.worker.wait() for p in work.bridge(armed).producers.values()))
    ready.set()
    await parent.wait()
    await settle_inputs(armed, 2)
    entries = list(work.bridge(armed).inputs.values())
    assert [i.admission["boundary"] for i in entries] == [1, 2]
    assert all(i.worker is parent and i.state == "settled" for i in entries)
    assert len(armed._rewrite_test_requests) == 1


@pytest.mark.asyncio
async def test_close_barrier_retains_late_completion_without_wake(armed):
    gate = asyncio.Event()
    async def output():
        await gate.wait()
        return "Late owned completion"
    armed._start_background("bash", {"command": "fixture"}, output())
    value = work.bridge(armed)
    producer = next(iter(value.producers.values()))
    cutoff = work.begin_close(armed)
    armed._gui_quitting = True
    gate.set()
    proof = await asyncio.wait_for(work.settle_before_shutdown(armed), 5)
    armed._work_test_shutdown = proof
    entry = next(iter(value.inputs.values()))
    assert entry.state == "held" and entry.admission is None
    assert not armed._rewrite_test_requests
    assert cutoff["producer_ids"] == [producer.ref["producer_id"]]
    assert proof["inputs"] == [{"input_id": entry.ref["input_id"], "disposition": "held"}]
    assert producer.worker.is_finished and producer.observer.done()
    assert proof["admission_closed"] and proof["effects_verified"] is False


@pytest.mark.asyncio
async def test_copied_producer_reference_cannot_enqueue_a_completion(armed):
    gate = asyncio.Event()
    async def output():
        await gate.wait()
        return "Owned completion"
    armed._start_background("bash", {"command": "fixture"}, output())
    producer = next(iter(work.bridge(armed).producers.values()))
    before = (armed.convo_dir / work.FILENAME).read_bytes()
    with pytest.raises(ValueError, match="producer"):
        work.enqueue(armed, producer, {"content": "Forged", "text": "Forged", "source": "harness", "tool_profile": "strict"})
    assert (armed.convo_dir / work.FILENAME).read_bytes() == before
    gate.set()
    await settle_inputs(armed, 1)


@pytest.mark.asyncio
async def test_actual_rpc_activation_ack_precedes_first_source_lifecycle_event(app, monkeypatch):
    from litetui import rpc
    from litetui.app import LiteTUI
    wire = []
    monkeypatch.setattr(rpc, "rpc_emit", lambda row: wire.append(copy.deepcopy(row)))
    monkeypatch.setattr(app, "_rpc_emit", lambda row, *, origin=None: LiteTUI._rpc_emit(app, row, origin=origin))
    descriptor = work.capture(app, command(app))
    original = work.activate
    def activate(app, cmd):
        result = original(app, cmd)
        asyncio.get_running_loop().call_soon(work.begin_close, app)
        return result
    monkeypatch.setattr(work, "activate", activate)
    rpc._dispatch(app, command(app, type="gui.work.native_activate", instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    await asyncio.gather(*(w.wait() for w in list(app.workers) if w.group == "gui-management"))
    await asyncio.sleep(0)
    ack = next(i for i, row in enumerate(wire) if row.get("id") == "work-test")
    emitted = next(i for i, row in enumerate(wire) if row.get("type") == "work_closing")
    assert wire[ack]["ok"] is True and ack < emitted


@pytest.mark.parametrize("mode", ["normal", "late_stop", "timeout"])
def test_actual_controlled_process_records_exact_pipe_and_kill_retirement(tmp_path, monkeypatch, mode):
    import os
    import subprocess
    import sys

    from litetui import tasks, ttyguard
    from litetui.plugins import core_tools
    actual_popen = subprocess.Popen
    task = tasks.new_task("bash", {"command": "controlled Python child"}, "fixture")
    task._pending_process_handoff = True
    def spawn(argv, **kwargs):
        kwargs.pop("kill_on_close", None)
        process = actual_popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
        if mode == "late_stop":
            assert tasks.request_kill(task) == (True, None)
        return process
    def kill(_pid, process):
        process.kill()
        process.wait(timeout=5)
        return True
    monkeypatch.setattr(ttyguard, "popen", spawn)
    monkeypatch.setattr(ttyguard, "kill_tree", kill)
    monkeypatch.setattr(core_tools.harness, "tool_process_env", lambda _agent: dict(os.environ))
    program = "print('controlled completed')" if mode == "normal" else "import time; time.sleep(30)"
    token = tasks.CURRENT.set(task)
    try:
        result = core_tools._run_shell([sys.executable, "-I", "-c", program], shell=False, timeout=0.1 if mode == "timeout" else 5)
    finally:
        tasks.CURRENT.reset(token)
    assert task.proc is not None and task.proc.poll() is not None
    proof = task.proc._litetui_retirement
    assert proof["process_exited"] and proof["pipes_settled"] and tasks.process_retired(task.proc)
    assert proof["kill_requested"] is (mode != "normal")
    assert proof["kill_confirmed"] is (None if mode == "normal" else True)
    assert result


@pytest.mark.asyncio
async def test_real_background_thread_retirement_is_joined_after_textual_cancel(armed, monkeypatch):
    import threading

    from litetui import tasks, tool_policy, tool_schemas
    started, release, retired = threading.Event(), threading.Event(), threading.Event()
    schema = {"type": "function", "function": {"name": "work_probe", "description": "controlled", "parameters": {
        "type": "object", "properties": {"background": {"type": "boolean"}}}}}
    def callback(_args):
        started.set()
        assert release.wait(5)
        retired.set()
        return "Retired actual thread"
    armed.plugins.add_tool("test", schema, callback, policy=tool_policy.READ_POLICY)
    monkeypatch.setattr(tool_schemas, "load", lambda name: schema)
    monkeypatch.setattr(tasks, "backgroundable", tasks.backgroundable.__wrapped__)
    armed.tools_enabled = True
    await armed._execute_tool("work_probe", {"background": True}, hooks_enabled=False)
    value = work.bridge(armed)
    record = next(iter(value.producers.values()))
    await asyncio.wait_for(asyncio.to_thread(started.wait), 2)
    record.worker.cancel()
    await asyncio.sleep(0)
    assert not record.observer.done() and not retired.is_set()
    release.set()
    await asyncio.wait_for(record.observer, 5)
    await settle_inputs(armed, 1)
    assert retired.is_set() and record.call.retired.is_set() and record.call.future.done()
    assert record.phase == "retired"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unknown", "bool_revision", "duplicate", "profile", "epoch", "reason"])
async def test_recovery_rejects_malformed_ledger_without_replay(app, monkeypatch, fault):
    descriptor = work.capture(app, command(app))
    work.activate(app, command(app, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    monkeypatch.setattr(app, "_user_bubble", lambda *_a, **_k: SimpleNamespace())
    gate = asyncio.Event()
    async def output():
        await gate.wait()
        return "Preserved completion"
    app._start_background("bash", {"command": "fixture"}, output())
    work.begin_close(app)
    gate.set()
    await work.settle_before_shutdown(app)
    path = app.convo_dir / work.FILENAME
    document = json.loads(path.read_text(encoding="utf-8"))
    if fault == "unknown":
        document["unexpected"] = True
    elif fault == "bool_revision":
        document["revision"] = True
    elif fault == "duplicate":
        document["inputs"].append(copy.deepcopy(document["inputs"][0]))
    elif fault == "profile":
        document["inputs"][0]["input"]["requested_profile"] = "owner"
    elif fault == "epoch":
        document["inputs"][0]["epoch"]["identity"]["agent_id"] = work.ident()
    else:
        document["inputs"][0]["reason"] = []
    path.write_bytes(work.encoded(document))
    app._native_work_bridge = None
    before = path.read_bytes()
    with pytest.raises(ValueError):
        work.capture(app, command(app))
    assert path.read_bytes() == before and not app._rewrite_test_requests


@pytest.mark.asyncio
async def test_quiet_identity_retirement_waits_without_cancel_and_recovers_original_epoch(armed):
    gate = asyncio.Event()
    async def output():
        await gate.wait()
        return "Historical\n\tUnicode \u00e9\U0001f9ea\u2028\b preserved"
    armed._start_background("bash", {"command": "fixture"}, output())
    old = work.bridge(armed)
    record = next(iter(old.producers.values()))
    pending = asyncio.create_task(work.before_identity_transition(armed))
    await asyncio.sleep(0)
    assert old.closing and not record.worker.is_cancelled and record.task.state == "running"
    gate.set()
    proof = await pending
    receipt = work.after_identity_transition(armed, proof)
    assert receipt["work_recapture_required"] and work.bridge(armed) is None
    descriptor = work.capture(armed, command(armed))
    assert len(descriptor["recovered"]) == 1
    row = descriptor["recovered"][0]
    assert row["state"] == "held" and row["epoch"]["instance"] == old.instance
    assert row["epoch"]["identity"] == old.identity and row["epoch"]["provider"] == old.provider
    work.activate(armed, command(armed, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    await asyncio.sleep(0)
    assert not armed._rewrite_test_requests and not armed._pending_input
    assert not work.bridge(armed).producers


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["output", "task"])
async def test_optional_persistence_failure_reports_evidence_and_still_delivers(armed, monkeypatch, fault):
    from litetui import tasks
    if fault == "output":
        original = Path.write_text
        def write(path, *args, **kwargs):
            if path.suffix == ".log":
                raise OSError("Controlled optional output failure")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "write_text", write)
    else:
        monkeypatch.setattr(tasks, "save_by_convo", lambda *_a, **_k: (_ for _ in ()).throw(OSError("Controlled task store failure")))
    async def output():
        return "Completion survives optional failure"
    armed._start_background("bash", {"command": "fixture"}, output())
    await settle_inputs(armed, 1)
    producer = next(iter(work.bridge(armed).producers.values()))
    assert producer.persistence[fault]["status"] == "failed"
    assert producer.persistence[fault]["reason"]
    assert len(armed._rewrite_test_requests) == 1


@pytest.mark.asyncio
async def test_captured_scheduler_same_session_waits_for_real_stamped_enqueue(armed):
    from litetui import gui_rpc, paths, scheduler
    job = scheduler.Job(prompt="Exact scheduled payload", schedule="* * * * *", tool_profile="strict")
    armed.jobs[:] = [job]
    scheduler.save(armed.jobs, paths.data_root())
    result = await gui_rpc.async_dispatch(armed, {"type": "gui.jobs.run", "id": "run-job", "job_id": job.id})
    assert result["delivered"] is True and result["run_count"] == 1 and result["invocation_id"]
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.producer.ref["kind"] == "cron"
    assert entry.ref["source"] == entry.admission["origin"]["source"] == "scheduled"
    assert entry.payload["content"] == job.prompt
    assert job.run_count == 1


@pytest.mark.asyncio
async def test_session_changing_job_returns_staged_receipt_then_exact_activation_continues_once(armed, monkeypatch):
    from litetui import hook_host, paths, scheduler
    # Controlled lifecycle records the actual new identity without starting an OS hook.
    monkeypatch.setattr(hook_host, "enter_conversation", lambda app, _event: setattr(app, "_hook_conversation_id", app.convo_id))
    job = scheduler.Job(prompt="New-session scheduled payload", schedule="* * * * *", tool_profile="strict", new_conversation=True)
    armed.jobs[:] = [job]
    scheduler.save(armed.jobs, paths.data_root())
    old_session = armed.convo_id
    worker = armed._fire_job(job, manual=True)
    result = await asyncio.wait_for(worker.wait(), 5)
    assert result["delivered"] is False and result["awaiting_work_capture"] is True
    assert armed.convo_id != old_session and not armed._rewrite_test_requests
    descriptor = work.capture(armed, command(armed))
    assert len(descriptor["deferred_jobs"]) == 1
    assert descriptor["deferred_jobs"][0]["invocation_id"] == result["invocation_id"]
    deferred = armed._native_work_deferred
    work.activate(armed, command(armed, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    await asyncio.sleep(0)
    await asyncio.wait_for(deferred.worker.wait(), 5)
    await settle_inputs(armed, 1)
    assert len(armed._rewrite_test_requests) == 1 and job.run_count == 1
    assert work.deferred_evidence(armed) == []
    with pytest.raises(ValueError):
        work.activate(armed, command(armed, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    assert len(armed._rewrite_test_requests) == 1


@pytest.mark.asyncio
async def test_next_completion_can_admit_before_prior_observer_receipt(armed, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    original = armed._stream_worker
    runs = 0
    async def stream(origin):
        nonlocal runs
        runs += 1
        if runs == 1:
            started.set()
            await release.wait()
        await original(origin)
        armed._rpc_emit({"type": "turn_end", "stopReason": "stop"}, origin=origin)
    monkeypatch.setattr(armed, "_stream_worker", stream)
    async def output(value):
        return value
    armed._start_background("bash", {"command": "first"}, output("First completion"))
    await started.wait()
    armed._start_background("bash", {"command": "second"}, output("Second completion"))
    value = work.bridge(armed)
    await asyncio.gather(*(p.worker.wait() for p in value.producers.values()))
    first, second = value.inputs.values()
    assert first.admission is not None and second.state == "queued"
    release.set()
    await first.worker.wait()
    armed._flush_pending_input()
    assert second.admission is not None
    events = [row for row, _origin in armed._work_test_events]
    second_index = next(i for i, row in enumerate(events) if row.get("type") == "work_admission" and row["input"] == second.ref)
    assert not any(row.get("type") == "work_settled" and row["input"] == first.ref for row in events[:second_index])
    await settle_inputs(armed, 2)
    assert first.worker.is_finished and second.worker.is_finished
    assert first.admission["operation_id"] != second.admission["operation_id"]


@pytest.mark.asyncio
async def test_quit_cancels_staged_invocation_before_activation_without_dispatch(armed, monkeypatch):
    from litetui import hook_host, paths, scheduler
    monkeypatch.setattr(hook_host, "enter_conversation", lambda app, _event: setattr(app, "_hook_conversation_id", app.convo_id))
    job = scheduler.Job(prompt="Never dispatch after Quit", schedule="* * * * *", tool_profile="strict", new_conversation=True)
    armed.jobs[:] = [job]
    scheduler.save(armed.jobs, paths.data_root())
    result = await armed._fire_job(job, manual=True).wait()
    assert result["delivered"] is False
    deferred = armed._native_work_deferred
    descriptor = work.capture(armed, command(armed))
    work.activate(armed, command(armed, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    work.begin_close(armed)
    armed._gui_quitting = True
    await asyncio.sleep(0)
    proof = await work.settle_before_shutdown(armed)
    assert deferred.worker is None and deferred.document["state"] == "cancelled"
    assert proof["producer_ids"] == [] and proof["inputs"] == []
    assert not armed._rewrite_test_requests and not work.deferred_evidence(armed)


@pytest.mark.asyncio
async def test_restart_capture_exposes_staged_job_but_cannot_resume_by_persisted_id(armed, monkeypatch):
    from litetui import hook_host, paths, scheduler
    monkeypatch.setattr(hook_host, "enter_conversation", lambda app, _event: setattr(app, "_hook_conversation_id", app.convo_id))
    job = scheduler.Job(prompt="Held after restart", schedule="* * * * *", tool_profile="strict", new_conversation=True)
    armed.jobs[:] = [job]
    scheduler.save(armed.jobs, paths.data_root())
    staged = await armed._fire_job(job, manual=True).wait()
    armed._native_work_deferred = None  # Simulate loss of runtime-only ownership.
    descriptor = work.capture(armed, command(armed))
    assert descriptor["deferred_jobs"][0]["invocation_id"] == staged["invocation_id"]
    work.activate(armed, command(armed, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    await asyncio.sleep(0)
    assert not armed._rewrite_test_requests and not work.bridge(armed).producers
    assert work.deferred_evidence(armed)[0]["state"] == "held"


@pytest.mark.asyncio
async def test_arbitrary_canonical_output_preserves_unicode_controls_nul_and_digest(armed):
    output_text = "Exact Unicode \u00e9\U0001f9ea\u2028\n\t\b\0 preserved"
    async def output():
        return output_text
    armed._start_background("bash", {"command": "fixture"}, output())
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    admitted = next(row for row, _origin in armed._work_test_events if row["type"] == "work_admission")
    assert output_text in entry.payload["content"]
    assert admitted["payload"] == entry.payload
    assert work.digest(work.encoded(admitted["payload"])) == entry.ref["payload_sha256"]
    assert entry.producer.persistence["output"]["status"] == "verified"


@pytest.mark.asyncio
async def test_actual_raw_and_gui_losing_local_prompts_refuse_before_queue_or_append(armed, monkeypatch):
    from litetui import rpc
    entered, release = asyncio.Event(), asyncio.Event()
    original = armed._stream_worker
    async def stream(origin):
        entered.set()
        await release.wait()
        await original(origin)
    monkeypatch.setattr(armed, "_stream_worker", stream)
    async def output():
        return "Autonomous turn wins the race"
    armed._start_background("bash", {"command": "fixture"}, output())
    await entered.wait()
    history = copy.deepcopy(armed.conversation)
    wire = []
    monkeypatch.setattr(rpc, "rpc_emit", lambda row: wire.append(copy.deepcopy(row)))
    rpc._dispatch(armed, {"type": "prompt", "id": "local-raw-race", "message": "Keep my local draft"})
    rpc._dispatch(armed, {"type": "gui.prompt.submit", "id": "local-gui-race", "message": "Keep my GUI draft"})
    await asyncio.gather(*(w.wait() for w in list(armed.workers) if w.group == "gui-management"))
    replies = [row for row in wire if row.get("id") in {"local-raw-race", "local-gui-race"}]
    assert len(replies) == 2 and all(row["ok"] is False for row in replies)
    assert armed.conversation == history and not armed._pending_input
    release.set()
    await settle_inputs(armed, 1)


@pytest.mark.asyncio
async def test_quit_joins_pending_admission_hook_and_retains_unappended_input(armed, monkeypatch):
    from litetui.lifecycle_hooks import HookResult
    started = asyncio.Event()
    monkeypatch.setattr(hook_host, "snapshot", lambda _app: SimpleNamespace(hooks=(object(),), error=None))
    async def dispatch(_app, event, _data, **_kwargs):
        assert event == "prompt_before"
        started.set()
        await asyncio.Event().wait()
        return HookResult(True)
    monkeypatch.setattr(hook_host, "dispatch", dispatch)
    async def output():
        return "Held while prompt hook waits"
    before = copy.deepcopy(armed.conversation)
    armed._start_background("bash", {"command": "fixture"}, output())
    await started.wait()
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.state == "claiming" and entry.admission_worker is not None
    armed._gui_quitting = True
    proof = await asyncio.wait_for(work.settle_before_shutdown(armed), 5)
    assert entry.state == "uncertain" and entry.admission is None
    assert entry.admission_worker.is_finished and armed.conversation == before
    assert proof["admission_ids"] == [] and proof["inputs"] == [{"input_id": entry.ref["input_id"], "disposition": "uncertain"}]
    armed._work_test_shutdown = proof


@pytest.mark.asyncio
async def test_action_stop_retains_late_completion_without_starting_continuation(armed, monkeypatch):
    from test_rust_conversation_rewrite import apply, reviewed, settle

    from litetui import conversation_action as action
    original = armed._stream_worker
    calls = []
    async def stream(origin):
        calls.append(origin)
        action.segment(armed, origin)
        async def output():
            await asyncio.sleep(0)
            return "Completion after action Stop"
        armed._start_background("bash", {"command": "fixture"}, output())
        action.stop(armed)
        await original(origin)
    monkeypatch.setattr(armed, "_stream_worker", stream)
    review = reviewed(armed)
    assert (await apply(armed, review))["turn"] == "accepted"
    await asyncio.wait_for(settle(armed, review), 5)
    value = work.bridge(armed)
    entry = next(iter(value.inputs.values()))
    assert entry.state == "held" and entry.admission is None and len(calls) == 1
    assert armed._pending_input == [entry.item]
    await entry.producer.observer


@pytest.mark.asyncio
async def test_post_launch_binding_failure_retains_exact_worker_and_settles_uncertainty(armed, monkeypatch):
    original = work.bind_stream
    def broken(app, worker, origin):
        raise ValueError("Controlled failure after Textual worker launch")
    monkeypatch.setattr(work, "bind_stream", broken)
    async def output():
        return "Admitted but binding interrupted"
    armed._start_background("bash", {"command": "fixture"}, output())
    value = work.bridge(armed)
    producer = next(iter(value.producers.values()))
    from textual.worker import WorkerFailed
    with pytest.raises(WorkerFailed):
        await producer.worker.wait()
    entry = next(iter(value.inputs.values()))
    assert entry.admission is not None and entry.state == "uncertain"
    assert entry.worker is not None and entry.observer is not None
    result = await asyncio.wait_for(work.settled(armed, command(armed, instance=value.instance, admission_id=entry.admission["admission_id"])), 5)
    assert entry.worker.is_finished and result["outcome"] == "cancelled"
    assert entry.receipt == result and not armed._rewrite_test_requests
    assert value.error is not None and not armed._pending_input
    monkeypatch.setattr(work, "bind_stream", original)


@pytest.mark.asyncio
async def test_goal_direct_resume_creates_owned_source_admission(armed):
    from litetui import goal_loop, seat_authority
    state = goal_loop.GoalState("Finish controlled goal", tool_profile="strict", started_by="typed")
    goal_loop.save_goal(armed.convo_dir, state)
    goal_loop._deliver_goal_turn(armed, state, "Resume exact evidence")
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.producer.ref["kind"] == "goal"
    assert entry.producer.ref["object_id"] == state.id
    assert entry.ref["source"] == seat_authority.goal_source(state)
    assert entry.admission["mode"] == "new_turn"
    assert entry.admission["origin"]["source"] == entry.ref["source"]
    assert entry.ref["requested_profile"] == "strict"
    assert len(armed._rewrite_test_requests) == 1


@pytest.mark.asyncio
async def test_goal_busy_resume_uses_exact_safe_boundary(armed, monkeypatch):
    from litetui import goal_loop, seat_authority
    entered, ready = asyncio.Event(), asyncio.Event()
    original = armed._stream_worker
    async def stream(origin):
        entered.set()
        await ready.wait()
        assert await hook_host.queued_prompt(armed)
        await original(origin)
    monkeypatch.setattr(armed, "_stream_worker", stream)
    parent = armed._stream(origin=EventOrigin(session_id=armed.convo_id, operation_id="goal-parent", source="rpc"))
    await entered.wait()
    state = goal_loop.GoalState("Queued goal", tool_profile="strict", started_by="typed")
    goal_loop._deliver_goal_turn(armed, state, "Continue from evidence")
    value = work.bridge(armed)
    producer = next(iter(value.producers.values()))
    await producer.worker.wait()
    assert len(armed._pending_input) == 1
    ready.set()
    await parent.wait()
    await settle_inputs(armed, 1)
    entry = next(iter(value.inputs.values()))
    assert entry.admission["mode"] == "in_turn" and entry.worker is parent
    assert entry.ref["source"] == seat_authority.goal_source(state)
    assert len(armed._rewrite_test_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["not_met", "met"])
async def test_goal_evaluator_owns_callback_and_canonical_continuation(armed, monkeypatch, verdict):
    from litetui import goal_loop, model_transport
    state = goal_loop.GoalState("Goal evidence", tool_profile="strict", started_by="typed")
    goal_loop.save_goal(armed.convo_dir, state)
    armed._append({"role": "user", "content": f"[goal {state.id}]\nGoal evidence"})
    runtime = goal_loop.GoalRuntime(armed)
    original = armed._stream_worker
    async def create(**_kwargs):
        assert work.capturing_completion(armed)
        producer = next(iter(work.bridge(armed).producers.values()))
        assert producer.ref["kind"] == "goal" and producer.phase == "running"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "verdict": verdict, "reason": "Goal evidence", "evidence": ["Goal evidence"],
            "missing_evidence": [], "next_instruction": "Continue controlled goal"})))])
    async def ready():
        return None
    monkeypatch.setattr(armed, "_ensure_chat_ready", ready)
    monkeypatch.setattr(model_transport, "for_app", lambda _app: SimpleNamespace(create=create))
    async def stream(origin):
        await original(origin)
        await runtime.on_plain_answer()
    monkeypatch.setattr(armed, "_stream_worker", stream)
    parent = armed._stream(origin=EventOrigin(session_id=armed.convo_id, operation_id="goal-evaluation", source="rpc"))
    await parent.wait()
    value = work.bridge(armed)
    producer = next(iter(value.producers.values()))
    await producer.observer
    saved = goal_loop.load_goal(armed.convo_dir)
    assert saved.evaluated_turns == 1
    assert producer.worker is parent and producer.phase == "retired"
    if verdict == "met":
        assert saved.status == "met" and not value.inputs and not armed._pending_input
    else:
        assert len(value.inputs) == 1 and len(armed._pending_input) == 1
        entry = next(iter(value.inputs.values()))
        assert armed._pending_input == [entry.item] and entry.state == "queued"
        assert entry.item["goal_continuation"] and "Continue controlled goal" in entry.payload["content"]
        monkeypatch.setattr(armed, "_stream_worker", original)
        armed._flush_pending_input()
        await settle_inputs(armed, 1)
        assert entry.admission["mode"] == "new_turn" and len(armed._rewrite_test_requests) == 2


@pytest.mark.asyncio
async def test_action_segment_prestart_binding_failure_retains_cancelled_exact_admission(armed, monkeypatch):
    from test_rust_conversation_rewrite import apply, reviewed, settle

    from litetui import conversation_action as action
    original_stream, original_bind = armed._stream_worker, work.bind_stream
    calls = []
    async def stream(origin):
        calls.append(origin)
        action.segment(armed, origin)
        async def output():
            await asyncio.sleep(0)
            return "Continuation accepted before controlled bind failure"
        armed._start_background("bash", {"command": "fixture"}, output())
        await original_stream(origin)
    def bind(app, worker, origin):
        if work._ADMISSION.get() is not None:
            raise ValueError("Controlled action continuation binding failure")
        return original_bind(app, worker, origin)
    monkeypatch.setattr(armed, "_stream_worker", stream)
    monkeypatch.setattr(work, "bind_stream", bind)
    review = reviewed(armed)
    assert (await apply(armed, review))["turn"] == "accepted"
    with pytest.raises(ValueError, match="Controlled action continuation binding failure"):
        await asyncio.wait_for(settle(armed, review), 5)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.admission["mode"] == "action_segment"
    assert entry.admission["parent_execution_id"] == calls[0].execution_id
    result = await asyncio.wait_for(work.settled(armed, command(armed,
        instance=work.bridge(armed).instance, admission_id=entry.admission["admission_id"])), 5)
    assert result["outcome"] == "cancelled" and entry.worker.is_finished
    assert len(calls) == 1 and len(action._attempt(armed).segments) == 1
    assert armed._conversation_rewrite_error == action.CLOSE_REQUIRED
    assert work.bridge(armed).error is not None
    assert not armed._pending_input


@pytest.mark.asyncio
async def test_quit_cancels_unowned_post_stream_aftercare_without_error(armed, monkeypatch):
    from litetui import conversation_action as action
    scheduled = []
    monkeypatch.setattr(armed, "call_after_refresh", lambda *args: scheduled.append(args))
    armed._conversation_rewrite_closing = True
    assert action.after_refresh(armed, armed._resync_ctx_if_stale) is False
    assert action.after_refresh(armed, armed._maybe_autocompact) is False
    assert scheduled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["inbox", "goal", "cron", "child_result"])
async def test_quit_cancels_and_joins_exact_non_task_producer(armed, kind):
    gate, started = asyncio.Event(), asyncio.Event()
    record = work.register_producer(armed, kind, "controlled-owner", owner_instance="controlled-runtime")
    async def callback():
        started.set()
        await gate.wait()
    worker = armed.run_worker(work.producer_runner(armed, record, callback()),
                              group="work-controlled", exit_on_error=False)
    work.bind_producer_worker(armed, record, worker)
    await started.wait()
    watchdog = asyncio.get_running_loop().call_later(1, gate.set)
    try:
        proof = await asyncio.wait_for(work.settle_before_shutdown(armed), 5)
    finally:
        watchdog.cancel()
    assert worker.is_cancelled and worker.is_finished and record.phase == "retired"
    assert proof["producer_ids"] == [record.ref["producer_id"]]
    assert proof["callback_settled"] and proof["worker_settled"]
    armed._work_test_shutdown = proof


@pytest.mark.asyncio
async def test_quit_after_admission_before_worker_body_reports_cancelled_uncertainty(armed, monkeypatch):
    original_emit = armed._rpc_emit
    def emit(event, *, origin=None):
        original_emit(event, origin=origin)
        if event.get("type") == "work_admission":
            work.begin_close(armed)
    monkeypatch.setattr(armed, "_rpc_emit", emit)
    async def output():
        return "Accepted immediately before the close barrier"
    armed._start_background("bash", {"command": "fixture"}, output())
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert not armed._rewrite_test_requests
    assert entry.receipt["outcome"] == "cancelled" and work.bridge(armed).error is not None
    phases = [row["phase"] for row, _origin in armed._work_test_events if row["type"] == "work_input"]
    assert phases == ["queued", "claiming", "uncertain"]
    proof = await work.settle_before_shutdown(armed)
    assert proof["inputs"] == [{"input_id": entry.ref["input_id"], "disposition": "settled"}]
    armed._work_test_shutdown = proof


@pytest.mark.asyncio
async def test_explicit_native_launch_holds_execution_until_matching_activation(app, monkeypatch):
    from litetui import goal_loop
    app._launch_options.work_lifecycle_version = 1
    state = goal_loop.GoalState("Held startup goal")
    goal_loop.save_goal(app.convo_dir, state)
    app._append({"role": "user", "content": f"[goal {state.id}] Held startup goal"})
    before = app.convo_path.read_bytes(), (app.convo_dir / goal_loop.GOAL_FILENAME).read_bytes()
    assert work.activation_pending(app) and work.prompt_refusal(app)
    with pytest.raises(ValueError, match="activation"):
        work.preflight_task(app, "bash", {"command": "must remain unstarted"})
    await goal_loop.GoalRuntime(app).on_plain_answer()
    descriptor = work.capture(app, command(app))
    assert work.activation_pending(app) and not work.enabled(app)
    assert before == (app.convo_path.read_bytes(), (app.convo_dir / goal_loop.GOAL_FILENAME).read_bytes())
    work.activate(app, command(app, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    assert not work.activation_pending(app) and work.prompt_refusal(app) is None
    assert not app._rewrite_test_requests


@pytest.mark.asyncio
async def test_capture_preserves_existing_restored_queue_as_inert_evidence(app):
    item = {"content": "Existing saved native input", "source": "scheduled",
            "tool_profile": "strict", "_restored_input_held": True}
    app._pending_input.append(item)
    before = app.convo_path.read_bytes()
    descriptor = work.capture(app, command(app))
    work.activate(app, command(app, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    assert app._pending_input == [item] and app._pending_input[0] is item
    assert app.convo_path.read_bytes() == before and not app._rewrite_test_requests
    assert not work.bridge(app).inputs and not work.bridge(app).producers
