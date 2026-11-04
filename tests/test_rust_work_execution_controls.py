"""Actual owned explicit Work launches; no provider, device or desktop startup."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
import pytest_asyncio
from test_rust_background_input import app as _app
from test_rust_background_input import command, settle_inputs
from test_rust_background_input import switch_app as _switch_app
from test_rust_scheduled_publication import published_rpc

from litetui import background_input as work
from litetui import goal_loop, native_work, paths, scheduler
from litetui import native_work_execution as execution

app, switch_app = _app, _switch_app
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def armed(app, monkeypatch):
    from litetui.app import LiteTUI
    app._launch_options.work_lifecycle_version = 1
    events = []
    def emit(event, *, origin=None):
        events.append(copy.deepcopy(event))
        LiteTUI._rpc_emit(app, event, origin=origin)
    monkeypatch.setattr(app, "_rpc_emit", emit)
    monkeypatch.setattr(app, "_user_bubble", lambda *_args, **_kwargs: SimpleNamespace(border_title="You"))
    descriptor = work.capture(app, command(app, type="gui.work.native_capture", execution_version=1))
    active = await published_rpc(app, monkeypatch, command(app, type="gui.work.native_activate", instance=descriptor["instance"],
        activation_token=descriptor["activation_token"], execution_version=1))
    app._execution_fixture = {"capture": descriptor, "activation": active, "events": events}
    return app


def request(app, operation, **fields):
    return {"type": execution.PREFIX + operation, "id": "execution-" + operation, **execution._base(work.bridge(app)), **fields}


def review(app, action="job_run", *, kind="cron", objective="Literal reviewed goal"):
    if action == "job_run":
        job = scheduler.Job(prompt="Reviewed explicit job", schedule="0 0 1 1 *", enabled=False, tool_profile="strict")
        if kind == "loop":
            job = scheduler.Job.loop(prompt="Reviewed explicit loop", interval_minutes=15, owner_convo_id=app.convo_id, tool_profile="strict")
            job.enabled = False
        app.jobs[:] = [job]
        scheduler.save(app.jobs, paths.data_root())
    if action == "goal_resume":
        goal_loop.save_goal(app.convo_dir, goal_loop.GoalState("Retained goal", status="paused", evaluated_turns=7,
            last_reason="Keep evidence", missing_evidence=["proof"], tool_profile="strict", started_by="typed"))
        path = app.convo_dir / goal_loop.GOAL_FILENAME
        raw = work.strict_json(path.read_bytes())
        raw["future_extension"] = {"keep": [False, 0, 0.0]}
        path.write_bytes(work.encoded(raw))
    snapshot = native_work.snapshot(app, command(app, type="gui.work.native_snapshot"))
    capture = app._native_work_inspection
    section = "jobs" if kind == "cron" else "loops"
    if action == "job_run":
        selected = capture.rows[section][0]
        target, revision = selected["row"]["id"], selected["revision"]
    else:
        section = "goal"
        goal = snapshot["goal"]
        target, revision = None if goal["value"] is None else goal["value"]["id"], goal["revision"]
    cmd = request(app, "validate", inspection_instance=capture.instance, snapshot=snapshot["snapshot"], action=action,
        section=section, target_id=target, target_revision=revision, draft={"objective": objective} if action == "goal_start" else {})
    result = execution.validate(app, cmd)
    app._execution_fixture.update(snapshot=snapshot, selected=copy.deepcopy(capture.rows), validate_command=cmd, review=result)
    return result


def read_command(app, result, scope="launch"):
    return request(app, "readback", action_id=result["action_id"], review_token=result["review_token"],
        apply_operation_id=result["apply_operation_id"], scope=scope, stop_id=result.get("stop_id"),
        result_operation_id=result["apply_operation_id"] if scope == "launch" else result["stop_operation_id"],
        result_sha256=work.digest(work.encoded(result)))


@pytest.mark.parametrize("action,kind", [("job_run", "cron"), ("job_run", "loop"), ("goal_start", "cron"), ("goal_resume", "cron")])
async def test_actual_explicit_launch_publishes_once_and_readbacks_actual_work(armed, monkeypatch, action, kind):
    r = review(armed, action, kind=kind)
    assert not armed._rewrite_test_requests and not work.bridge(armed).producers
    apply_cmd = {"type": execution.PREFIX + "apply", "id": "apply-execution", "review": r}
    result = await published_rpc(armed, monkeypatch, apply_cmd)
    assert result["status"] == "published", result
    await settle_inputs(armed, 1)
    read_cmd = read_command(armed, result)
    read = await published_rpc(armed, monkeypatch, read_cmd)
    assert read["verified"] and read["document"] == result["document"]
    assert execution.mutation_hold(armed) is None
    document = execution.validate_document(result["document"].encode())
    assert document["producer"]["object_id"] == r["target_id"]
    assert len(armed._rewrite_test_requests) == 1
    assert len(work.bridge(armed).inputs) == 1
    if action == "goal_resume":
        before, after = execution._decode_raw(document["before"]), execution._decode_raw(document["after"])
        assert before["id"] == after["id"] and after["future_extension"] == before["future_extension"]
        assert after["evaluated_turns"] == 7 and after["missing_evidence"] == ["proof"]
    if action == "job_run":
        assert not armed.jobs[0].enabled and armed.jobs[0].run_count == 1
    with pytest.raises(ValueError):
        await execution.apply(armed, apply_cmd)
    armed._execution_fixture.update(apply_command=apply_cmd, result=result, readback_command=read_cmd, readback=read,
        ledger=copy.deepcopy(work.bridge(armed).document()), document=document)


    stop_cmd = request(armed, "stop", action_id=r["action_id"], review_token=r["review_token"], apply_operation_id=result["apply_operation_id"])
    stopped = await published_rpc(armed, monkeypatch, stop_cmd)
    stop_read_cmd = read_command(armed, stopped, "stop")
    stop_read = await published_rpc(armed, monkeypatch, stop_read_cmd)
    work.begin_close(armed)
    shutdown = await execution.settle_before_shutdown(armed)
    work_shutdown = await work.settle_before_shutdown(armed)
    armed._execution_fixture.update(stop_command=stop_cmd, stop_result=stopped, stop_readback_command=stop_read_cmd,
        stop_readback=stop_read, shutdown=shutdown, work_shutdown=work_shutdown)
    export_case(armed, action + "-" + kind)


def export_case(app, name):
    import os
    from pathlib import Path
    target = os.environ.get("LITETUI_EXECUTION_FIXTURES")
    if not target:
        return
    path = Path(target)
    cases = {} if not path.exists() else {row["name"]: row for row in work.strict_json(path.read_bytes())["cases"]}
    cases[name] = {"name": name, **app._execution_fixture}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(work.encoded({"version": 1, "cases": list(cases.values())}))


@pytest.mark.parametrize("objective", ["pause", "resume", "clear", "status", "show"])
async def test_goal_objective_is_literal_text_not_a_command(armed, monkeypatch, objective):
    r = review(armed, "goal_start", objective=objective)
    result = await published_rpc(armed, monkeypatch, {"type": execution.PREFIX + "apply", "id": "literal", "review": r})
    assert result["status"] == "published", result
    assert goal_loop.load_goal(armed.convo_dir).objective == objective
    await settle_inputs(armed, 1)


@pytest.mark.parametrize("action", ["job_run", "goal_start", "goal_resume"])
async def test_changed_selected_raw_refuses_before_any_effect(armed, monkeypatch, action):
    r = review(armed, action)
    path = scheduler.jobs_path(paths.data_root()) if action == "job_run" else armed.convo_dir / goal_loop.GOAL_FILENAME
    if action == "job_run":
        rows = work.strict_json(path.read_bytes())
        rows[0]["future_extension"] = False
        path.write_bytes(work.encoded(rows))
    elif action == "goal_start":
        goal_loop.save_goal(armed.convo_dir, goal_loop.GoalState("Competing goal"))
    else:
        row = work.strict_json(path.read_bytes())
        row["future_extension"]["keep"][0] = 0
        path.write_bytes(work.encoded(row))
    before = path.read_bytes()
    result = await published_rpc(armed, monkeypatch, {"type": execution.PREFIX + "apply", "id": "stale", "review": r})
    assert result["status"] == "refused" and not result["effect_attempted"], result
    assert path.read_bytes() == before and not armed._rewrite_test_requests
    assert not work.bridge(armed).producers
    read = await published_rpc(armed, monkeypatch, read_command(armed, result))
    assert read["document"] is None and execution.mutation_hold(armed) is None


@pytest.mark.parametrize("phase", ["stamp", "receipt"])
async def test_attempted_publication_never_rolls_back_or_replays(armed, monkeypatch, phase):
    r = review(armed)
    if phase == "stamp":
        save = scheduler.save
        def fail(*args):
            save(*args)
            raise OSError("Controlled save succeeded then failed")
        monkeypatch.setattr(scheduler, "save", fail)
    else:
        def fail(*_args):
            raise OSError("Controlled receipt publication failure")
        monkeypatch.setattr(execution, "_write_receipt", fail)
    result = await published_rpc(armed, monkeypatch, {"type": execution.PREFIX + "apply", "id": "ambiguous", "review": r})
    assert result["status"] == "uncertain" and result["close_required"] and result["effect_attempted"], result
    assert armed.jobs[0].run_count == 1
    assert work.strict_json(scheduler.jobs_path(paths.data_root()).read_bytes())[0]["run_count"] == 1
    assert not armed._rewrite_test_requests
    assert execution.mutation_hold(armed) and work.bridge(armed).error


async def test_receipt_gate_precedes_reentrant_input_event_and_first_wake(armed, monkeypatch):
    r = review(armed)
    original = armed._rpc_emit
    checked = []
    def emit(event, *, origin=None):
        if event["type"] == "work_input" and event["phase"] == "queued":
            a = execution._attempt(armed)
            assert a.entry is not None and a.document is None
            with pytest.raises(ValueError, match="has not released"):
                work._begin(armed, a.entry, False)
            checked.append(True)
        original(event, origin=origin)
    monkeypatch.setattr(armed, "_rpc_emit", emit)
    result = await published_rpc(armed, monkeypatch, {"type": execution.PREFIX + "apply", "id": "gate", "review": r})
    assert result["status"] == "published" and checked == [True], result
    await settle_inputs(armed, 1)


async def test_goal_capacity_failure_precedes_saved_goal_effects(armed, monkeypatch):
    r = review(armed, "goal_start")
    monkeypatch.setitem(work.LIMITS, "inputs", 0)
    result = await published_rpc(armed, monkeypatch, {"type": execution.PREFIX + "apply", "id": "full", "review": r})
    assert result["status"] == "refused" and not result["effect_attempted"], result
    assert not (armed.convo_dir / goal_loop.GOAL_FILENAME).exists()


async def test_early_stop_has_exact_independent_proof_before_producer_exists(armed, monkeypatch):
    from litetui import gui_rpc, rpc
    r = review(armed)
    entered = asyncio.Event()
    original = execution.execute
    async def pause(app, attempt):
        attempt.task = work._task()
        entered.set()
        await asyncio.Event().wait()
        return await original(app, attempt)
    monkeypatch.setattr(execution, "execute", pause)
    cmd = {"type": execution.PREFIX + "apply", "id": "paused-apply", "review": r}
    applying = asyncio.create_task(gui_rpc.async_dispatch(armed, cmd))
    await asyncio.wait_for(entered.wait(), 5)
    stop_cmd = request(armed, "stop", action_id=r["action_id"], review_token=r["review_token"], apply_operation_id=cmd["id"])
    stopped = await published_rpc(armed, monkeypatch, stop_cmd)
    result = await asyncio.wait_for(applying, 5)
    assert stopped["status"] == "stopped" and stopped["disposition"] == "no_effect", stopped
    assert result["status"] == "refused" and not work.bridge(armed).producers
    ticket = execution.prepare_publication(armed, cmd, result)
    rpc._respond(cmd["id"], ok=True, result=result)
    execution.finish_publication(armed, ticket)
    launch = await published_rpc(armed, monkeypatch, read_command(armed, result))
    assert execution.mutation_hold(armed)
    stop_read = await published_rpc(armed, monkeypatch, read_command(armed, stopped, "stop"))
    assert execution.mutation_hold(armed) is None
    assert not armed._rewrite_test_requests and armed.jobs[0].run_count == 0
    work.begin_close(armed)
    shutdown = await execution.settle_before_shutdown(armed)
    work_shutdown = await work.settle_before_shutdown(armed)
    assert shutdown["action_id"] == r["action_id"] and shutdown["disposition"] == "no_effect"
    assert shutdown["owners"]["launch"] == "retired" and shutdown["input_id"] is None
    armed._execution_fixture.update(apply_command=cmd, result=result, readback=launch, stop_command=stop_cmd,
        stop_result=stopped, stop_readback=stop_read, shutdown=shutdown, work_shutdown=work_shutdown, ledger=work.bridge(armed).document())
    export_case(armed, "early-stop")


async def test_legacy_job_default_projection_matches_actual_canonical_save(armed, monkeypatch):
    review(armed)
    path = scheduler.jobs_path(paths.data_root())
    row = work.strict_json(path.read_bytes())[0]
    minimal = {key: row[key] for key in ("id", "prompt", "schedule", "created")}
    minimal.update(tool_profile="strict", future_extension={"keep": [False, 0, 0.0]})
    path.write_bytes(work.encoded([minimal]))
    snapshot = native_work.snapshot(armed, command(armed, type="gui.work.native_snapshot"))
    capture = armed._native_work_inspection
    cmd = {**armed._execution_fixture["validate_command"], "snapshot": snapshot["snapshot"],
           "target_revision": capture.rows["jobs"][0]["revision"]}
    r = execution.validate(armed, cmd)
    assert "enabled" not in execution._decode_raw(r["before"])
    assert execution._decode_raw(r["after_template"])["enabled"] is True
    result = await published_rpc(armed, monkeypatch, {"type": execution.PREFIX + "apply", "id": "minimal", "review": r})
    assert result["status"] == "published", result
    await settle_inputs(armed, 1)
    d = execution.validate_document(result["document"].encode())
    assert execution._decode_raw(d["before"]) == minimal
    assert execution._decode_raw(d["after"])["future_extension"] == minimal["future_extension"]


async def test_real_goal_evaluation_changes_current_state_before_launch_readback(armed, monkeypatch):
    from litetui import model_transport
    original = armed._stream_worker
    async def ready():
        return None
    monkeypatch.setattr(armed, "_ensure_chat_ready", ready)
    calls = []
    async def create(**fields):
        calls.append(fields)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict":"blocked","reason":"Need external evidence","missing_evidence":["proof"]}'))])
    monkeypatch.setattr(model_transport, "for_app", lambda _app: SimpleNamespace(create=create))
    async def stream(origin):
        await original(origin)
        await goal_loop.GoalRuntime(armed).on_plain_answer()
    monkeypatch.setattr(armed, "_stream_worker", stream)
    r = review(armed, "goal_start")
    result = await published_rpc(armed, monkeypatch, {"type": execution.PREFIX + "apply", "id": "evaluated", "review": r})
    assert result["status"] == "published", result
    await settle_inputs(armed, 1)
    read = await published_rpc(armed, monkeypatch, read_command(armed, result))
    assert len(calls) == 1 and calls[0]["purpose"] == "goal-eval"
    assert execution._decode_raw(read["current"]["row"])["status"] == "blocked"
    d = execution.validate_document(read["document"].encode())
    assert execution._decode_raw(d["after"])["status"] == "active"
    assert execution._decode_raw(d["after"])["evaluated_turns"] == 0
    assert execution.mutation_hold(armed) is None


@pytest.mark.parametrize("phase", ["result", "readback", "stop", "stop_readback"])
async def test_failed_exact_response_publication_retains_barrier(armed, monkeypatch, phase):
    from litetui import gui_rpc
    r = review(armed)
    apply_cmd = {"type": execution.PREFIX + "apply", "id": "publication", "review": r}
    if phase == "result":
        result = await gui_rpc.async_dispatch(armed, apply_cmd)
        cmd = apply_cmd
    else:
        result = await published_rpc(armed, monkeypatch, apply_cmd)
        await settle_inputs(armed, 1)
        if phase == "readback":
            cmd = read_command(armed, result)
            result = execution.readback(armed, cmd)
        else:
            await published_rpc(armed, monkeypatch, read_command(armed, result))
            cmd = request(armed, "stop", action_id=r["action_id"], review_token=r["review_token"], apply_operation_id=apply_cmd["id"])
            if phase == "stop":
                result = await execution.stop(armed, cmd)
            else:
                result = await published_rpc(armed, monkeypatch, cmd)
                cmd = read_command(armed, result, "stop")
                result = execution.readback(armed, cmd)
    ticket = execution.prepare_publication(armed, cmd, result)
    assert ticket is not None and execution.mutation_hold(armed)
    with pytest.raises(ValueError):
        execution.prepare_publication(armed, cmd, copy.deepcopy(result))
    result["kind"] = "foreign"
    with pytest.raises(ValueError):
        execution.finish_publication(armed, ticket)
    assert execution.mutation_hold(armed)
    assert work.prompt_refusal(armed)
    assert armed.jobs[0].run_count == 1
    await settle_inputs(armed, 1)


async def test_human_new_keeps_exact_old_execution_proof_until_fresh_capture(armed, monkeypatch):
    from test_rust_conversation_transition import ENTER, LEAVE, reviewed

    from litetui import gui_rpc, hook_host
    monkeypatch.setattr(hook_host, "enter_conversation", ENTER)
    monkeypatch.setattr(hook_host, "leave_conversation", LEAVE)
    armed._hook_conversation_id = armed.convo_id
    r = review(armed)
    result = await published_rpc(armed, monkeypatch, {"type": execution.PREFIX + "apply", "id": "old-execution", "review": r})
    await settle_inputs(armed, 1)
    await published_rpc(armed, monkeypatch, read_command(armed, result))
    old, attempt, receipt_path = work.bridge(armed), execution._attempt(armed), execution._receipt_file(armed)
    raw = receipt_path.read_bytes()
    changed = await gui_rpc.async_dispatch(armed, {**reviewed(armed, "new"), "type": "gui.conversations.native_apply", "id": "human-new"})
    assert changed["status"] == "created", work.encoded(changed).decode()
    proof = changed["work_execution_shutdown"]
    assert proof["action_id"] == r["action_id"] and proof["work_instance"] == old.instance
    assert proof["effects_verified"] is False and execution._attempt(armed) is attempt
    assert receipt_path.read_bytes() == raw
    assert await execution.settle_before_shutdown(armed) == proof
    capture = work.capture(armed, command(armed, type="gui.work.native_capture", execution_version=1))
    assert capture["execution_receipt"]["status"] == "absent"
    assert execution._attempt(armed) is attempt
    await published_rpc(armed, monkeypatch, command(armed, type="gui.work.native_activate", instance=capture["instance"],
        activation_token=capture["activation_token"], execution_version=1))
    assert execution._attempt(armed) is None and receipt_path.read_bytes() == raw


async def test_recovery_is_historical_and_never_reconstructs_live_authority(armed, monkeypatch):
    r = review(armed)
    result = await published_rpc(armed, monkeypatch, {"type": execution.PREFIX + "apply", "id": "historical", "review": r})
    await settle_inputs(armed, 1)
    recovery = execution.recovery(armed)
    assert recovery["status"] == "historical" and recovery["document"] == result["document"]
    armed._native_work_execution = None
    armed._native_work_execution_review = None
    with pytest.raises(ValueError, match="restart never grants authority"):
        await execution.stop(armed, request(armed, "stop", action_id=r["action_id"], review_token=r["review_token"], apply_operation_id="historical"))
    assert not execution.mutation_hold(armed) and len(armed._rewrite_test_requests) == 1
    raw = work.strict_json(result["document"])
    after = execution._decode_raw(raw["after"])
    after["run_count"] += 1
    raw["after"] = execution._raw(after)
    execution._receipt_file(armed).write_bytes(work.encoded(raw))
    assert execution.recovery(armed)["status"] == "unavailable"


async def test_actual_rpc_quit_joins_registered_launch_before_any_producer(armed, monkeypatch):
    from litetui import gui_rpc, rpc
    r = review(armed)
    entered = asyncio.Event()
    async def pause(app, attempt):
        attempt.task = work._task()
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(execution, "execute", pause)
    applying = asyncio.create_task(gui_rpc.async_dispatch(armed, {"type": execution.PREFIX + "apply", "id": "quit-apply", "review": r}))
    await asyncio.wait_for(entered.wait(), 5)
    responses, exits = [], []
    monkeypatch.setattr(rpc, "_respond", lambda ident, **body: responses.append((ident, body)))
    monkeypatch.setattr(armed, "exit", lambda: exits.append(True))
    rpc._dispatch(armed, {"type": "shutdown", "id": "quit"})
    await asyncio.wait_for(armed._conversation_rewrite_exit_task, 5)
    result = await asyncio.wait_for(applying, 5)
    assert result["status"] == "refused" and not work.bridge(armed).producers
    assert len(responses) == 1 and responses[0][0] == "quit" and responses[0][1]["ok"], responses
    reply = responses[0][1]["result"]
    proof = reply["work_execution_shutdown"]
    assert proof["action_id"] == r["action_id"] and proof["apply_operation_id"] == "quit-apply"
    assert proof["disposition"] == "no_effect" and proof["owners"]["launch"] == "retired"
    assert proof["effects_verified"] is False and not proof["launch_readback_published"]
    assert "work_shutdown" in reply and exits == [True]


@pytest.mark.parametrize("fault", ["wrong_version", "extra_key", "raw_bool", "payload", "producer", "foreign_identity"])
async def test_changed_receipt_is_unavailable_and_cannot_be_overwritten(armed, monkeypatch, fault):
    import uuid
    r = review(armed)
    result = await published_rpc(armed, monkeypatch, {"type": execution.PREFIX + "apply", "id": "receipt", "review": r})
    await settle_inputs(armed, 1)
    await published_rpc(armed, monkeypatch, read_command(armed, result))
    raw = work.strict_json(result["document"])
    if fault == "wrong_version":
        raw["version"] = True
    elif fault == "extra_key":
        raw["extra"] = 1
    elif fault == "raw_bool":
        row = execution._decode_raw(raw["after"])
        row["enabled"] = 0
        raw["after"] = execution._raw(row)
    elif fault == "payload":
        raw["payload"]["content"] = raw["payload"]["text"] = "Foreign prompt"
        raw["payload_sha256"] = work.digest(work.encoded(raw["payload"]))
        raw["input"]["payload_sha256"] = raw["payload_sha256"]
    elif fault == "producer":
        raw["producer"]["generation"] += 1
    else:
        raw["identity"]["session_id"] = str(uuid.uuid4())
    path = execution._receipt_file(armed)
    path.write_bytes(work.encoded(raw))
    assert execution.recovery(armed)["status"] == "unavailable"
    before = path.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        review(armed)
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", [True, 1.0, 0, 2, None, "1"])
async def test_execution_capture_requires_exact_version(app, version):
    app._launch_options.work_lifecycle_version = 1
    with pytest.raises(ValueError):
        work.capture(app, command(app, type="gui.work.native_capture", execution_version=version))
    assert not execution.enabled(app)


async def test_negotiation_exact_echo_and_publication_coexist_with_scheduled_mode(app):
    app._launch_options.work_lifecycle_version = 1
    captured = work.capture(app, command(app, type="gui.work.native_capture", execution_version=1))
    cmd = command(app, type="gui.work.native_activate", instance=captured["instance"], activation_token=captured["activation_token"])
    with pytest.raises(ValueError, match="negotiation"):
        work.activate(app, cmd)
    cmd.update(execution_version=1, scheduled_transition_version=1)
    result = work.activate(app, cmd)
    assert result["execution_version"] == result["scheduled_transition_version"] == 1
    assert not execution.enabled(app)
    ticket = work.prepare_publication(app, cmd, result)
    work.finish_publication(app, ticket)
    assert execution.enabled(app) and work.bridge(app).scheduled_transition_version == 1
    with pytest.raises(ValueError):
        work.finish_publication(app, ticket)


@pytest.mark.parametrize("action", ["job_run", "goal_start"])
async def test_actual_pre_turn_hook_has_no_interactive_authority(armed, monkeypatch, action):
    from litetui import hook_host, lifecycle_hooks
    hook = lifecycle_hooks.Hook("execution-hook", ("prompt_before",), "DENIED-NO-PROCESS", mode="gate")
    path = armed.convo_dir / "execution-hooks.json"
    path.write_bytes(work.encoded({"version": 1, "hooks": [hook.document()]}))
    armed.hook_config = lifecycle_hooks.HookConfig(path, armed.convo_dir / "no-project-hooks.json")
    seen, origins = [], []
    original = hook_host.invoke
    async def invoke(app, hook, document, profile, **kwargs):
        origins.append(document["source"])
        return await original(app, hook, document, profile, **kwargs)
    monkeypatch.setattr(hook_host, "invoke", invoke)
    async def authorize(*args, **kwargs):
        seen.append(kwargs["allow_prompt"])
        return ("Denied controlled hook", None)
    monkeypatch.setattr(armed, "_authorize_action", authorize)
    r = review(armed, action)
    result = await published_rpc(armed, monkeypatch, {"type": execution.PREFIX + "apply", "id": "noninteractive", "review": r})
    assert result["status"] == "published", result
    a = execution._attempt(armed)
    await asyncio.wait_for(a.entry.admission_worker.wait(), 5)
    await asyncio.wait_for(a.entry.producer.observer, 5)
    assert seen == [False] and origins == [r["payload"]["source"]]
    assert a.entry.state == "held" and a.entry.admission is None and not armed._rewrite_test_requests
    read = await published_rpc(armed, monkeypatch, read_command(armed, result))
    assert read["lifecycle"]["input_state"] == "held" and execution.mutation_hold(armed)
    stopped = await published_rpc(armed, monkeypatch, request(armed, "stop", action_id=r["action_id"],
        review_token=r["review_token"], apply_operation_id="noninteractive"))
    assert stopped["disposition"] == "cancelled_before_admission"
    await published_rpc(armed, monkeypatch, read_command(armed, stopped, "stop"))
    assert execution.mutation_hold(armed) is None and not armed._pending_input


@pytest.mark.parametrize("fault", ["scheduler_paused", "new_conversation", "foreign_loop", "native_goal"])
async def test_unsupported_owned_execution_refuses_before_mutation(armed, monkeypatch, fault):
    from test_rust_backend_transition import disk
    action = "goal_start" if fault == "native_goal" else "job_run"
    review(armed, action, kind="loop" if fault == "foreign_loop" else "cron")
    if fault == "scheduler_paused":
        armed._gui_schedules_paused = True
    elif fault == "native_goal":
        monkeypatch.setattr(armed.backend, "owns_native_turns", True, raising=False)
    else:
        job = armed.jobs[0]
        if fault == "new_conversation":
            job.new_conversation = True
        else:
            import uuid
            job.owner_convo_id = str(uuid.uuid4())
        scheduler.save(armed.jobs, paths.data_root())
    snapshot = native_work.snapshot(armed, command(armed, type="gui.work.native_snapshot"))
    capture = armed._native_work_inspection
    cmd = {**armed._execution_fixture["validate_command"], "snapshot": snapshot["snapshot"]}
    if action == "job_run":
        cmd["target_revision"] = capture.rows[cmd["section"]][0]["revision"]
    before = disk(armed)
    with pytest.raises(ValueError):
        execution.validate(armed, cmd)
    assert disk(armed) == before and not armed._rewrite_test_requests and not work.bridge(armed).producers


async def test_empty_loop_owner_is_refused_before_review_or_stamp(armed):
    review(armed, kind="loop")
    armed.jobs[0].owner_convo_id = ""
    scheduler.save(armed.jobs, paths.data_root())
    before = scheduler.jobs_path(paths.data_root()).read_bytes()
    snapshot = native_work.snapshot(armed, command(armed, type="gui.work.native_snapshot"))
    assert not snapshot["sources"]["loops"]["available"]
    cmd = {**armed._execution_fixture["validate_command"], "snapshot": snapshot["snapshot"]}
    with pytest.raises(ValueError):
        execution.validate(armed, cmd)
    assert scheduler.jobs_path(paths.data_root()).read_bytes() == before
    assert armed.jobs[0].run_count == 0 and not armed._rewrite_test_requests
