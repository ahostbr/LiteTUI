"""Reviewed Work launches with exact workers, immutable receipts and no replay."""
from __future__ import annotations

import asyncio
import copy
import os
import secrets
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from litetui import background_input as work
from litetui import native_work as inspect
from litetui import native_work_controls as controls
from litetui.shutdown_wait import join_task

VERSION = 1
PREFIX = "gui.work.native_execution_"
OPERATIONS = tuple(PREFIX + x for x in ("validate", "apply", "readback", "stop"))
FILENAME = "native-work-execution.json"
MAX_RECEIPT = 524288
COMMON = ("version", "execution_version", "session_id", "agent_id", "backend", "model", "work_instance")
CLOSE_REQUIRED = "Explicit Work execution is uncertain; close without replay"


@dataclass(eq=False)
class Review:
    wire: bytes
    app: Any
    bridge: work.Bridge
    owner: tuple
    before: Any
    previous: bytes | None
    path: Path
    job: Any = None


@dataclass(eq=False)
class Proof:
    command: bytes
    result: dict
    raw: bytes
    published: bool = False
    readback: dict | None = None
    readback_raw: bytes | None = None
    readback_command: bytes | None = None
    verified: bool = False
    publication: Any = None


@dataclass(eq=False)
class Attempt:
    review: Review
    operation: str
    command: bytes
    worker: Any = None
    task: Any = None
    parent: Any = None
    parent_task: Any = None
    delivery: Any = None
    delivery_task: Any = None
    reservation: Any = None
    effect_attempted: bool = False
    stop_requested: bool = False
    after: Any = None
    store_revision: str | None = None
    entry: Any = None
    document: bytes | None = None
    proof: Proof | None = None
    stop_proof: Proof | None = None
    stop_id: str | None = None
    stop_operation: str | None = None
    stop_task: Any = None
    joined: list = field(default_factory=list)


@dataclass(eq=False)
class Publication:
    app: Any
    attempt: Attempt
    proof: Proof
    phase: str
    task: Any
    command: bytes
    result: dict
    raw: bytes
    used: bool = False


def _attempt(app):
    value = getattr(app, "_native_work_execution", None)
    return value if isinstance(value, Attempt) else None


def enabled(app):
    value = work.bridge(app)
    return value is not None and value.active and getattr(value, "execution_version", None) == VERSION


def _base(value):
    return {"version": VERSION, "execution_version": VERSION, **value.identity, "work_instance": value.instance}


def _owner(app):
    return (app._agent_session, app.store, app.backend, app.settings)


def _same_owner(app, review, *, historical=False):
    if review.app is not app or app._agent_session is not review.owner[0]:
        raise ValueError("Explicit Work owner changed")
    app._validate_owned_identity()
    if not historical and (any(current is not expected for current, expected in zip(_owner(app), review.owner, strict=True)) or work.bridge(app) is not review.bridge
            or work.identity(app) != review.bridge.identity):
        raise ValueError("Explicit Work runtime changed")


def _command(app, cmd, action, fields=(), attempt=None):
    work._keys(cmd, ("type", "id", *COMMON, *fields))
    if cmd["type"] != PREFIX + action or type(cmd["version"]) is not int or cmd["version"] != VERSION:
        raise ValueError("Unknown explicit Work operation or version")
    if type(cmd["execution_version"]) is not int or cmd["execution_version"] != VERSION:
        raise ValueError("Explicit Work requires exact execution version 1")
    work.text(cmd["id"])
    value = work.bridge(app) if attempt is None else attempt.review.bridge
    if value is None or any(cmd[k] != v for k, v in _base(value).items()):
        raise ValueError("Explicit Work command changed owned identity")
    if attempt is not None:
        _same_owner(app, attempt.review, historical=attempt.proof is not None and not attempt.effect_attempted)
    else:
        if not enabled(app):
            raise ValueError("Explicit Work execution is not negotiated and active")
        value.check()
    _bounded(cmd)
    return value


def _bounded(value, cap=work.LIMITS["reply_bytes"]):
    if len(work.encoded(value)) > cap:
        raise ValueError("Explicit Work record exceeds its bounded contract")
    return value


def _raw(value):
    if value is None:
        return {"json": None, "sha256": None}
    raw = work.encoded(value)
    if len(raw) > inspect.MAX_ROW:
        raise ValueError("Selected Work document exceeds its exact row budget")
    return {"json": raw.decode("utf-8"), "sha256": work.digest(raw)}


def _decode_raw(value):
    work._keys(value, ("json", "sha256"))
    if value["json"] is None:
        if value["sha256"] is not None:
            raise ValueError("Absent raw document has a digest")
        return None
    raw = value["json"].encode("utf-8")
    if len(raw) > inspect.MAX_ROW or work.digest(raw) != value["sha256"]:
        raise ValueError("Raw execution evidence changed")
    row = work.strict_json(raw)
    if not isinstance(row, dict) or work.encoded(row) != raw:
        raise ValueError("Raw execution evidence is not canonical")
    return row


def _receipt_file(app):
    return Path(app.convo_dir) / FILENAME


def _read_receipt(app):
    raw, _revision = inspect._read(_receipt_file(app))
    if raw is not None:
        document = validate_document(raw)
        if any(document["identity"][key] != work.identity(app)[key] for key in ("session_id", "agent_id")):
            raise ValueError("Execution receipt belongs to another owned conversation")
    return raw


def validate_document(raw):
    if len(raw) > MAX_RECEIPT:
        raise ValueError("Execution receipt exceeds its file budget")
    d = work.strict_json(raw)
    work._keys(d, ("version", "kind", "identity", "work_instance", "provider", "action_id", "apply_operation_id",
        "review_sha256", "action", "target_id", "previous_receipt_sha256", "before", "after", "store_revision",
        "payload", "payload_sha256", "producer", "input"))
    if type(d["version"]) is not int or d["version"] != VERSION or d["kind"] != "work_execution_launch":
        raise ValueError("Unsupported execution receipt")
    work._keys(d["identity"], ("session_id", "agent_id", "backend", "model"))
    for key in ("session_id", "agent_id"):
        work.canonical_id(d["identity"][key])
    work.text(d["identity"]["backend"], 64)
    work.text(d["identity"]["model"], 1024)
    for key in ("work_instance", "review_sha256", "store_revision", "payload_sha256"):
        work._hash(d[key])
    if d["previous_receipt_sha256"] is not None:
        work._hash(d["previous_receipt_sha256"])
    work.canonical_id(d["action_id"])
    work.text(d["apply_operation_id"])
    before, after = _decode_raw(d["before"]), _decode_raw(d["after"])
    if after is None or d["action"] not in {"job_run", "goal_start", "goal_resume"}:
        raise ValueError("Execution receipt lacks its publication")
    if d["action"] == "job_run":
        inspect._job(after)
    else:
        inspect.goal_row(after)
    work.validate_payload(d["payload"])
    if work.digest(work.encoded(d["payload"])) != d["payload_sha256"]:
        raise ValueError("Execution payload digest changed")
    ledger = {"version": 1, "identity": d["identity"], "instance": d["work_instance"], "provider": d["provider"],
        "revision": 1, "producers": [{"producer": d["producer"], "phase": "running"}],
        "inputs": [{"input": d["input"], "payload": d["payload"], "state": "queued", "reason": None,
            "admission": None, "epoch": {"instance": d["work_instance"], "identity": d["identity"], "provider": d["provider"]}}]}
    work.validate_document(ledger)
    if (d["producer"]["object_id"] != d["target_id"] or d["input"]["payload_sha256"] != d["payload_sha256"]
            or after["id"] != d["target_id"] or d["action"] != "goal_start" and before is None):
        raise ValueError("Execution receipt changed target ownership")
    _document_effect(d, before, after)
    return d


def _document_effect(d, before, after):
    from litetui import goal_loop, scheduler
    if d["action"] == "job_run":
        prior = inspect._job(before)["row"]
        if prior["new_conversation"] or prior["kind"] == "loop" and prior["owner_convo_id"] != d["identity"]["session_id"]:
            raise ValueError("Receipt job was not owned by the same conversation")
        expected = {**before, **prior, "run_count": prior["run_count"] + 1, "last_fired_slot": after["last_fired_slot"]}
        slot = datetime.fromisoformat(after["last_fired_slot"])
        if slot.strftime("%Y-%m-%dT%H:%M") != after["last_fired_slot"]:
            raise ValueError("Receipt job slot is not a canonical minute")
        if prior["kind"] == "loop":
            when = datetime.fromisoformat(after["next_run_at"])
            lower = slot + timedelta(minutes=prior["interval_minutes"])
            if not lower <= when < lower + timedelta(minutes=1):
                raise ValueError("Receipt loop cadence is not its canonical rearm")
            expected["next_run_at"] = after["next_run_at"]
        payload = {"content": prior["prompt"], "text": prior["prompt"], "source": "scheduled", "tool_profile": scheduler.level_of(prior["tool_profile"])}
        if d["producer"]["kind"] != prior["kind"] or d["producer"]["generation"] != prior["run_count"] + 1:
            raise ValueError("Receipt job generation changed")
    else:
        row = inspect.goal_row(after)
        if d["action"] == "goal_start":
            if len(row["id"]) != 12 or any(c not in "0123456789abcdef" for c in row["id"]):
                raise ValueError("Fresh goal ID is not canonical")
            expected = asdict(goal_loop.GoalState(row["objective"], id=row["id"], created=row["created"], updated=row["created"],
                tool_profile=row["tool_profile"], started_by=row["started_by"]))
        else:
            inspect.goal_row(before)
            expected = {**before, "status": "active", "updated": after["updated"]}
        state = goal_loop.GoalState(**row)
        item = goal_loop.goal_turn_item(state, goal_loop.START_INSTRUCTION if d["action"] == "goal_start" else goal_loop.RESUME_INSTRUCTION)
        payload = {key: item[key] for key in ("content", "text", "source", "tool_profile")}
        if d["producer"]["kind"] != "goal" or d["producer"]["generation"] < row["evaluated_turns"] + 1:
            raise ValueError("Receipt goal generation changed")
    controls._matches(after, expected)
    controls._matches(d["payload"], payload)


def recovery(app):
    try:
        raw = _read_receipt(app)
        if raw is None:
            return {"status": "absent", "document": None, "document_sha256": None, "reason": None}
        d = validate_document(raw)
        if any(d["identity"][k] != work.identity(app)[k] for k in ("session_id", "agent_id")):
            raise ValueError("Execution receipt belongs to another owned conversation")
        return {"status": "historical", "document": raw.decode("utf-8"), "document_sha256": work.digest(raw), "reason": None}
    except (ValueError, TypeError, OSError, KeyError, UnicodeError) as exc:
        return {"status": "unavailable", "document": None, "document_sha256": None, "reason": _reason(exc)}


def _reason(exc):
    return (str(exc) or type(exc).__name__).encode("utf-8")[:2048].decode("utf-8", "ignore")


def owned(app, job=None):
    from textual.worker import NoActiveWorker, get_current_worker
    a = _attempt(app)
    if a is None or a.task is not work._task():
        return None
    try:
        if get_current_worker() is not a.worker:
            return None
    except NoActiveWorker:
        return None
    if job is not None and a.review.job is not job:
        return None
    return a


def _retired(worker):
    return worker is None or worker.is_finished


def _terminal(a):
    e = a.entry
    return ("pending" not in lifecycle(a)["owners"].values()
        and (e is None or e.state in work.TERMINAL and e.producer.phase == "retired"))


def _verified(a):
    return a.proof is not None and a.proof.verified and (a.stop_proof is None or a.stop_proof.verified) and a.stop_task is None


def mutation_hold(app):
    a = _attempt(app)
    if a is not None and owned(app) is None and (not _verified(a) or not _terminal(a)):
        return "An explicit Work launch or Stop still owns its exact readback/lifecycle"
    return None


def operation_allowed(app, operation):
    from litetui import native_schedule
    if (mutation_hold(app) and operation not in native_schedule.INSPECTION and operation not in OPERATIONS
            and operation not in {"shutdown", "abort", "gui.work.native_settled", "tool_approval", "user_input"}):
        raise ValueError(mutation_hold(app))


def _guard(app, *, goal=False, reservation=None):
    from litetui import conversation_action, native_deferred, native_schedule
    from litetui.gui_rpc import _idle
    _idle(app)
    conversation_action.require_open(app)
    app._validate_owned_execution()
    from litetui import backend_transition
    backend_transition._quiet(app, require_unowned_server=False)
    value = work.bridge(app)
    if value is None or not enabled(app) or getattr(app, "_gui_quitting", False):
        raise ValueError("Explicit Work runtime is closed or inactive")
    value.check()
    if native_deferred.mutation_hold(app) or native_schedule.admission_refusal(app):
        raise ValueError("A different Work mutation owns admission")
    if app._pending_input or any(i.state not in work.TERMINAL for i in value.inputs.values()):
        raise ValueError("Resolve queued or uncertain Work input before execution")
    if any(p.phase != "retired" for p in value.producers.values()) or value.reservations - {reservation}:
        raise ValueError("Wait for exact producer and reservation retirement")
    if work.deferred_evidence(app) or getattr(app, "_native_work_recapture_required", None):
        raise ValueError("Resolve retained Work before execution")
    if any(not f.done() for f in getattr(app, "_approval_waiters", {}).values()) or getattr(app, "_gui_model_pending", {}):
        raise ValueError("Resolve approval/model work before execution")
    if getattr(app, "_hook_lifecycle", ()) or app._hook_lock.locked():
        raise ValueError("Lifecycle hooks are still pending")
    if goal:
        controls._goal_guard(app)


def _context(app):
    from litetui import conversation_transition
    return work.digest(conversation_transition._protected(app) + work.encoded(app.conversation)
                       + repr(conversation_transition._hooks(app)).encode("utf-8"))


def validate(app, cmd):
    from litetui import goal_loop, scheduler, seat_authority
    value = _command(app, cmd, "validate", ("inspection_instance", "snapshot", "action", "section", "target_id", "target_revision", "draft"))
    app._native_work_execution_review = None
    if mutation_hold(app):
        raise ValueError(mutation_hold(app))
    action = cmd["action"]
    if action not in {"job_run", "goal_start", "goal_resume"}:
        raise ValueError("Unsupported explicit Work action")
    _guard(app, goal=action != "job_run")
    capture = getattr(app, "_native_work_inspection", None)
    if (not isinstance(capture, inspect.Capture) or capture.instance != cmd["inspection_instance"]
            or capture.snapshot["snapshot"] != cmd["snapshot"] or capture.owner != inspect._owner(app, inspect._identity(app))):
        raise ValueError("Review requires the exact current Work inspection")
    previous = _read_receipt(app)
    draft = cmd["draft"]
    job = None
    started_by = None
    if action == "job_run":
        work._keys(draft, ())
        if cmd["section"] not in {"jobs", "loops"}:
            raise ValueError("Select a job or loop")
        selected = next((r for r in capture.rows[cmd["section"]] if r["row"]["id"] == cmd["target_id"]
                         and r["revision"] == cmd["target_revision"]), None)
        if selected is None:
            raise ValueError("Selected job changed after inspection")
        rows, _rev = controls.read_jobs(scheduler.jobs_path(app._agent_session.store.data_root))
        before = next((r for r in rows if r["id"] == cmd["target_id"]), None)
        if before is None or inspect._job(before)["revision"] != cmd["target_revision"]:
            raise ValueError("Selected raw job changed after inspection")
        canonical = inspect._job(before)["row"]
        if canonical["new_conversation"] or canonical["kind"] == "loop" and canonical["owner_convo_id"] != app.convo_id:
            raise ValueError("Explicit Run requires the current conversation and loop owner")
        if getattr(app, "_gui_schedules_paused", False):
            raise ValueError("The scheduler is paused")
        profile = scheduler.level_of(canonical["tool_profile"])
        if why := seat_authority.withheld(app, profile):
            raise ValueError(why)
        # Read-only adoption preserves the canonical runtime Job object's identity.
        scheduler.refresh(app.jobs, app._agent_session.store.data_root)
        job = next((j for j in app.jobs if j.id == cmd["target_id"]), None)
        if job is None:
            raise ValueError("Selected job was deleted")
        after = {**copy.deepcopy(before), **canonical, "run_count": canonical["run_count"] + 1, "last_fired_slot": None}
        times = ["last_fired_slot"]
        if canonical["kind"] == "loop":
            after["next_run_at"] = None
            times.append("next_run_at")
        target = job.id
        payload = {"content": job.prompt, "text": job.prompt, "source": "scheduled", "tool_profile": profile}
        generation = canonical["run_count"] + 1
        consequence = "Run this saved prompt once here at its saved profile; consume one generation and rearm loop cadence. Enabled state stays unchanged. Pre-turn hooks cannot ask for approval."
    else:
        if cmd["section"] != "goal":
            raise ValueError("Select the current goal")
        current = capture.snapshot["goal"]
        before, revision = controls._goal(Path(app.convo_dir) / goal_loop.GOAL_FILENAME)
        expected_id = None if before is None else before["id"]
        if current["status"] == "unavailable" or revision != cmd["target_revision"] or cmd["target_id"] != expected_id or current["revision"] != revision:
            raise ValueError("Goal changed after inspection")
        if action == "goal_start":
            work._keys(draft, ("objective",))
            objective = work.payload_text(draft["objective"]).strip()
            if not objective:
                raise ValueError("Goal objective must be nonempty")
            source = getattr(app, "_command_source", None)
            app._command_source = "rpc"
            try:
                started_by = seat_authority.command_origin(app)
            finally:
                app._command_source = source
            state = goal_loop.GoalState(objective, tool_profile=seat_authority.seat_profile(app), started_by=started_by)
            after = asdict(state)
            after.update(created=None, updated=None)
            times = ["created", "updated"]
            draft = {"objective": objective}
        else:
            work._keys(draft, ())
            if before is None:
                raise ValueError("There is no goal to resume")
            state = goal_loop.GoalState(**{k: v for k, v in before.items() if k in goal_loop.GoalState.__dataclass_fields__})
            after = {**copy.deepcopy(before), "status": "active", "updated": None}
            times = ["updated"]
            started_by = state.started_by
        target = state.id
        item = goal_loop.goal_turn_item(state, goal_loop.START_INSTRUCTION if action == "goal_start" else goal_loop.RESUME_INSTRUCTION)
        payload = {k: item[k] for k in ("content", "text", "source", "tool_profile")}
        generation = max([state.evaluated_turns + 1, *(p.ref["generation"] + 1 for p in value.producers.values()
                          if p.ref["kind"] == "goal" and p.ref["object_id"] == state.id)])
        consequence = ("Replace the reviewed saved goal and reset its evidence; " if action == "goal_start" else "Preserve goal identity and evidence; ") + "publish active state and launch one canonical goal input. Stop-current-turn does not pause the goal. Pre-turn hooks cannot ask for approval."
    work.validate_payload(payload)
    result = {**_base(value), "kind": "work_execution_review", "inspection_instance": capture.instance,
        "snapshot": cmd["snapshot"], "action": action, "action_id": str(uuid.uuid4()), "review_token": secrets.token_hex(32),
        "section": cmd["section"], "selected_id": cmd["target_id"], "target_revision": cmd["target_revision"],
        "target_id": target, "draft": copy.deepcopy(draft), "before": _raw(before), "after_template": _raw(after),
        "time_fields": times, "payload": payload, "payload_sha256": work.digest(work.encoded(payload)),
        "producer_generation": generation, "started_by": started_by, "materialize": bool(app.store.pending),
        "previous_receipt_sha256": None if previous is None else work.digest(previous), "context_sha256": _context(app), "consequence": consequence}
    _bounded(result)
    if len(work.encoded(result)) + len(work.encoded(payload)) + 16384 > MAX_RECEIPT:
        raise ValueError("Review cannot fit its complete immutable launch receipt")
    app._native_work_execution_review = Review(work.encoded(result), app, value, _owner(app), copy.deepcopy(before), previous,
                                              _receipt_file(app), job=job)
    return result


def _preflight(app, a):
    _same_owner(app, a.review)
    if a.stop_requested or getattr(app, "_gui_quitting", False):
        raise ValueError("Explicit launch stopped before effects")
    _guard(app, goal=a.review.job is None, reservation=a.reservation)
    r = work.strict_json(a.review.wire)
    if _context(app) != r["context_sha256"] or _read_receipt(app) != a.review.previous:
        raise ValueError("Reviewed execution context or receipt changed")


def preflight_job(app, job, *, manual):
    from litetui import scheduler, seat_authority
    a = owned(app, job)
    if a is None:
        return None
    if not manual:
        raise ValueError("Explicit manual capability cannot authorize an automatic tick")
    _preflight(app, a)
    rows, _revision = controls.read_jobs(scheduler.jobs_path(app._agent_session.store.data_root))
    raw = next((r for r in rows if r["id"] == job.id), None)
    controls._matches(raw, a.review.before)
    canonical = inspect._job(raw)["row"]
    if (work.encoded(asdict(job)) != work.encoded(canonical) or job.new_conversation
            or job.kind == "loop" and job.owner_convo_id != app.convo_id
            or getattr(app, "_gui_schedules_paused", False) or seat_authority.withheld(app, scheduler.level_of(job.tool_profile))):
        raise ValueError("Explicit Run lost its exact manual candidate")
    a.effect_attempted = True
    return a


def stamped(app, job):
    from litetui import scheduler
    a = owned(app, job)
    if a is None:
        return
    rows, revision = controls.read_jobs(scheduler.jobs_path(app._agent_session.store.data_root))
    a.after = next((r for r in rows if r["id"] == job.id), None)
    a.store_revision = revision
    if a.after is None or work.encoded(inspect._job(a.after)["row"]) != work.encoded(asdict(job)):
        raise ValueError("Explicit Run stamp did not independently read back")


def bind_entry(app, entry):
    a = _attempt(app)
    if a is not None and (owned(app) is a or a.delivery_task is work._task()):
        if a.entry is not None:
            raise ValueError("Explicit launch attempted another captured input")
        a.entry = entry


def claim_entry(app, entry):
    """Bind and persist inside actual producer delivery, before any first wake."""
    a = _attempt(app)
    if a is None:
        return
    valid = owned(app) is a or (a.delivery is not None and a.delivery_task is work._task())
    if not valid:
        return
    if a.entry is not None and a.entry is not entry or a.document is not None:
        raise ValueError("Explicit launch attempted a second input")
    a.entry = entry
    r = work.strict_json(a.review.wire)
    if (entry.ref["payload_sha256"] != r["payload_sha256"] or entry.producer.ref["object_id"] != r["target_id"]
            or entry.producer.ref["generation"] != r["producer_generation"] or a.after is None):
        raise ValueError("Canonical delivery changed the reviewed input")
    d = {"version": 1, "kind": "work_execution_launch", "identity": copy.deepcopy(a.review.bridge.identity),
        "work_instance": a.review.bridge.instance, "provider": a.review.bridge.provider,
        "action_id": r["action_id"], "apply_operation_id": a.operation, "review_sha256": work.digest(a.review.wire),
        "action": r["action"], "target_id": r["target_id"], "previous_receipt_sha256": r["previous_receipt_sha256"],
        "before": r["before"], "after": _raw(a.after), "store_revision": a.store_revision,
        "payload": copy.deepcopy(entry.payload), "payload_sha256": entry.ref["payload_sha256"],
        "producer": copy.deepcopy(entry.producer.ref), "input": copy.deepcopy(entry.ref)}
    raw = work.encoded(d)
    validate_document(raw)
    _write_receipt(app, a, raw)
    a.document = raw
    if a.stop_requested or getattr(app, "_gui_quitting", False):
        _cancel_unclaimed(app, a)


def _write_receipt(app, a, raw):
    _same_owner(app, a.review)
    if a.review.path != _receipt_file(app) or _read_receipt(app) != a.review.previous:
        raise ValueError("Execution receipt changed before publication")
    descriptor, name = tempfile.mkstemp(prefix=".work-execution-", suffix=".tmp", dir=a.review.path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _same_owner(app, a.review)
        if _read_receipt(app) != a.review.previous:
            raise ValueError("Execution receipt changed during publication")
        os.replace(name, a.review.path)
        if work._read(a.review.path, MAX_RECEIPT) != raw:
            raise OSError("Execution receipt publication did not read back")
    finally:
        Path(name).unlink(missing_ok=True)


def admission_allowed(app, entry):
    a = _attempt(app)
    if a is not None and a.entry is entry and (a.document is None or a.stop_requested):
        raise ValueError("Exact explicit launch has not released admission")


def pre_turn_hook(app):
    a = _attempt(app)
    if a is None:
        return False
    if owned(app) is a:
        return True
    e = a.entry
    return e is not None and e.admission is None and (work._task() is a.delivery_task
        or e.admission_worker is not None and getattr(e.admission_worker, "_task", None) is work._task())


def _state(a, worker):
    return "absent" if worker is None else "retired" if worker.is_finished and any(w is worker for w in a.joined) else "pending"


def lifecycle(a):
    e = a.entry
    callbacks = [] if e is None else [t for t in (e.producer.observer, e.observer) if t is not None]
    admission = None if e is None else e.admission_worker
    if (admission is not None and e.observer is not None and e.observer.done()
            and not e.observer.cancelled() and e.observer.exception() is None and admission.is_finished
            and not any(w is admission for w in a.joined)):
        a.joined.append(admission)
    return {"producer_phase": None if e is None else e.producer.phase, "input_state": None if e is None else e.state,
        "admission": None if e is None else copy.deepcopy(e.admission), "settlement": None if e is None else copy.deepcopy(e.receipt),
        "owners": {"launch": _state(a, a.worker), "delivery": _state(a, a.delivery),
                   "admission": _state(a, admission),
                   "callbacks": "absent" if not callbacks else "retired" if all(t.done() and not t.cancelled() and t.exception() is None for t in callbacks) else "pending"}}


def _result(a, status, reason=None):
    r = work.strict_json(a.review.wire)
    return _bounded({**_base(a.review.bridge), "kind": "work_execution_result", "action_id": r["action_id"],
        "review_token": r["review_token"], "apply_operation_id": a.operation, "review_sha256": work.digest(a.review.wire),
        "action": r["action"], "target_id": r["target_id"], "status": status, "effect_attempted": a.effect_attempted,
        "close_required": status == "uncertain", "reason": reason,
        "document": None if a.document is None else a.document.decode("utf-8"),
        "document_sha256": None if a.document is None else work.digest(a.document), "lifecycle": lifecycle(a)})


async def execute(app, a):
    """Actual registered outer worker; controlled fixtures may pause this boundary."""
    a.task = work._task()
    try:
        _preflight(app, a)
        if a.review.job is not None:
            result = await work.run_job(app, a.review.job, manual=True)
            if not isinstance(result, dict) or a.document is None:
                raise ValueError("Explicit Run lacks its exact durable launch")
        else:
            await _run_goal(app, a)
        return _result(a, "published")
    except BaseException as exc:  # noqa: BLE001 - attempted effects are never rolled back
        if a.effect_attempted:
            a.review.bridge.error = CLOSE_REQUIRED
        return _result(a, "uncertain" if a.effect_attempted else "refused", _reason(exc))
    finally:
        work.release(app, a.reservation)


async def apply(app, cmd):
    from textual.worker import NoActiveWorker, get_current_worker
    work._keys(cmd, ("type", "id", "review"))
    work.text(cmd["id"])
    review = getattr(app, "_native_work_execution_review", None)
    if cmd["type"] != PREFIX + "apply" or not isinstance(review, Review) or work.encoded(cmd["review"]) != review.wire:
        raise ValueError("Apply requires its exact unconsumed execution Review")
    if mutation_hold(app):
        raise ValueError(mutation_hold(app))
    app._native_work_execution_review = None
    a = Attempt(review, cmd["id"], work.encoded(cmd))
    app._native_work_execution = a
    try:
        a.parent = get_current_worker()
    except NoActiveWorker:
        pass
    a.parent_task = work._task()
    a.worker = app.run_worker(execute(app, a), group="work-execution", exclusive=False, exit_on_error=False)
    if review.job is not None:
        if not hasattr(app, "_native_work_jobs"):
            app._native_work_jobs = {}
        if not hasattr(app, "_native_work_job_owners"):
            app._native_work_job_owners = {}
        app._native_work_jobs[review.job.id] = a.worker
        app._native_work_job_owners[a.worker] = {"parent": a.parent, "parent_task": a.parent_task, "task": None}
    from textual.worker import WorkerCancelled
    try:
        result = await join_task(asyncio.create_task(a.worker.wait()))
    except WorkerCancelled:
        result = None
    if not isinstance(result, dict):
        result = _result(a, "uncertain" if a.effect_attempted else "refused", "Launch was cancelled before its worker body")
    a.joined.append(a.worker)
    result["lifecycle"] = lifecycle(a)
    a.proof = Proof(work.encoded(cmd), result, work.encoded(result))
    return result


async def _run_goal(app, a):
    from litetui import goal_loop
    from litetui.shared_state import coordinated_write
    r = work.strict_json(a.review.wire)
    a.reservation = work.reserve(app)
    _preflight(app, a)
    path = Path(app.convo_dir) / goal_loop.GOAL_FILENAME
    if app.store.pending:
        a.effect_attempted = True
        app._materialise_convo()
    with coordinated_write(path):
        _same_owner(app, a.review)
        controls._goal_guard(app)
        current, _revision = controls._goal(path)
        controls._matches(current, a.review.before)
        if a.stop_requested or getattr(app, "_gui_quitting", False):
            raise ValueError("Goal launch stopped before publication")
        after = _decode_raw(r["after_template"])
        now = datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005 - canonical local time
        for key in r["time_fields"]:
            after[key] = now
        a.effect_attempted = True
        goal_loop._replace_goal(app.convo_dir, after)
        actual, revision = controls._goal(path)
        controls._matches(actual, after)
        a.after, a.store_revision = actual, revision
    state = goal_loop.GoalState(**{k: v for k, v in after.items() if k in goal_loop.GoalState.__dataclass_fields__})
    item = {**copy.deepcopy(r["payload"]), "goal_continuation": True}
    a.delivery = work.start_goal(app, state, item, reservation=a.reservation, execution=a)
    a.reservation = None
    if a.delivery is None:
        raise ValueError("Goal launch did not retain its actual delivery worker")
    await join_task(asyncio.create_task(a.delivery.wait()))
    a.joined.append(a.delivery)
    if a.document is None:
        raise ValueError("Goal launch lacks its exact durable receipt")


def _select(app, cmd, action, fields):
    a = _attempt(app)
    if a is None:
        raise ValueError("Unknown explicit invocation; restart never grants authority")
    _command(app, cmd, action, ("action_id", "review_token", "apply_operation_id", *fields), a)
    r = work.strict_json(a.review.wire)
    if cmd["action_id"] != r["action_id"] or cmd["review_token"] != r["review_token"] or cmd["apply_operation_id"] != a.operation:
        raise ValueError("Explicit Work invocation token changed")
    return a


def _cancel_unclaimed(app, a):
    e = a.entry
    if e is None or e.admission is not None:
        return
    if e.state in {"claiming", "uncertain"} or e.claim_task is not None or e.appended or e.provider_delivery is not None:
        raise ValueError("Pre-admission cancellation cannot prove nondelivery")
    a.review.bridge.input_event(e, "cancelled", "Explicit launch stopped before admission")
    app._pending_input[:] = [item for item in app._pending_input if item is not e.item]


async def _join_worker(a, worker):
    if worker is None:
        return
    from textual.worker import WorkerCancelled, WorkerFailed
    try:
        await join_task(asyncio.create_task(worker.wait()))
    except (WorkerCancelled, WorkerFailed):
        pass
    if not worker.is_finished:
        raise ValueError("Explicit execution worker did not retire")
    if not any(w is worker for w in a.joined):
        a.joined.append(worker)


async def stop(app, cmd):
    a = _select(app, cmd, "stop", ())
    if a.stop_task is not None or a.stop_proof is not None and not a.stop_proof.verified:
        raise ValueError("An exact Stop still awaits verification")
    a.stop_task = work._task()
    a.stop_id, a.stop_operation = str(uuid.uuid4()), cmd["id"]
    status, disposition, reason = "stopped", "no_effect", None
    try:
        if a.entry is not None and a.entry.admission is not None:
            status = disposition = "settled" if a.entry.receipt is not None else "admitted"
        else:
            a.stop_requested = True
            for worker in (a.delivery, None if a.entry is None else a.entry.admission_worker, a.worker):
                if worker is not None and not worker.is_finished:
                    worker.cancel()
            for worker in (a.worker, a.delivery, None if a.entry is None else a.entry.admission_worker):
                await _join_worker(a, worker)
            _cancel_unclaimed(app, a)
            if a.entry is not None and a.entry.producer.observer is not None:
                await join_task(a.entry.producer.observer)
            if a.effect_attempted and a.document is None:
                raise ValueError("Stopped publication lacks immutable launch evidence")
            disposition = "no_effect" if not a.effect_attempted else "cancelled_before_admission"
    except BaseException as exc:  # noqa: BLE001 - retain unknown cancellation effects
        status = disposition = "uncertain"
        reason = _reason(exc)
        a.review.bridge.error = CLOSE_REQUIRED
    finally:
        a.stop_task = None
    r = work.strict_json(a.review.wire)
    result = {**_base(a.review.bridge), "kind": "work_execution_stop_result", "action_id": r["action_id"],
        "review_token": r["review_token"], "apply_operation_id": a.operation, "stop_id": a.stop_id,
        "stop_operation_id": cmd["id"], "status": status, "effect_attempted": a.effect_attempted,
        "close_required": status == "uncertain", "reason": reason,
        "document_sha256": None if a.document is None else work.digest(a.document), "disposition": disposition, "lifecycle": lifecycle(a)}
    a.stop_proof = Proof(work.encoded(cmd), result, work.encoded(result))
    return _bounded(result)


def readback(app, cmd):
    from litetui import goal_loop, scheduler
    a = _select(app, cmd, "readback", ("scope", "stop_id", "result_operation_id", "result_sha256"))
    proof = a.proof if cmd["scope"] == "launch" else a.stop_proof if cmd["scope"] == "stop" else None
    expected_stop = None if cmd["scope"] == "launch" else a.stop_id
    operation = a.operation if cmd["scope"] == "launch" else a.stop_operation
    if (proof is None or not proof.published or proof.readback is not None or cmd["stop_id"] != expected_stop
            or cmd["result_operation_id"] != operation or cmd["result_sha256"] != work.digest(proof.raw)
            or work.encoded(proof.result) != proof.raw):
        raise ValueError("Readback lacks its exact published proof scope")
    value = a.review.bridge
    if work._read(a.review.path, MAX_RECEIPT) != (a.document if a.document is not None else a.review.previous):
        raise ValueError("Immutable execution receipt changed")
    if work._read(value.path) != value.raw:
        raise ValueError("Execution Work ledger changed outside its owner")
    if value.raw is not None:
        ledger = work.strict_json(value.raw)
        work.validate_document(ledger)
        if a.entry is not None:
            matches = [row for row in ledger["inputs"] if row["input"] == a.entry.ref and row["payload"] == a.entry.payload]
            if len(matches) != 1:
                raise ValueError("Execution lost its exact durable Work input")
    current = {"status": "unavailable", "row": _raw(None), "store_revision": None, "reason": None}
    try:
        if a.review.job is not None:
            rows, rev = controls.read_jobs(scheduler.jobs_path(app._agent_session.store.data_root))
            row = next((r for r in rows if r["id"] == a.review.job.id), None)
        else:
            row, rev = controls._goal(Path(app.convo_dir) / goal_loop.GOAL_FILENAME)
            if row is not None:
                inspect.goal_row(row)
        current.update(status="absent" if row is None else "present", row=_raw(row), store_revision=rev)
    except (ValueError, TypeError, OSError) as exc:
        current["reason"] = _reason(exc)
    r = work.strict_json(a.review.wire)
    result = {**_base(value), "kind": "work_execution_readback", "action_id": r["action_id"], "review_token": r["review_token"],
        "apply_operation_id": a.operation, "scope": cmd["scope"], "stop_id": expected_stop,
        "result_operation_id": operation, "result_sha256": cmd["result_sha256"], "verified": True,
        "document": None if a.document is None else a.document.decode("utf-8"),
        "document_sha256": None if a.document is None else work.digest(a.document), "ledger_revision": value.revision,
        "ledger_sha256": None if value.raw is None else work.digest(value.raw), "current": current, "lifecycle": lifecycle(a)}
    proof.readback, proof.readback_raw, proof.readback_command = result, work.encoded(result), work.encoded(cmd)
    return _bounded(result)


def prepare_publication(app, cmd, result):
    if cmd.get("type") not in OPERATIONS or cmd["type"] == PREFIX + "validate":
        return None
    a = _attempt(app)
    if a is None:
        raise ValueError("Execution publication lost its invocation")
    phase = "readback" if cmd["type"] == PREFIX + "readback" else "result"
    proof = a.stop_proof if cmd["type"] == PREFIX + "stop" or cmd.get("scope") == "stop" else a.proof
    if proof is None:
        raise ValueError("Execution publication lost its proof")
    expected = proof.readback if phase == "readback" else proof.result
    raw = proof.readback_raw if phase == "readback" else proof.raw
    command = proof.readback_command if phase == "readback" else proof.command
    if (expected is not result or work.encoded(result) != raw or work.encoded(cmd) != command or proof.publication is not None
            or (proof.verified if phase == "readback" else proof.published)):
        raise ValueError("Execution publication changed its exact response")
    ticket = Publication(app, a, proof, phase, work._task(), command, result, raw)
    proof.publication = ticket
    return ticket


def finish_publication(app, ticket):
    if (not isinstance(ticket, Publication) or ticket.app is not app or ticket.used or ticket.task is not work._task()
            or _attempt(app) is not ticket.attempt or ticket.proof.publication is not ticket
            or work.encoded(ticket.result) != ticket.raw):
        raise ValueError("Explicit execution publication is foreign, changed or consumed")
    _same_owner(app, ticket.attempt.review, historical=not ticket.attempt.effect_attempted)
    ticket.used = True
    ticket.proof.publication = None
    if ticket.phase == "readback":
        ticket.proof.verified = True
    else:
        ticket.proof.published = True


def _shutdown_proof(app, a, value):
    r = None if a is None else work.strict_json(a.review.wire)
    life = None if a is None else lifecycle(a)
    e = None if a is None else a.entry
    return {"version": 1, "execution_version": 1, "identity": copy.deepcopy(value.identity), "work_instance": value.instance,
        "action_id": None if r is None else r["action_id"], "review_sha256": None if a is None else work.digest(a.review.wire),
        "apply_operation_id": None if a is None else a.operation,
        "stop_ids": [] if a is None or a.stop_id is None else [a.stop_id],
        "stop_operation_ids": [] if a is None or a.stop_operation is None else [a.stop_operation],
        "launch_readback_published": a is not None and a.proof is not None and a.proof.verified,
        "stop_readbacks_published": [a.stop_id] if a is not None and a.stop_proof is not None and a.stop_proof.verified else [],
        "effect_attempted": a is not None and a.effect_attempted,
        "document_sha256": None if a is None or a.document is None else work.digest(a.document),
        "disposition": "idle" if a is None else "no_effect" if not a.effect_attempted else "uncertain" if a.document is None or e is not None and e.state in {"claiming", "uncertain"} or value.error == CLOSE_REQUIRED
                       else "admitted_settled" if e is not None and e.receipt is not None
                       else "cancelled_before_admission" if e is not None and e.state == "cancelled" else "published",
        "owners": {k: "absent" for k in ("launch", "delivery", "admission", "callbacks")} if life is None else life["owners"],
        "input_id": None if e is None else e.ref["input_id"], "admission_id": None if e is None or e.admission is None else e.admission["admission_id"],
        "admission_closed": True, "worker_settled": True, "callback_settled": True, "effects_verified": False}


async def settle_before_shutdown(app):
    value = work.bridge(app)
    a = _attempt(app)
    if a is not None:
        value = a.review.bridge
    if value is None or getattr(value, "execution_version", None) != VERSION:
        return copy.deepcopy(getattr(app, "_native_execution_predecessor", None))
    if a is not None:
        a.stop_requested = True
        for worker in (a.worker, a.delivery, None if a.entry is None else a.entry.admission_worker):
            if worker is not None and not worker.is_finished:
                worker.cancel()
        for worker in (a.worker, a.delivery, None if a.entry is None else a.entry.admission_worker):
            await _join_worker(a, worker)
        if a.stop_task is not None and a.stop_task is not work._task():
            await join_task(a.stop_task)
        if a.entry is not None:
            for task in (a.entry.producer.observer, a.entry.observer):
                if task is not None:
                    await join_task(task)
        if "pending" in lifecycle(a)["owners"].values():
            raise ValueError("Execution shutdown retains unretired owners")
    proof = _shutdown_proof(app, a, value)
    app._native_execution_shutdown = copy.deepcopy(proof)
    return proof


def identity_retired(app, value):
    if getattr(value, "execution_version", None) != VERSION:
        return {}
    a = _attempt(app)
    if a is not None and (a.review.bridge is not value or not _verified(a) or not _terminal(a)):
        raise ValueError("Identity transition cannot abandon explicit execution ownership")
    proof = _shutdown_proof(app, a, value)
    app._native_execution_predecessor = copy.deepcopy(proof)
    return {"work_execution_shutdown": proof}


def activated(app, value):
    a = _attempt(app)
    if a is not None and a.review.bridge is not value:
        if not _verified(a) or not _terminal(a) or getattr(app, "_native_execution_predecessor", None) is None:
            raise ValueError("Fresh capture cannot replace unresolved execution ownership")
        app._native_work_execution = None
        app._native_work_execution_review = None
    app._native_execution_predecessor = None
