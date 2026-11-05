"""Opt-in durable ownership of autonomous input and exact producer lifetimes.

Canonical queue/hook code decides delivery timing; copied context never grants
admission. Legacy callers do not create this bridge.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import secrets
import stat
import tempfile
import threading
import uuid
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from litetui.event_origin import EventOrigin

VERSION = 1
LIMITS = {"producers": 256, "inputs": 256, "payload_bytes": 65536,
          "ledger_bytes": 33554432, "envelope_bytes": 131072, "reply_bytes": 1048576}
FILENAME = "native-work-inputs.json"
TERMINAL = frozenset({"settled", "cancelled", "reconciled"})
_EXECUTION: ContextVar[Any] = ContextVar("native_work_execution", default=None)
_PRODUCER: ContextVar[Any] = ContextVar("native_work_producer", default=None)
_ADMISSION: ContextVar[Any] = ContextVar("native_work_admission", default=None)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def ident():
    return str(uuid.uuid4())


def text(value, cap=128):
    if type(value) is not str or not value or "\0" in value or len(value.encode()) > cap:
        raise ValueError("Invalid Work identity or bounded text")
    return value


def canonical_id(value):
    text(value)
    if str(uuid.UUID(value)) != value:
        raise ValueError("Work ID must be a canonical UUID")
    return value


def identity(app):
    return {"session_id": canonical_id(app.convo_id),
            "agent_id": canonical_id(app._agent_session.authority.agent_id),
            "backend": text(app.backend.name, 64), "model": text(app.model_id, 1024)}


def provider(app):
    if getattr(app.backend, "owns_native_turns", False):
        return "claude"
    return "codex" if hasattr(app.backend, "app_server") else "host"


def bridge(app):
    value = getattr(app, "_native_work_bridge", None)
    return value if isinstance(value, Bridge) else None


def enabled(app):
    value = bridge(app)
    return value is not None and value.active


def _task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _observe(coroutine):
    # A settlement observer is not the worker it joins. Textual's active_worker
    # context otherwise follows create_task and falsely diagnoses self-wait.
    from textual.worker import active_worker
    context = copy_context()
    context.run(cast(Any, active_worker).set, None)
    for variable in (_EXECUTION, _PRODUCER, _ADMISSION):
        context.run(variable.set, None)
    task = asyncio.create_task(coroutine, context=context)
    # Retain failures for exact readback/shutdown; retrieving the exception here
    # prevents an unrelated event-loop warning from becoming their only report.
    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return task


def _read(path, cap=LIMITS["ledger_bytes"]):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Work ledger must be a regular unlinked file")
    with path.open("rb") as handle:
        value = handle.read(cap + 1)
    if len(value) > cap:
        raise ValueError("Work ledger exceeds its byte capacity")
    return value


@dataclass
class Producer:
    ref: dict
    task: Any = None
    worker: Any = None
    worker_id: str | None = None
    asyncio_task: Any = None
    action: Any = None
    phase: str = "registered"
    input_id: str | None = None
    observer: Any = None
    call: Any = None
    kill_workers: list = field(default_factory=list)
    persistence: dict = field(default_factory=lambda: {
        "task": {"status": "not_applicable", "reason": None},
        "output": {"status": "not_applicable", "reason": None}})


@dataclass
class Input:
    ref: dict
    payload: dict
    item: dict
    producer: Producer
    state: str = "queued"
    reason: str | None = None
    admission: dict | None = None
    worker: Any = None
    observer: Any = None
    receipt: dict | None = None
    claim_task: Any = None
    appended: bool = False
    committed_verified: bool = False
    committed_finished: bool = False
    provider_lane: Any = None
    reconciliation: Any = None
    recovered: bool = False
    provider_delivery: Any = None
    admission_worker: Any = None
    epoch: Any = None


class Bridge:
    def __init__(self, app):
        self.app = app
        self.identity = identity(app)
        self.provider = provider(app)
        self.instance = secrets.token_hex(32)
        self.activation_token = secrets.token_hex(32)
        self.active = False
        self.activation_publication: Any = None
        self.scheduled_transition_version: int | None = None
        self.execution_version: int | None = None
        self.requested_execution_version: int | None = None
        self.runtime_controls_version: int | None = None
        self.requested_runtime_controls_version: int | None = None
        self.closing = False
        self.sequence = 0
        self.revision = 0
        self.queue_revision = 0
        self.producers: dict[str, Producer] = {}
        self.inputs: dict[str, Input] = {}
        self.executions: dict[str, tuple[Any, EventOrigin]] = {}
        self.boundaries: dict[str, int] = {}
        self.reservations: set[str] = set()
        self.calls: list[ToolCall] = []
        self.error: str | None = None
        self.uncertain_inputs: set[str] = set()
        self.uncertain_error_owned = False
        self.pending_readbacks: dict[str, dict] = {}
        self.user_queue_review: tuple[Any, bytes, Any] | None = None
        self.close_event = None
        self.shutdown_task = None
        self.owner = (app._agent_session, app.store, app.backend, app.model_id, app.convo_id)
        self.path = Path(app.convo_dir) / FILENAME
        self.raw = _read(self.path)
        self.recovered = []
        if self.raw is not None:
            document = strict_json(self.raw)
            validate_document(document)
            if document.get("version") != VERSION or any(document["identity"][key] != self.identity[key] for key in ("session_id", "agent_id")):
                raise ValueError("Work ledger belongs to another owned identity")
            self.revision = document["revision"]
            if type(self.revision) is not int or self.revision < 0:
                raise ValueError("Malformed Work ledger revision")
            producer_rows = {row["producer"]["producer_id"]: row for row in document["producers"]}
            for row in document["inputs"]:
                if row["state"] in TERMINAL:
                    continue
                ref = row["input"]
                canonical_id(ref["input_id"])
                producer_row = producer_rows[ref["producer_id"]]
                producer_record = Producer(copy.deepcopy(producer_row["producer"]), phase="retired")
                state = "uncertain" if row["state"] in {"claiming", "admitted", "uncertain"} else "held"
                payload = copy.deepcopy(row["payload"])
                if digest(encoded(payload)) != ref["payload_sha256"]:
                    raise ValueError("Recovered Work payload changed")
                item = {**payload, "_work_input_id": ref["input_id"]}
                entry = Input(copy.deepcopy(ref), payload, item, producer_record, state=state,
                              reason="Retained from a prior runtime; no automatic replay", admission=row.get("admission"), recovered=True, epoch=copy.deepcopy(row["epoch"]))
                self.inputs[ref["input_id"]] = entry
                self.queue_revision = max(self.queue_revision, ref["queue_revision"])
                self.recovered.append({"producer": producer_record.ref, "input": entry.ref, "state": state,
                                       "reason": entry.reason, "admission": entry.admission, "epoch": entry.epoch})

    def check(self, *, allow_closing=False, allow_error=False):
        if (self.app._agent_session is not self.owner[0] or self.app.store is not self.owner[1]
                or self.app.backend is not self.owner[2] or self.app.model_id != self.owner[3]
                or self.app.convo_id != self.owner[4] or identity(self.app) != self.identity
                or provider(self.app) != self.provider):
            raise ValueError("Work runtime ownership changed")
        if getattr(self.app.store, "owned", False) is not True:
            expected = self.app._agent_session.conversation_directory(self.app.convo_id)
            if (not self.app.store.pending or self.app.store.agent_session is not self.app._agent_session
                    or Path(self.app.convo_dir) != expected or self.raw is not None):
                raise ValueError("Work conversation storage is no longer owned")
        self.app._validate_owned_identity()
        if self.error and not allow_error:
            raise ValueError(self.error)
        if self.pending_readbacks and not allow_error:
            raise ValueError("Work reconciliation awaits its exact published readback")
        if self.closing and not allow_closing:
            raise ValueError("Work admission is closed")

    def document(self):
        producers = {p.ref["producer_id"]: p for p in self.producers.values()}
        producers.update({i.producer.ref["producer_id"]: i.producer for i in self.inputs.values()})
        return {"version": VERSION, "identity": self.identity, "instance": self.instance, "provider": self.provider, "revision": self.revision,
                "producers": [{"producer": p.ref, "phase": p.phase} for p in producers.values()],
                "inputs": [{"input": i.ref, "payload": i.payload, "state": i.state, "reason": i.reason,
                            "admission": i.admission, "epoch": i.epoch, "reconciliation": i.reconciliation} for i in self.inputs.values()]}

    def persist(self):
        self.check(allow_closing=True, allow_error=True)
        if not self.app.store.owned or self.app.store.pending:
            raise ValueError("Work ledger publication requires canonical materialization and its exact lease")
        if _read(self.path) != self.raw:
            self.error = "Work ledger changed outside its owner; close and inspect without replay"
            raise ValueError(self.error)
        self.revision += 1
        data = encoded(self.document())
        if len(data) > LIMITS["ledger_bytes"]:
            self.error = "Work ledger capacity exhausted; inputs remain owned"
            raise ValueError(self.error)
        descriptor, name = tempfile.mkstemp(prefix=".native-work-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
            if _read(self.path) != data:
                raise OSError("Work ledger readback differs from its publication")
            self.raw = data
        except BaseException:
            self.error = "Work input persistence is uncertain; close and inspect without replay"
            try:
                Path(name).unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def emit(self, kind, *, origin=None, **fields):
        self.sequence += 1
        row = {"type": kind, "version": VERSION, "instance": self.instance,
               "sequence": self.sequence, "identity": copy.deepcopy(self.identity), **copy.deepcopy(fields)}
        origin = origin or EventOrigin(session_id=self.identity["session_id"], source="work")
        envelope = {"type": "gui.event", "event_id": ident(), **origin.envelope_fields(), "event": row}
        if len(encoded(envelope)) > LIMITS["envelope_bytes"]:
            self.error = "Work lifecycle event exceeded its negotiated bound"
            raise ValueError(self.error)
        try:
            self.app._rpc_emit(row, origin=origin)
        except BaseException:
            self.error = "Work lifecycle publication is uncertain; do not replay"
            raise
        return row

    def input_event(self, entry, phase, reason=None):
        entry.state, entry.reason = phase, reason
        self.persist()
        return self.emit("work_input", phase=phase, input=entry.ref, reason=reason)


def _command(app, cmd, operation, extras=(), *, admission=True, expected_identity=None):
    expected = {"version", "id", "session_id", "agent_id", "backend", "model", *extras}
    if set(cmd) - {"type"} != expected or "type" in cmd and cmd["type"] != "gui.work.native_" + operation:
        raise ValueError("Work command fields do not match its exact operation")
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Work lifecycle requires version1")
    if expected_identity is not None and admission:
        raise ValueError("Historical Work identity cannot grant execution")
    owner = identity(app) if expected_identity is None else expected_identity
    if any(cmd.get(key) != value for key, value in owner.items()):
        raise ValueError("Work command belongs to another owned identity")
    text(cmd.get("id"))
    (app._validate_owned_execution if admission else app._validate_owned_identity)()


def capture(app, cmd):
    from litetui import native_schedule
    native_schedule.capture_allowed(app)
    _command(app, cmd, "capture", tuple(k for k in ("execution_version", "runtime_controls_version") if k in cmd))
    if "runtime_controls_version" in cmd and (type(cmd["runtime_controls_version"]) is not int or cmd["runtime_controls_version"] != VERSION):
        raise ValueError("Runtime controls require exact version 1")
    if "execution_version" in cmd and (type(cmd["execution_version"]) is not int or cmd["execution_version"] != VERSION):
        raise ValueError("Explicit execution requires exact version 1")
    value = bridge(app)
    if value is None:
        from litetui import restored_input
        pending = any(not restored_input.held(item) for item in getattr(app, "_pending_input", ()))
        if app._chat_running() or pending or any(t.state == "running" for t in app.bg_tasks.values()):
            raise ValueError("Capture Work before admitting active or pending work")
        if getattr(app, "_hold_restored_input", False) is not True:
            raise ValueError("Work requires explicit restored-input hold policy")
        value = Bridge(app)
        app._native_work_bridge = value
    value.check()
    if value.active or value.activation_publication is not None:
        raise ValueError("Work capture is already active or awaiting publication")
    value.requested_execution_version = cmd.get("execution_version")
    value.requested_runtime_controls_version = cmd.get("runtime_controls_version")
    result = {"kind": "work_capture", "version": VERSION, "instance": value.instance,
              "activation_token": value.activation_token, "sequence": 0, "identity": value.identity,
              "provider": provider(app), "limits": dict(LIMITS), "recovered": copy.deepcopy(value.recovered), "deferred_jobs": deferred_evidence(app)}
    if value.requested_runtime_controls_version is not None:
        from litetui import native_runtime_controls
        result.update(native_runtime_controls.capture(app, value))
    if value.requested_execution_version is not None:
        from litetui import native_work_execution
        result.update(execution_version=VERSION, execution_receipt=native_work_execution.recovery(app))
    if len(encoded(result)) > LIMITS["reply_bytes"]:
        raise ValueError("Recovered Work snapshot exceeds reply bound; no activation")
    return result


def activate(app, cmd):
    extras = ("instance", "activation_token", *(k for k in ("scheduled_transition_version", "execution_version", "runtime_controls_version") if k in cmd))
    _command(app, cmd, "activate", extras)
    requested = cmd.get("scheduled_transition_version")
    if "scheduled_transition_version" in cmd and (type(requested) is not int or requested != VERSION):
        raise ValueError("Scheduled transitions require exact version1")
    value = bridge(app)
    if value is None or value.active or cmd.get("instance") != value.instance or cmd.get("activation_token") != value.activation_token:
        raise ValueError("Unknown or consumed Work activation")
    value.check()
    runtime = cmd.get("runtime_controls_version")
    if runtime != value.requested_runtime_controls_version or "runtime_controls_version" in cmd and (type(runtime) is not int or runtime != VERSION):
        raise ValueError("Activation changed exact runtime controls negotiation")
    execution = cmd.get("execution_version")
    if execution != value.requested_execution_version or "execution_version" in cmd and (type(execution) is not int or execution != VERSION):
        raise ValueError("Activation changed exact execution negotiation")
    deferred = getattr(app, "_native_work_deferred", None)
    if getattr(deferred, "schedule_owner", None) is not None and requested != VERSION:
        raise ValueError("Scheduled handoff activation requires negotiated response publication")
    if value.activation_publication is not None:
        raise ValueError("Work activation publication is already reserved")
    if requested is not None or execution is not None or runtime is not None:
        from litetui import native_schedule
        return native_schedule.activation(app, cmd, value)
    return _activate(app, value)


def _activate(app, value, *, scheduled_transition_version=None, execution_version=None, runtime_controls_version=None):
    value.check()
    recapture = getattr(app, "_native_work_recapture_required", None)
    if recapture is not None:
        if recapture["identity"] != value.identity or recapture["previous_instance"] == value.instance:
            raise ValueError("Work recapture changed its verified transition target")
        app._native_work_recapture_required = None
    value.active = True
    value.scheduled_transition_version = scheduled_transition_version
    value.execution_version = execution_version
    from litetui import native_work_execution
    native_work_execution.activated(app, value)
    value.runtime_controls_version = runtime_controls_version
    if runtime_controls_version is not None:
        from litetui import native_runtime_controls
        native_runtime_controls.activated(app, value)
    app._native_work_initial_activated = True
    deferred = getattr(app, "_native_work_deferred", None)
    if deferred is not None and not deferred.claimed:
        deferred.handle = asyncio.get_running_loop().call_soon(_resume_deferred, app, deferred, value)
    return {"kind": "work_active", "version": VERSION, "instance": value.instance,
            "sequence": 0, "identity": copy.deepcopy(value.identity)}


def reserve(app):
    require_recapture_complete(app)
    value = bridge(app)
    if value is None or not value.active:
        return None
    value.check()
    app._validate_owned_execution()
    active = sum(p.phase != "retired" for p in value.producers.values())
    pending = sum(i.state not in TERMINAL for i in value.inputs.values())
    uncompleted = sum(p.phase != "retired" and p.input_id is None for p in value.producers.values())
    if active + len(value.reservations) >= LIMITS["producers"] or pending + uncompleted + len(value.reservations) >= LIMITS["inputs"]:
        raise ValueError("Work completion capacity is full; no producer started")
    if len(encoded(value.document())) + (active + len(value.reservations) + 1) * (LIMITS["payload_bytes"] + 4096) > LIMITS["ledger_bytes"]:
        raise ValueError("Work durable completion capacity is full; no producer started")
    token = ident()
    value.reservations.add(token)
    return token


def release(app, token):
    value = bridge(app)
    if value is not None and token is not None:
        value.reservations.discard(token)


def register_producer(app, kind, object_id, *, owner_instance, owner_created=None, generation=1, task=None, reservation=None, call=None, queue_parent=None):
    require_recapture_complete(app)
    value = bridge(app)
    if value is None or not value.active:
        return None
    value.check()
    if kind not in {"host_task", "goal", "cron", "loop", "inbox", "child_result", "provider_question", "provider_recovery", "compact_wake", "user_queue", "user_interrupt"} or type(generation) is not int or generation < 1:
        raise ValueError("Invalid Work producer kind or generation")
    if reservation is None:
        reservation = reserve(app)
    if reservation not in value.reservations:
        raise ValueError("Work producer lacks reserved completion capacity")
    from litetui import conversation_action as action
    attempt = action._attempt(app) if action._authorized(app) else None
    origin = action.event_origin(app) if attempt is not None else current_origin(app)
    if kind in {"user_queue", "user_interrupt"}:
        owned = value.executions.get(getattr(queue_parent, "execution_id", None))
        if (attempt is not None or queue_parent is None or owned is None or owned[0].is_finished
                or owned[1] != queue_parent or owner_instance != value.instance):
            raise ValueError("Queued composer input requires its exact live turn")
        origin = queue_parent
    elif queue_parent is not None:
        raise ValueError("Unexpected foreground parent for Work producer")
    if attempt is not None and attempt.kind == "compact" and not attempt.segments and origin is not None and origin.execution_id is None:
        # A manual compact can finish before any model stream exists. Its
        # registered maintenance worker is the real causal execution; no new
        # execution identity is minted for this aftercare callback.
        execution = attempt.execution
        root = json.loads(attempt.review.wire)["root_execution_id"]
        if (execution is None or not attempt.root_started or not execution.worker.is_finished
                or execution.origin.execution_id != root or execution.origin.operation_id != attempt.operation
                or execution.origin.session_id != app.convo_id or execution.origin.source != "rpc"
                or origin.operation_id != execution.origin.operation_id or origin.session_id != execution.origin.session_id
                or not any(binding.root and binding.worker is execution.worker and binding.origin == execution.origin
                           for binding in attempt.owned or ())):
            value.reservations.discard(reservation)
            raise ValueError("Compact completion lacks its exact retired maintenance root")
        origin = execution.origin
    ref = {"producer_id": ident(), "kind": kind, "object_id": text(object_id),
           "producer_session_id": app.convo_id, "owner_instance": text(owner_instance),
           "owner_created": None if owner_created is None else text(owner_created), "generation": generation,
           "causal_action_id": json.loads(attempt.review.wire)["action_id"] if attempt else None,
           "causal_operation_id": origin.operation_id if origin else None,
           "causal_execution_id": origin.execution_id if origin else None}
    record = Producer(ref, task=task, action=attempt, call=call)
    if call is not None:
        if call not in value.calls or call.handed:
            raise ValueError("Work tool was foreign or already handed to a producer")
        call.handed = True
    value.producers[ref["producer_id"]] = record
    value.reservations.remove(reservation)
    value.persist()
    value.emit("work_producer", phase="registered", producer=ref, worker_id=None, task_state=getattr(task, "state", None), reason=None, proof=None)
    return record


def producer_runner(app, record, callback):
    if record is None:
        return callback
    async def run():
        from textual.worker import get_current_worker
        if get_current_worker() is not record.worker or record.asyncio_task is not None:
            raise ValueError("Work producer is not its exact registered worker")
        record.asyncio_task = _task()
        token = _PRODUCER.set((app, record, _task()))
        try:
            return await callback
        finally:
            _PRODUCER.reset(token)
    return run()


def bind_producer_worker(app, record, worker):
    if record is None:
        return
    value = bridge(app)
    assert value is not None
    if record.worker is not None or worker is None:
        raise ValueError("Work producer worker binding was missing or replayed")
    record.worker, record.worker_id, record.phase = worker, ident(), "running"
    value.persist()
    value.emit("work_producer", phase="running", producer=record.ref, worker_id=record.worker_id,
               task_state=getattr(record.task, "state", None), reason=None, proof=None)
    async def observe():
        from textual.worker import WorkerCancelled, WorkerFailed
        try:
            await asyncio.shield(worker.wait())
        except (WorkerCancelled, WorkerFailed):
            pass
        if record.call is not None:
            await join_call(record.call)
        for cleanup in record.kill_workers:
            await cleanup.wait()
        if not worker.is_finished:
            raise RuntimeError("Work producer did not retire")
        from litetui import tasks
        if not tasks.process_retired(getattr(record.task, "proc", None)):
            raise RuntimeError("Work producer process or pipe retirement is unproven")
        record.phase = "retired"
        value.persist()
        value.emit("work_producer", phase="retired", producer=record.ref, worker_id=record.worker_id,
                   task_state=getattr(record.task, "state", None),
                   reason=None if record.input_id else "Producer ended before emitting a completion",
                   proof={"worker_retired": True, "callback_retired": True, "process_retired": True,
                          "persistence": record.persistence})
    record.observer = _observe(observe())


def capturing_completion(app):
    scope = _PRODUCER.get()
    return bool(enabled(app) and scope is not None and scope[0] is app and scope[2] is _task()
                and scope[1].asyncio_task is _task() and bridge(app).producers.get(scope[1].ref["producer_id"]) is scope[1])


def complete_producer(app, task, *, task_saved, output_text):
    if not capturing_completion(app):
        return
    record = _PRODUCER.get()[1]
    if record.task is not task:
        raise ValueError("Work completion belongs to another task")
    from litetui import tasks
    task_reason = None
    try:
        raw = _read(Path(app.convo_dir) / tasks.STORE)
        rows = json.loads(raw) if raw else []
        if not task_saved or type(rows) is not list or [row for row in rows if row.get("id") == task.id] != [task.to_row()]:
            task_reason = "Canonical task row did not match its durable readback"
    except (OSError, ValueError, TypeError):
        task_reason = "Canonical task row readback failed"
    output_reason = None
    try:
        if not task.log:
            raise OSError("No canonical log")
        expected = output_text.replace("\n", os.linesep).encode("utf-8", errors="replace")
        size = 0
        saved_hash = hashlib.sha256()
        with Path(task.log).open("rb") as handle:
            while chunk := handle.read(65536):
                size += len(chunk)
                if size > len(expected):
                    raise OSError("Canonical output log changed")
                saved_hash.update(chunk)
        if size != len(expected) or saved_hash.hexdigest() != digest(expected):
            raise OSError("Canonical output log differs")
    except (OSError, ValueError):
        output_reason = "Canonical output log did not match its durable readback"
    record.persistence = {
        "task": {"status": "failed" if task_reason else "verified", "reason": task_reason},
        "output": {"status": "failed" if output_reason else "verified", "reason": output_reason}}



def enqueue(app, producer, item):
    value = bridge(app)
    if value is None or not value.active:
        return None
    value.check(allow_closing=True, allow_error=True)
    if not capturing_completion(app) or _PRODUCER.get()[1] is not producer:
        raise ValueError("Work completion lacks its exact producer task")
    if value.producers.get(producer.ref["producer_id"]) is not producer or producer.input_id is not None:
        raise ValueError("Work producer completion was foreign or duplicated")
    keys = ("delivery", "text", "tool_profile", "source", "history") if item.get("delivery") == "committed_history" else ("content", "text", "tool_profile", "source")
    payload = {key: copy.deepcopy(item[key]) for key in keys if key in item}
    validate_payload(payload)
    if payload.get("delivery") == "committed_history" and producer.ref["kind"] != "child_result":
        raise ValueError("Committed history requires its exact child-result producer")
    value.queue_revision += 1
    ref = {"input_id": ident(), "producer_id": producer.ref["producer_id"], "payload_sha256": digest(encoded(payload)),
           "source": payload["source"], "requested_profile": payload["tool_profile"],
           "delivery_session_id": app.convo_id, "queue_revision": value.queue_revision}
    entry = Input(ref, payload, item, producer, epoch={"instance": value.instance, "identity": copy.deepcopy(value.identity), "provider": value.provider})
    producer.input_id = ref["input_id"]
    value.inputs[ref["input_id"]] = entry
    from litetui import native_work_execution
    native_work_execution.bind_entry(app, entry)
    item["_work_input_id"] = ref["input_id"]
    value.input_event(entry, "held" if value.closing or value.error else "queued", "Runtime closing or uncertain; completion retained without wake" if value.closing or value.error else None)
    from litetui import native_work_execution
    native_work_execution.claim_entry(app, entry)
    return entry


def capture_inbox(app, item):
    if not capturing_completion(app):
        return None
    entry = enqueue(app, _PRODUCER.get()[1], item)
    if entry is not None and entry.state == "queued":
        defer_action_input(app, entry)
    return entry


def entry_for(app, item):
    value = bridge(app)
    if value is None or not value.active or "_work_input_id" not in item:
        return None
    entry = value.inputs.get(item["_work_input_id"])
    if entry is None or entry.item is not item:
        raise ValueError("Work input lacks its exact captured queue object")
    return entry


def current_origin(app):
    scope = _EXECUTION.get()
    if scope is None or scope[0] is not app or scope[1] is not _task():
        return None
    value = bridge(app)
    if value is None or scope[2].execution_id not in value.executions:
        return None
    return scope[2]


def _begin(app, entry, in_turn):
    from litetui import native_runtime_controls, native_work_execution
    if why := native_runtime_controls.mutation_hold(app):
        raise ValueError(why)
    native_work_execution.admission_allowed(app, entry)
    value = bridge(app)
    assert value is not None
    value.check()
    if entry.recovered or entry.state != "queued" or entry.claim_task is not None:
        raise ValueError("Work input is held, uncertain, or already claimed")
    if digest(encoded(entry.payload)) != entry.ref["payload_sha256"] or any(entry.item.get(k) != v for k, v in entry.payload.items()):
        raise ValueError("Work input changed before admission")
    if not app._pending_input or app._pending_input[0] is not entry.item:
        raise ValueError("Only the oldest exact queued Work input may be claimed")
    if in_turn and (entry.producer.ref["kind"] == "user_interrupt"
                    or value.provider == "claude" and entry.producer.ref["kind"] == "user_queue"):
        raise ValueError("Interrupting text requires a fresh turn after the interrupted worker retires")
    if in_turn and current_origin(app) is None:
        raise ValueError("Safe-boundary Work admission lacks its exact execution")
    entry.claim_task = _task()
    value.input_event(entry, "claiming")
    delivery = {**entry.item, **entry.payload,
                "operation_id": current_origin(app).operation_id if in_turn else ident()}
    if in_turn:
        delivery["_gui_in_turn"] = True
    return delivery


def _persist_user(app, entry, message):
    value = bridge(app)
    assert value is not None
    value.check()
    if entry.claim_task is not _task() or entry.state != "claiming" or entry.appended:
        raise ValueError("Work append lacks its exact single-use claimant")
    message = {**message, "work_input_id": entry.ref["input_id"], "work_payload_sha256": entry.ref["payload_sha256"]}
    app._append(message)
    from litetui.conversation import ConversationRepository
    meta, rows = ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)
    matches = [row for row in rows if row.get("work_input_id") == entry.ref["input_id"]]
    if meta.get("id") != app.convo_id or matches != [message] or getattr(app.store, "persist_error", None):
        raise OSError("Work input did not append exactly once to its owned transcript")
    entry.appended = True
    entry.provider_delivery = message.get("claude_delivery") or message.get("codex_delivery")


def _finish_admission(app, entry, accepted, in_turn):
    value = bridge(app)
    assert value is not None
    if accepted is None:
        value.input_event(entry, "held", "Canonical admission refused; input retained in original FIFO position")
        return False
    if not entry.appended or not app._pending_input or app._pending_input[0] is not entry.item:
        raise ValueError("Work admission lost its exact queue claim or append proof")
    if in_turn:
        origin = current_origin(app)
        assert origin is not None
        _announce(app, entry, origin, "in_turn")
        app._pending_input.pop(0)
        entry.worker = value.executions[origin.execution_id][0]
        _watch_admission(app, entry)
    else:
        token = _ADMISSION.set((app, entry, _task()))
        try:
            worker = app._stream(origin=accepted)
            if entry.admission is None or worker is None or entry.worker is not worker:
                raise RuntimeError("Work admission did not bind its scheduled worker")
        finally:
            _ADMISSION.reset(token)
        app._pending_input.pop(0)
    return True


def _uncertain(app, entry):
    value = bridge(app)
    assert value is not None
    entry.state, entry.reason = "uncertain", "Canonical Work admission is uncertain; never replay automatically"
    try:
        value.persist()
        value.emit("work_input", phase="uncertain", input=entry.ref, reason=entry.reason)
    finally:
        if value.error is None or value.uncertain_error_owned and value.error == entry.reason:
            value.uncertain_inputs.add(entry.ref["input_id"])
            value.uncertain_error_owned = True
            value.error = entry.reason


def admit_sync(app, item, *, in_turn=False):
    entry = entry_for(app, item)
    if entry is None:
        return None
    if entry.state != "queued":
        return False
    from litetui import hook_host
    delivery = _begin(app, entry, in_turn)
    try:
        accepted = hook_host.accept_prompt_origin(app, delivery, on_refused=lambda _reason: None,
                    persist_user=lambda message: _persist_user(app, entry, message))
        native = item.get("_codex_entry")
        if accepted is not None and native is not None and native.get("state") in {"admitted", "next_turn"}:
            from dataclasses import replace
            app._hook_turn_id = native.get("admission", {}).get("turn_id", app._hook_turn_id)
            accepted = replace(accepted, hook_id=app._hook_turn_id)
        if accepted is not None and entry.producer.ref["kind"] == "compact_wake":
            app._hooks_suppressed = True
        return _finish_admission(app, entry, accepted, in_turn)
    except BaseException:
        _uncertain(app, entry)
        raise
    finally:
        entry.claim_task = None


async def admit_async(app, item, *, in_turn=False):
    entry = entry_for(app, item)
    if entry is None:
        return None
    if entry.state != "queued":
        return False
    from litetui import hook_host
    delivery = _begin(app, entry, in_turn)
    try:
        def before():
            value = bridge(app)
            assert value is not None
            value.check()
            if not app._pending_input or app._pending_input[0] is not item:
                raise ValueError("Work queue changed while hooks were pending")
        accepted = await hook_host.admit_prompt_origin(app, delivery, on_refused=lambda _reason: None,
                       before_accept=before, persist_user=lambda message: _persist_user(app, entry, message))
        return _finish_admission(app, entry, accepted, in_turn)
    except BaseException:
        _uncertain(app, entry)
        raise
    finally:
        entry.claim_task = None


def _announce(app, entry, origin, mode):
    from litetui import seat_authority
    value = bridge(app)
    assert value is not None
    value.check()
    boundary = 0
    parent = None
    if mode == "in_turn":
        parent = origin.execution_id
        boundary = value.boundaries.get(parent, 0) + 1
        value.boundaries[parent] = boundary
    elif mode == "action_segment":
        action = entry.producer.action
        parent = action.segments[-1] if action.segments else entry.producer.ref["causal_execution_id"]
    native = entry.provider_delivery
    native = {"id": native["id"], "state": native["state"]} if native else None
    admission = {"admission_id": ident(), "input_id": entry.ref["input_id"], "mode": mode,
                 "operation_id": origin.operation_id, "origin": origin.envelope_fields(),
                 "parent_execution_id": parent, "boundary": boundary,
                 "requested_profile": entry.ref["requested_profile"], "effective_profile": app._active_tool_profile,
                 "seat_profile": seat_authority.seat_profile(app), "provider": provider(app), "provider_delivery": native}
    entry.admission, entry.state = admission, "admitted"
    value.persist()
    value.emit("work_admission", origin=origin, status="accepted", input=entry.ref, admission=admission, payload=entry.payload)


def before_stream(app, origin):
    from litetui import native_runtime_controls
    if why := native_runtime_controls.mutation_hold(app):
        raise ValueError(why)
    require_recapture_complete(app)
    value = bridge(app)
    if value is not None and value.active:
        value.check()
    scope = _ADMISSION.get()
    if scope is None or scope[0] is not app or scope[2] is not _task():
        return
    entry = scope[1]
    from litetui import conversation_action as action
    mode = "action_segment" if action._authorized(app) and entry.producer.action is action._attempt(app) else "new_turn"
    _announce(app, entry, origin, mode)


def bind_stream(app, worker, origin):
    value = bridge(app)
    if value is None or not value.active:
        return
    value.executions[origin.execution_id] = (worker, origin)
    scope = _ADMISSION.get()
    if scope is not None and scope[0] is app and scope[2] is _task():
        entry = scope[1]
        entry.worker = worker
        _watch_admission(app, entry)


def stream_runner(app, origin, callback):
    if not enabled(app):
        return callback
    async def run():
        value = bridge(app)
        assert value is not None
        from textual.worker import get_current_worker
        if value.executions.get(origin.execution_id, (None,))[0] is not get_current_worker():
            raise ValueError("Work execution lacks its exact registered worker")
        if value.closing or value.error:
            entry = next((item for item in value.inputs.values() if item.worker is get_current_worker()
                and item.admission is not None and item.admission["origin"] == origin.envelope_fields()), None)
            if entry is not None:
                # Accepted and durably appended, but Quit won before the body.
                # Preserve the claim and prove cancellation without turn_start.
                _uncertain(app, entry)
                raise asyncio.CancelledError
        token = _EXECUTION.set((app, _task(), origin))
        try:
            return await callback()
        finally:
            _EXECUTION.reset(token)
    return run


def _watch_admission(app, entry):
    if entry.observer is not None:
        raise ValueError("Work admission observer was already bound")
    value = bridge(app)
    assert value is not None
    async def observe():
        from textual.worker import WorkerCancelled, WorkerFailed
        outcome = "completed"
        try:
            result = await asyncio.shield(entry.worker.wait())
            from litetui.turn_deferral import STREAM_DEFERRED
            if result is STREAM_DEFERRED:
                outcome = "deferred"
        except WorkerCancelled:
            outcome = "cancelled"
        except WorkerFailed:
            outcome = "failed"
        if not entry.worker.is_finished:
            raise RuntimeError("Work admission worker is still live")
        if entry.producer.observer is not None:
            await asyncio.shield(entry.producer.observer)
        if entry.provider_lane is not None:
            await asyncio.shield(entry.provider_lane.observer)
        from litetui.conversation import ConversationRepository
        _meta, rows = ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)
        durable = entry.appended or entry.committed_verified
        if entry.committed_verified and not entry.committed_finished and outcome == "completed":
            outcome = "failed"  # The callback retired without a durable receipt disposition.
        if rows != app.conversation or not durable:
            raise RuntimeError("Work durable history differs from its exact admitted runtime")
        if entry.admission_worker is not None:
            await entry.admission_worker.wait()
        if getattr(app, "_conversation_transition_cleanup_error", None):
            raise RuntimeError("Work hook cleanup is unproven")
        entry.state = "settled"
        value.persist()
        app._pending_input[:] = [item for item in app._pending_input if item is not entry.item]
        assert entry.admission is not None
        origin = EventOrigin(**entry.admission["origin"])
        entry.receipt = value.emit("work_settled", origin=origin, input=entry.ref, admission=entry.admission,
                    outcome=outcome, proof={"worker_retired": True, "callbacks_retired": True,
                    "streams_retired": True, "input_durable": True}, history_sha256=digest(encoded(rows)),
                    ledger_sha256=digest(value.raw))
    entry.observer = _observe(observe())


async def settled(app, cmd):
    _command(app, cmd, "settled", ("instance", "admission_id"), admission=False)
    value = bridge(app)
    if value is None or not value.active or cmd.get("instance") != value.instance:
        raise ValueError("Unknown Work lifecycle instance")
    entry = next((i for i in value.inputs.values() if i.admission and i.admission["admission_id"] == cmd.get("admission_id") and not i.recovered), None)
    if entry is None or entry.observer is None:
        raise ValueError("Unknown Work admission settlement")
    await asyncio.shield(entry.observer)
    value.check(allow_closing=True, allow_error=True)
    if _read(value.path) != value.raw:
        raise ValueError("Work settlement ledger changed outside its owner")
    if entry.receipt is None:
        raise ValueError("Work admission has no settled receipt")
    return copy.deepcopy(entry.receipt)


def begin_close(app):
    from litetui import native_runtime_controls
    native_runtime_controls.begin_close(app)
    cancel_deferred(app)
    value = bridge(app)
    if value is None or not value.active:
        return None
    if value.close_event is not None:
        return value.close_event
    value.closing = True
    value.close_event = value.emit("work_closing", cutoff_sequence=value.sequence + 1,
        producer_ids=[p.ref["producer_id"] for p in value.producers.values() if p.phase != "retired"],
        input_ids=[i.ref["input_id"] for i in value.inputs.values() if i.state not in TERMINAL],
        admission_ids=[i.admission["admission_id"] for i in value.inputs.values() if i.admission is not None and i.receipt is None and not i.recovered])
    return value.close_event


async def settle_before_shutdown(app):
    cutoff = begin_close(app)
    from litetui import native_dictation, native_playback
    await native_playback.retire(app, closing=True)
    await native_dictation.retire(app, closing=True)
    if cutoff is None:
        return None
    value = bridge(app)
    assert value is not None
    if value.shutdown_task is None:
        value.shutdown_task = _observe(_settle_shutdown(app, value, cutoff))
    from litetui.shutdown_wait import join_task
    proof = await join_task(value.shutdown_task)
    from textual.worker import WorkerCancelled, WorkerFailed
    for worker in list(getattr(app, "_native_work_jobs", {}).values()):
        try:
            await worker.wait()
        except (WorkerCancelled, WorkerFailed):
            pass
        if not worker.is_finished:
            raise RuntimeError("Scheduled invocation worker remains live")
    deferred = getattr(app, "_native_work_deferred", None)
    if deferred is not None and deferred.worker is not None:
        await deferred.worker.wait()
    return copy.deepcopy(proof)


async def _settle_shutdown(app, value, cutoff, *, cancel=True):
    from textual.worker import WorkerCancelled, WorkerFailed
    # Closing prevents new producers. Existing callbacks may still publish their
    # one completion; each late input is retained and included in the final set.
    for call in value.calls:
        if cancel:
            call.cancelled.set()
    for entry in list(value.inputs.values()):
        if cancel and entry.admission_worker is not None and not entry.admission_worker.is_finished:
            entry.admission_worker.cancel()
    for record in list(value.producers.values()):
        if cancel and record.worker is not None and not record.worker.is_finished:
            task = record.task
            if task is None:
                # Goal evaluators, inbox claims and scheduled/child callbacks
                # own an exact Textual worker without a canonical host Task row.
                # Quit cancels it; quiet identity transitions naturally join it.
                record.worker.cancel()
            elif task.state == "running":
                reason = app._kill_background(task.id)
                if reason:
                    raise RuntimeError(reason)
    # Existing turn owners run canonical cancellation and close streams. A Work
    # admission never outlives its exact worker even if the caller is cancelled.
    for entry in list(value.inputs.values()):
        if cancel and entry.worker is not None and not entry.worker.is_finished:
            entry.worker.cancel()
    for record in list(value.producers.values()):
        if record.observer is not None:
            await asyncio.shield(record.observer)
        elif record.phase != "retired":
            raise RuntimeError("Work producer has no retirement observer")
    for call in value.calls:
        await join_call(call)
    for entry in list(value.inputs.values()):
        if entry.admission_worker is not None:
            try:
                await entry.admission_worker.wait()
            except (WorkerCancelled, WorkerFailed):
                pass
        if entry.observer is not None:
            await asyncio.shield(entry.observer)
        elif entry.state not in TERMINAL | {"held", "uncertain"}:
            value.input_event(entry, "held", "Runtime closed before admission; input retained")
    if getattr(app, "_conversation_transition_cleanup_error", None):
        raise RuntimeError("Work hook process or pipe retirement is unproven")
    if getattr(app, "_hook_lifecycle", ()) or app._hook_lock.locked():
        raise RuntimeError("Work lifecycle hooks remain pending")
    if app.store.pending and value.raw is None:
        # A fresh conversation has no store lease or ledger to flush. Joining an
        # unused bridge must not materialize a conversation just to close it.
        value.check(allow_closing=True)
        if (value.revision or value.producers or value.inputs or value.calls or value.reservations
                or value.executions or _read(value.path) is not None):
            raise ValueError("Pending conversation has unexpected Work ledger activity")
        ledger_hash = digest(b"")
    else:
        value.persist()
        ledger_hash = digest(value.raw)
    ids = set(cutoff["input_ids"])
    ids.update(i.ref["input_id"] for i in value.inputs.values() if i.ref["producer_id"] in cutoff["producer_ids"])
    return {"version": VERSION, "instance": value.instance, "closing_sequence": cutoff["sequence"],
            "final_sequence": value.sequence, "admission_closed": True, "worker_settled": True,
            "callback_settled": True, "process_settled": True, "producer_ids": cutoff["producer_ids"],
            "inputs": [{"input_id": key, "disposition": value.inputs[key].state} for key in sorted(ids)],
            "admission_ids": cutoff["admission_ids"], "ledger_sha256": ledger_hash, "effects_verified": False}


def action_queue_owned(app, attempt):
    value = bridge(app)
    if value is None or not value.active:
        return False
    return all((entry := entry_for(app, item)) is not None and entry.producer.action is attempt
               and entry.state in {"queued", "held"} and not entry.recovered for item in app._pending_input)


def action_profile_owned(app, attempt, expected):
    if getattr(app, "_active_tool_profile", None) == expected:
        return True
    value = bridge(app)
    return bool(value is not None and value.active and any(entry.producer.action is attempt and entry.admission
                and entry.admission["effective_profile"] == app._active_tool_profile for entry in value.inputs.values()))


def defer_action_input(app, entry):
    attempt = entry.producer.action
    if attempt is None or not attempt.pending:
        return
    from litetui import conversation_action as action
    if action._attempt(app) is not attempt or not capturing_completion(app):
        raise ValueError("Background continuation lacks its exact captured producer")
    assert attempt is not None
    if attempt.callbacks is None:
        attempt.callbacks = []
    if attempt.scheduled_callbacks >= action.MAX_CALLBACKS:
        raise ValueError("Captured background continuation budget exceeded")
    attempt.scheduled_callbacks += 1
    attempt.callbacks.append((app._flush_pending_input, ()))


def start_prompt(app, item):
    entry = entry_for(app, item)
    if entry is None:
        return None
    from litetui import conversation_action as action
    from litetui import hook_host
    if entry.producer.action is not None and entry.producer.action.pending and not action._authorized(app):
        return False  # Exact aftercare callback already retained by enqueue.
    if entry.state != "queued":
        return False
    if entry.producer.action is not None and entry.producer.action.stopped:
        bridge(app).input_event(entry, "held", "Captured action stopped before continuation; input retained")
        return False
    if bridge(app).pending_readbacks:
        return False
    if entry.producer.ref["kind"] == "compact_wake":
        return admit_sync(app, item)  # Canonical compaction wake suppresses prompt hooks.
    native = item.get("_codex_entry")
    if native is not None and native.get("state") in {"admitted", "next_turn"}:
        return admit_sync(app, item)  # Canonical native admission already ran its hooks.
    state = hook_host.snapshot(app)
    if not state.hooks and not state.error:
        return admit_sync(app, item)
    if entry.admission_worker is not None:
        return entry.admission_worker
    async def deliver():
        return await admit_async(app, item)
    if entry.producer.action is not None and entry.producer.action.pending:
        action._work_budget(app)
        origin = action.event_origin(app)
        runner = action._owned_runner(app, entry.producer.action, "continuation", origin, deliver)
        worker = app.run_worker(runner, group="work-admission", exclusive=False, exit_on_error=False)
        action._track_owned(app, worker, "continuation", origin)
    else:
        worker = app.run_worker(deliver(), group="work-admission", exclusive=False, exit_on_error=False)
    entry.admission_worker = worker
    return worker


def inline_runner(app, origin, callback):
    if not enabled(app):
        return callback
    async def run():
        from textual.worker import get_current_worker
        value = bridge(app)
        assert value is not None
        value.executions[origin.execution_id] = (get_current_worker(), origin)
        token = _EXECUTION.set((app, _task(), origin))
        try:
            return await callback
        finally:
            _EXECUTION.reset(token)
    return run()


def preflight_task(app, name, args):
    require_recapture_complete(app)
    if not enabled(app):
        return
    from litetui import paths, tasks
    # Both text and content are retained. Budget worst-case four-byte Unicode
    # across the canonical excerpt, plus exact labels/path and bounded metadata.
    label = tasks.label_of(name, args)
    path = tasks.log_path(tasks.Task("t-" + "f" * 32, name, label, app.convo_id, 0), paths.data_root())
    overhead = 2 * len((str(path) + label + name).encode("utf-8")) + 8192
    if overhead + 12 * (tasks.HEAD_CHARS + tasks.TAIL_CHARS + 40) > LIMITS["payload_bytes"]:
        raise ValueError("Canonical task completion cannot fit its reserved input; no tool started")


@dataclass(eq=False)
class ToolCall:
    owner: Any = None
    observer: bool = False
    retired: Any = field(default_factory=threading.Event)
    cancelled: Any = field(default_factory=threading.Event)
    lock: Any = field(default_factory=threading.Lock)
    claimed: bool = False
    future: Any = None
    handed: bool = False
    authorization_child: Any = None


def capture_tool(app, callback, *, allow_error=False):
    return _capture_callback(app, callback, execution=True, allow_error=allow_error)


def capture_observer(app, callback):
    """Join an owned control callback without granting prompt/tool admission."""
    return _capture_callback(app, callback, execution=False, allow_error=True)


def _capture_callback(app, callback, *, execution, allow_error):
    require_recapture_complete(app)
    value = bridge(app)
    if value is None or not value.active:
        return callback, None
    value.check(allow_error=allow_error)
    if execution:
        app._validate_owned_execution()
    observer = not execution
    cap = 1 if observer else LIMITS["producers"]
    if sum(not call.retired.is_set() and call.observer == observer for call in value.calls) >= cap:
        raise ValueError("Work observer callback capacity exhausted" if observer else "Work callback capacity exhausted before execution")
    call = ToolCall(owner=value, observer=observer)
    value.calls.append(call)
    def invoke(*args, **kwargs):
        with call.lock:
            if call.claimed:
                raise ValueError("Work callback cannot be replayed")
            call.claimed = True
        try:
            if _task() is not None or call.cancelled.is_set():
                raise asyncio.CancelledError
            from litetui.question_result import question_lifetime
            with question_lifetime(call.cancelled):
                return callback(*args, **kwargs)
        finally:
            with call.lock:
                call.retired.set()
    return invoke, call


def track_tool(call, awaitable):
    if call is None:
        return awaitable
    async def start():
        if call.future is not None:
            raise ValueError("Work tool future cannot be replayed")
        if call.cancelled.is_set():
            call.retired.set()
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise asyncio.CancelledError
        call.future = asyncio.ensure_future(awaitable)
        try:
            return await asyncio.shield(call.future)
        except asyncio.CancelledError:
            call.cancelled.set()
            await join_call(call)
            raise
    return start()


async def join_call(call):
    from litetui.shutdown_wait import join_task
    if call.future is None:
        with call.lock:
            call.cancelled.set()
            if not call.claimed:
                call.retired.set()  # Never invoked; cancellation prevents any later call.
        if not call.retired.is_set():
            raise RuntimeError("Work callback ran without its registered join future")
        if call.authorization_child is not None:
            call.authorization_child.close()
        return
    try:
        await join_task(call.future)
    except Exception as exc:  # noqa: BLE001 - canonical caller retains its actual tool error/result
        from litetui import runtime_log
        runtime_log.record("work.tool_settled_error", error=type(exc).__name__)
    if not call.future.done() or not call.retired.is_set():
        raise RuntimeError("Work tool callback has not retired")
    if call.authorization_child is not None:
        call.authorization_child.close()


async def finish_tool(call):
    if call is not None and not call.handed:
        if _task() is not None and _task().cancelling():
            call.cancelled.set()
        await join_call(call)
        # Foreground results have no producer owner. Keeping their completed
        # futures here would retain every tool result for this entire epoch.
        # Closing joins this exact list; never change its iteration underneath it.
        owner = call.owner
        if owner is not None and not owner.closing and not call.observer and call.retired.is_set():
            for index, retained in enumerate(owner.calls):
                if retained is call:
                    del owner.calls[index]
                    call.owner = None
                    break


def track_kill(app, task, worker):
    value = bridge(app)
    if value is None or not value.active:
        return
    record = next((p for p in value.producers.values() if p.task is task), None)
    if record is not None:
        record.kill_workers.append(worker)
        if record.phase != "stop_requested":
            record.phase = "stop_requested"
            value.persist()
            value.emit("work_producer", phase="stop_requested", producer=record.ref,
                       worker_id=record.worker_id, task_state=task.state,
                       reason="Canonical task Stop requested", proof=None)



def _keys(value, required, optional=()):
    if type(value) is not dict or set(value) - set(optional) != set(required):
        raise ValueError("Malformed Work ledger fields")


def _positive(value, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError("Malformed Work ledger integer")


def _hash(value):
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Malformed Work ledger digest")


def validate_document(document):
    from litetui.tool_policy import PROFILES
    _keys(document, ("version", "identity", "instance", "provider", "revision", "producers", "inputs"))
    if type(document["version"]) is not int or document["version"] != VERSION:
        raise ValueError("Unsupported Work ledger version")
    _keys(document["identity"], ("session_id", "agent_id", "backend", "model"))
    owner = document["identity"]
    canonical_id(owner["session_id"])
    canonical_id(owner["agent_id"])
    text(owner["backend"], 64)
    text(owner["model"], 1024)
    _hash(document["instance"])
    if document["provider"] not in {"host", "codex", "claude"}:
        raise ValueError("Malformed Work provider identity")
    _positive(document["revision"], zero=True)
    if type(document["producers"]) is not list or type(document["inputs"]) is not list:
        raise ValueError("Malformed Work ledger records")
    producers = {}
    for row in document["producers"]:
        _keys(row, ("producer", "phase"))
        p = row["producer"]
        _keys(p, ("producer_id", "kind", "object_id", "producer_session_id", "owner_instance", "owner_created", "generation", "causal_action_id", "causal_operation_id", "causal_execution_id"))
        canonical_id(p["producer_id"])
        canonical_id(p["producer_session_id"])
        if p["producer_id"] in producers or p["producer_session_id"] != owner["session_id"]:
            raise ValueError("Duplicate or foreign Work producer")
        if p["kind"] not in {"host_task", "goal", "cron", "loop", "inbox", "child_result", "provider_question", "provider_recovery", "compact_wake", "user_queue", "user_interrupt"} or row["phase"] not in {"registered", "running", "stop_requested", "retired"}:
            raise ValueError("Malformed Work producer state")
        for key in ("object_id", "owner_instance"):
            text(p[key])
        if p["owner_created"] is not None:
            text(p["owner_created"])
        _positive(p["generation"])
        for key in ("causal_action_id", "causal_execution_id"):
            if p[key] is not None:
                canonical_id(p[key])
        if p["causal_operation_id"] is not None:
            text(p["causal_operation_id"])
        if p["kind"] in {"user_queue", "user_interrupt"}:
            canonical_id(p["object_id"])
            _hash(p["owner_instance"])
            if (p["generation"] != 1 or p["owner_created"] is not None or p["causal_action_id"] is not None
                    or not p["causal_operation_id"] or not p["causal_execution_id"]):
                raise ValueError("Queued composer input lost its foreground owner")
        producers[p["producer_id"]] = p
    seen, used_producers, revisions, admissions = set(), set(), set(), set()
    pending = 0
    for row in document["inputs"]:
        _keys(row, ("input", "payload", "state", "reason", "admission", "epoch"), ("reconciliation",))
        epoch = row["epoch"]
        _keys(epoch, ("instance", "identity", "provider"))
        _hash(epoch["instance"])
        _keys(epoch["identity"], ("session_id", "agent_id", "backend", "model"))
        if any(epoch["identity"][key] != owner[key] for key in ("session_id", "agent_id")):
            raise ValueError("Foreign recovered Work epoch")
        text(epoch["identity"]["backend"], 64)
        text(epoch["identity"]["model"], 1024)
        if epoch["provider"] not in {"host", "codex", "claude"}:
            raise ValueError("Malformed recovered Work provider")
        i, payload = row["input"], row["payload"]
        _keys(i, ("input_id", "producer_id", "payload_sha256", "source", "requested_profile", "delivery_session_id", "queue_revision"))
        for key in ("input_id", "producer_id", "delivery_session_id"):
            canonical_id(i[key])
        _hash(i["payload_sha256"])
        text(i["source"])
        _positive(i["queue_revision"])
        if (i["input_id"] in seen or i["producer_id"] in used_producers or i["queue_revision"] in revisions
                or i["producer_id"] not in producers or i["delivery_session_id"] != owner["session_id"]):
            raise ValueError("Duplicate or foreign Work input")
        seen.add(i["input_id"])
        used_producers.add(i["producer_id"])
        revisions.add(i["queue_revision"])
        validate_payload(payload)
        producer = producers[i["producer_id"]]
        if producer["kind"] in {"user_queue", "user_interrupt"} and (
                (epoch["provider"] != "host" and not (epoch["provider"] == "claude" and producer["kind"] == "user_queue"))
                or epoch["instance"] != producer["owner_instance"]
                or set(payload) != {"content", "text", "source", "tool_profile"} or payload["source"] != "rpc"
                or type(payload["content"]) is not str or payload["text"] != payload["content"]):
            raise ValueError("Queued composer input lost its captured text or host epoch")
        if payload.get("delivery") == "committed_history" and (
                producers[i["producer_id"]]["kind"] != "child_result"
                or payload["history"]["session_id"] != owner["session_id"]):
            raise ValueError("Foreign committed-history producer or session")
        if (i["requested_profile"] not in PROFILES or payload["tool_profile"] != i["requested_profile"]
                or payload["source"] != i["source"] or len(encoded(payload)) > LIMITS["payload_bytes"]
                or digest(encoded(payload)) != i["payload_sha256"]):
            raise ValueError("Recovered Work payload changed")
        if row["state"] not in {"queued", "claiming", "admitted", "held", "uncertain", "cancelled", "settled", "reconciled"}:
            raise ValueError("Malformed Work input state")
        if row["reason"] is not None:
            text(row["reason"], 1024)
        pending += row["state"] not in TERMINAL
        evidence = row.get("reconciliation")
        if row["state"] == "reconciled":
            validate_reconciliation(evidence, i, epoch)
        elif evidence is not None:
            raise ValueError("Unresolved Work input has premature reconciliation evidence")
        a = row["admission"]
        if a is None:
            if row["state"] in {"admitted", "settled"}:
                raise ValueError("Work admission evidence is missing")
            continue
        _keys(a, ("admission_id", "input_id", "mode", "operation_id", "origin", "parent_execution_id", "boundary", "requested_profile", "effective_profile", "seat_profile", "provider", "provider_delivery"))
        canonical_id(a["admission_id"])
        if a["admission_id"] in admissions or a["input_id"] != i["input_id"]:
            raise ValueError("Duplicate or foreign Work admission")
        admissions.add(a["admission_id"])
        text(a["operation_id"])
        if a["mode"] not in {"new_turn", "in_turn", "action_segment"} or a["provider"] != epoch["provider"]:
            raise ValueError("Malformed Work admission mode")
        for key in ("requested_profile", "effective_profile", "seat_profile"):
            if a[key] not in PROFILES:
                raise ValueError("Malformed Work admission profile")
        if a["requested_profile"] != i["requested_profile"]:
            raise ValueError("Work admission changed requested profile")
        origin = a["origin"]
        _keys(origin, ("session_id", "operation_id", "hook_id", "source", "execution_id"))
        if origin["session_id"] != owner["session_id"] or origin["operation_id"] != a["operation_id"]:
            raise ValueError("Foreign Work admission origin")
        canonical_id(origin["execution_id"])
        if (producer["kind"] == "user_interrupt" or producer["kind"] == "user_queue" and epoch["provider"] == "claude") and (a["mode"] != "new_turn"
                or a["operation_id"] == producer["causal_operation_id"]
                or origin["execution_id"] == producer["causal_execution_id"]):
            raise ValueError("Interrupt delivery did not begin a fresh turn")
        if origin["hook_id"] is not None:
            canonical_id(origin["hook_id"])
        text(origin["source"])
        _positive(a["boundary"], zero=True)
        if a["parent_execution_id"] is not None:
            canonical_id(a["parent_execution_id"])
        if a["mode"] == "new_turn" and (a["parent_execution_id"] is not None or a["boundary"] != 0 or origin["source"] != i["source"]):
            raise ValueError("Malformed new-turn Work admission")
        if a["mode"] == "in_turn" and (a["parent_execution_id"] != origin["execution_id"] or a["boundary"] < 1):
            raise ValueError("Malformed in-turn Work admission")
        delivery = a["provider_delivery"]
        if delivery is not None:
            _keys(delivery, ("id", "state"))
            text(delivery["id"])
            if delivery["state"] not in {"prepared", "pending", "accepted"}:
                raise ValueError("Malformed provider delivery evidence")
    if pending > LIMITS["inputs"]:
        raise ValueError("Recovered Work inputs exceed capacity")



async def before_identity_transition(app):
    """Join a quiet epoch naturally after the caller's canonical preflight."""
    value = bridge(app)
    if value is None or not value.active:
        return None
    value.check(allow_closing=True)
    if value.closing and getattr(value, "transition_proof", None) is not None:
        return copy.deepcopy(value.transition_proof)
    if app._chat_running() or app._pending_input or any(i.claim_task is not None for i in value.inputs.values()):
        raise ValueError("Work identity transition requires the existing quiet queue boundary")
    from litetui import native_work_execution
    if why := native_work_execution.mutation_hold(app):
        raise ValueError(why)
    # A human identity control can race an already emitted scheduler proposal.
    # Retain only its exact no-effect decision capability through the old close.
    from litetui import native_schedule
    from litetui import native_runtime_controls
    if why := native_runtime_controls.mutation_hold(app):
        raise ValueError(why)
    native_schedule.retire_proposal(app, value)
    cutoff = begin_close(app)
    if value.shutdown_task is None:
        value.shutdown_task = _observe(_settle_shutdown(app, value, cutoff, cancel=False))
    from litetui.shutdown_wait import join_task
    proof = await join_task(value.shutdown_task)
    value.runtime_controls_retirement = await native_runtime_controls.shutdown(app)
    value.transition_proof = copy.deepcopy(proof)
    return copy.deepcopy(proof)


def after_identity_transition(app, proof):
    if proof is None:
        return {}
    value = bridge(app)
    if (value is None or not value.closing or getattr(value, "transition_proof", None) != proof
            or value.shutdown_task is None or not value.shutdown_task.done()):
        raise ValueError("Work identity transition lacks exact old-epoch settlement")
    if app._agent_session is not value.owner[0] or identity(app)["agent_id"] != value.identity["agent_id"]:
        raise ValueError("Work identity transition changed its owned agent")
    app._validate_owned_identity()
    # Existing ledger partitions remain on disk under their owned conversation;
    # unresolved records preserve their original epoch when subsequently read.
    from litetui import native_work_execution
    from litetui import native_runtime_controls
    runtime = native_runtime_controls.retired(app, value)
    execution = native_work_execution.identity_retired(app, value)
    app._native_work_bridge = None
    app._native_work_recapture_required = {"previous_instance": value.instance, "identity": identity(app)}
    return {"work_shutdown": copy.deepcopy(proof), "work_recapture_required": True, **execution, **runtime}



DEFERRED_FILENAME = "native-work-deferred-job.json"


@dataclass(eq=False)
class DeferredJob:
    document: dict
    raw: bytes
    session: Any
    store: Any
    path: Path
    job: Any
    claimed: bool = False
    handle: Any = None
    worker: Any = None
    schedule_owner: Any = None


def _write_deferred(app, document, expected):
    if not app.store.owned or app.store.pending:
        raise ValueError("Deferred job requires its materialized owned target conversation")
    app._validate_owned_identity()
    owner = (app._agent_session, app.store, Path(app.convo_dir), identity(app))
    path = owner[2] / DEFERRED_FILENAME
    if _read(path, LIMITS["reply_bytes"]) != expected:
        raise ValueError("Deferred job journal changed outside its exact owner")
    raw = encoded(document)
    if len(raw) > LIMITS["reply_bytes"]:
        raise ValueError("Deferred job exceeds bounded durable evidence")
    descriptor, name = tempfile.mkstemp(prefix=".native-work-job-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        # The conversation lease excludes cooperating writers. Recheck the
        # exact bytes immediately before replacement to retain detected drift;
        # this is not an OS compare-and-swap against arbitrary external writers.
        app._validate_owned_identity()
        if (app._agent_session is not owner[0] or app.store is not owner[1]
                or Path(app.convo_dir) != owner[2] or identity(app) != owner[3]
                or not app.store.owned or app.store.pending
                or _read(path, LIMITS["reply_bytes"]) != expected):
            raise ValueError("Deferred job journal or owner changed before replacement")
        os.replace(name, path)
        if _read(path, LIMITS["reply_bytes"]) != raw:
            raise OSError("Deferred job readback differs")
    finally:
        Path(name).unlink(missing_ok=True)
    return raw


def deferred_document(app):
    """Return exact validated disk bytes and document, without display projection."""
    path = Path(app.convo_dir) / DEFERRED_FILENAME
    raw = _read(path, LIMITS["reply_bytes"])
    if raw is None:
        return None
    document = strict_json(raw)
    if type(document) is not dict or type(document.get("version")) is not int:
        raise ValueError("Malformed deferred job journal version")
    version = document["version"]
    if version == 1:
        _keys(document, ("version", "state", "input_id", "record"))
        if document["state"] not in {"pending", "delivered", "cancelled", "uncertain"}:
            raise ValueError("Malformed deferred job journal")
    elif version == 2:
        _keys(document, ("version", "state", "input_id", "record", "retirement"))
        if document["state"] != "discarded":
            raise ValueError("Deferred journal version 2 is terminal only")
    else:
        raise ValueError("Unknown deferred job journal version")
    row = document["record"]
    _keys(row, ("invocation_id", "job_id", "kind", "generation", "source", "requested_profile", "payload", "job_snapshot", "job_sha256", "epoch", "target", "shutdown", "state", "reason"))
    canonical_id(row["invocation_id"])
    text(row["job_id"])
    _positive(row["generation"])
    _keys(row["target"], ("session_id", "agent_id"))
    if row["target"] != {key: identity(app)[key] for key in ("session_id", "agent_id")}:
        raise ValueError("Deferred job belongs to another owned target")
    epoch = row["epoch"]
    _keys(epoch, ("instance", "identity", "provider"))
    _hash(epoch["instance"])
    _keys(epoch["identity"], ("session_id", "agent_id", "backend", "model"))
    canonical_id(epoch["identity"]["session_id"])
    if epoch["identity"]["agent_id"] != row["target"]["agent_id"] or epoch["provider"] not in {"host", "codex", "claude"}:
        raise ValueError("Foreign deferred job producer epoch")
    text(epoch["identity"]["backend"], 64)
    text(epoch["identity"]["model"], 1024)
    text(row["job_snapshot"], LIMITS["payload_bytes"])
    if digest(row["job_snapshot"].encode("utf-8")) != row["job_sha256"] or type(strict_json(row["job_snapshot"])) is not dict:
        raise ValueError("Deferred job snapshot changed")
    if row["kind"] not in {"cron", "loop"} or row["source"] != "scheduled" or row["state"] not in {"held", "uncertain"}:
        raise ValueError("Malformed deferred job metadata")
    from litetui.tool_policy import PROFILES
    payload = row["payload"]
    _keys(payload, ("content", "source", "tool_profile"), ("text",))
    payload_text(payload["content"])
    if (row["requested_profile"] not in PROFILES or payload["tool_profile"] != row["requested_profile"]
            or payload["source"] != row["source"] or len(encoded(payload)) > LIMITS["payload_bytes"]
            or "text" in payload and type(payload["text"]) is not str):
        raise ValueError("Deferred job payload changed")
    shutdown = row["shutdown"]
    _keys(shutdown, ("version", "instance", "closing_sequence", "final_sequence", "admission_closed", "worker_settled", "callback_settled", "process_settled", "producer_ids", "inputs", "admission_ids", "ledger_sha256", "effects_verified"))
    if (type(shutdown["version"]) is not int or shutdown["version"] != VERSION or shutdown["instance"] != epoch["instance"]
            or any(shutdown[key] is not True for key in ("admission_closed", "worker_settled", "callback_settled", "process_settled"))
            or shutdown["effects_verified"] is not False):
        raise ValueError("Deferred job lacks old-epoch retirement")
    _positive(shutdown["closing_sequence"])
    _positive(shutdown["final_sequence"])
    if shutdown["final_sequence"] < shutdown["closing_sequence"]:
        raise ValueError("Deferred job retirement sequence regressed")
    _hash(shutdown["ledger_sha256"])
    for key in ("producer_ids", "admission_ids"):
        if type(shutdown[key]) is not list or len(shutdown[key]) > LIMITS["producers"] or len(set(shutdown[key])) != len(shutdown[key]):
            raise ValueError("Deferred job retirement IDs malformed")
        for item in shutdown[key]:
            canonical_id(item)
    if type(shutdown["inputs"]) is not list or len(shutdown["inputs"]) > LIMITS["inputs"]:
        raise ValueError("Deferred job retirement inputs malformed")
    seen_inputs = set()
    for item in shutdown["inputs"]:
        _keys(item, ("input_id", "disposition"))
        canonical_id(item["input_id"])
        if item["input_id"] in seen_inputs:
            raise ValueError("Duplicate deferred job retirement input")
        seen_inputs.add(item["input_id"])
        if item["disposition"] not in {"settled", "held", "cancelled", "uncertain", "reconciled"}:
            raise ValueError("Deferred job retirement input disposition malformed")
    if row["reason"] is not None:
        text(row["reason"], 2048)
    if document["input_id"] is not None:
        canonical_id(document["input_id"])
    if version == 2:
        audit = document["retirement"]
        _keys(audit, ("operation_id", "review", "previous_sha256", "previous_state",
                      "identity", "reason", "no_replay", "effects_undone"))
        text(audit["operation_id"])
        _hash(audit["review"])
        _hash(audit["previous_sha256"])
        if (audit["previous_state"] not in {"pending", "uncertain"}
                or audit["reason"] != "user_discarded" or audit["no_replay"] is not True
                or audit["effects_undone"] is not False):
            raise ValueError("Malformed deferred retirement audit")
        retired = audit["identity"]
        _keys(retired, ("session_id", "agent_id", "backend", "model"))
        canonical_id(retired["session_id"])
        canonical_id(retired["agent_id"])
        text(retired["backend"], 64)
        text(retired["model"], 1024)
        if any(retired[key] != row["target"][key] for key in ("session_id", "agent_id")):
            raise ValueError("Deferred retirement belongs to another owned target")
    return raw, document


def deferred_evidence(app):
    evidence = deferred_document(app)
    if evidence is None:
        return []
    raw, document = evidence
    if document["state"] in {"delivered", "cancelled", "discarded"}:
        return []
    row = document["record"]
    live = getattr(app, "_native_work_deferred", None)
    if live is not None and live.raw == raw and live.session is app._agent_session and live.store is app.store:
        return [copy.deepcopy(row)]
    row = copy.deepcopy(row)
    row["state"] = "uncertain" if document["state"] == "uncertain" else "held"
    row["reason"] = "Owned staged invocation retained; restart never grants replay authority"
    return [row]


class _ScheduledTransitionHold(ValueError):
    """A canonical refreshed job needs an unsupported native identity handoff."""


def _native_job_transition_reason(app, job):
    from litetui import native_schedule
    if native_schedule.accepted_owner(app, job) is not None:
        return None
    version = getattr(getattr(app, "_launch_options", None), "work_lifecycle_version", None)
    if type(version) is int and version == VERSION and job.new_conversation:
        return ("Scheduled jobs that open a new conversation are held in this Rust session. "
                "Edit the job to use the current conversation, or run it in the Python frontend "
                "until native transition support is available.")
    return None


def _hold_scheduled_job(app, job, reason, *, manual):
    if manual:
        raise ValueError(reason)
    # One latest bounded hold, never an accumulating set of job identifiers.
    ident = job.id
    app._native_work_scheduled_transition_hold_job = ident if isinstance(ident, str) and len(ident.encode("utf-8")) <= 128 else None
    app._native_work_scheduled_transition_hold = reason
    if not getattr(app, "_native_work_scheduler_hold_reported", False):
        app._native_work_scheduler_hold_reported = True
        app._system(reason)


def start_job(app, job, *, manual=False):
    from litetui import native_schedule
    if native_schedule.offer(app, job, manual=manual):
        return None
    reason = _native_job_transition_reason(app, job)
    if reason is not None:
        return _hold_scheduled_job(app, job, reason, manual=manual)
    try:
        if getattr(app, "_native_work_deferred", None) is not None:
            raise ValueError("A staged scheduled invocation still awaits exact Work capture")
        value = bridge(app)
        if value is None or not value.active:
            raise ValueError("Scheduled Work capture is not active")
        value.check()
        if deferred_evidence(app):
            raise ValueError("A retained staged job requires explicit reconciliation before another invocation")
        jobs = getattr(app, "_native_work_jobs", None)
        if jobs is None:
            jobs = app._native_work_jobs = {}
        if job.id in jobs and not jobs[job.id].is_finished:
            raise ValueError("This exact scheduled invocation is already pending")
    except ValueError as exc:
        if manual:
            raise
        detail = (str(exc) or "Work admission is unavailable").encode("utf-8", "replace")[:384].decode("utf-8", "ignore")
        return _hold_scheduled_job(app, job, "Scheduled work is held: " + detail + ". Resolve the active Work state to allow a later tick.", manual=False)
    from textual.worker import NoActiveWorker, get_current_worker
    try:
        parent = get_current_worker()
    except NoActiveWorker:
        parent = None
    worker = app.run_worker(run_job(app, job, manual=manual), group="work-jobs", exclusive=False, exit_on_error=False)
    if not hasattr(app, "_native_work_job_owners"):
        app._native_work_job_owners = {}
    app._native_work_job_owners[worker] = {"parent": parent, "parent_task": _task(), "task": None}
    jobs[job.id] = worker
    return worker


async def run_job(app, job, manual=False):
    from textual.worker import get_current_worker
    worker = get_current_worker()
    owner = app._native_work_job_owners.get(worker)
    if owner is None or owner["task"] is not None or app._native_work_jobs.get(job.id) is not worker:
        raise ValueError("Scheduled invocation lacks its exact registered worker")
    owner["task"] = _task()
    try:
        return await _run_job(app, job, manual)
    finally:
        app._native_work_job_owners.pop(worker, None)


def transition_waiters(app):
    from litetui import native_work_execution
    execution = native_work_execution.owned(app)
    if (execution is not None and execution.parent is not None and not execution.parent.is_finished
            and getattr(execution.parent, "_task", None) is execution.parent_task):
        return (execution.parent,)
    from textual.worker import NoActiveWorker, get_current_worker
    try:
        worker = get_current_worker()
    except NoActiveWorker:
        return ()
    owner = getattr(app, "_native_work_job_owners", {}).get(worker)
    if (owner is None or owner["task"] is not _task() or owner["parent"] is None
            or owner["parent"].is_finished or getattr(owner["parent"], "_task", None) is not owner["parent_task"]):
        return ()
    return (owner["parent"],)


async def _run_job(app, job, manual=False):
    from dataclasses import asdict

    from litetui import conversation_transition
    value = bridge(app)
    assert value is not None
    reservation = reserve(app)
    from litetui import native_work_execution
    if execution := native_work_execution.owned(app, job):
        execution.reservation = reservation
    try:
        snapshot = encoded(asdict(job))
        if len(snapshot) > LIMITS["payload_bytes"]:
            raise ValueError("Scheduled job snapshot exceeds captured bounds before its stamp")
        payload = {"content": job.prompt, "text": job.prompt, "tool_profile": job.tool_profile, "source": "scheduled"}
        if len(encoded(payload)) > LIMITS["payload_bytes"]:
            raise ValueError("Scheduled job payload exceeds captured bounds before its stamp")
        try:
            prepared = app._fire_job(job, manual=manual, _work_prepare=True)
        except _ScheduledTransitionHold as exc:
            return _hold_scheduled_job(app, job, str(exc), manual=manual)
        if not prepared:
            raise ValueError("Job was not delivered: scheduler paused, lease unavailable, or durable stamp failed")
        invocation = ident()
        transition = prepared.get("transition")
        snapshot = encoded(asdict(job))
        if transition is not None:
            release(app, reservation)
            reservation = None
            proof = await before_identity_transition(app)
            if getattr(app, "_gui_quitting", False):
                raise ValueError("Scheduled transition stopped by Quit before publication")
            from litetui import native_schedule
            pending = native_schedule.accepted_owner(app, job)
            result = (await conversation_transition.apply(app, transition) if pending is None
                      else await conversation_transition.apply_review(app, transition, pending.review))
            if pending is not None:
                pending.transition = result
            if result["status"] != "created":
                if not result["close_required"] and bridge(app) is value:
                    after_identity_transition(app, proof)
                raise ValueError("Scheduled conversation transition did not complete: " + str(result.get("error")))
            if bridge(app) is value:
                after_identity_transition(app, proof)
            row = {"invocation_id": invocation, "job_id": job.id, "kind": job.kind, "generation": job.run_count,
                   "source": "scheduled", "requested_profile": prepared["item"]["tool_profile"],
                   "payload": prepared["item"], "job_snapshot": snapshot.decode("utf-8"), "job_sha256": digest(snapshot),
                   "epoch": {"instance": value.instance, "identity": copy.deepcopy(value.identity), "provider": value.provider},
                   "target": {key: identity(app)[key] for key in ("session_id", "agent_id")}, "shutdown": proof,
                   "state": "held", "reason": "Live owned invocation awaits a fresh Work activation"}
            document = {"version": VERSION, "state": "pending", "input_id": None, "record": row}
            try:
                raw = _write_deferred(app, document, None)
            except BaseException:
                app._conversation_transition_error = "Scheduled invocation publication is uncertain; close without replay"
                raise
            app._native_work_deferred = DeferredJob(document, raw, app._agent_session, app.store,
                                                  Path(app.convo_dir) / DEFERRED_FILENAME, copy.deepcopy(job))
            return {"delivered": False, "awaiting_work_capture": True, "job_id": job.id, "invocation_id": invocation,
                    "work_shutdown": proof, "work_recapture_required": True}
        result = _deliver_job(app, job, prepared["item"], invocation, reservation)
        if result["delivered"] and getattr(app, "_native_work_scheduled_transition_hold_job", None) == job.id:
            app._native_work_scheduled_transition_hold = None
            app._native_work_scheduled_transition_hold_job = None
            app._native_work_scheduler_hold_reported = False
        return result
    finally:
        release(app, reservation)


def queue_user_input(app, parent, review, payload, *, interrupt=False, claude=None):
    """Capture one reviewed reply under the actual GUI worker and canonical FIFO."""
    from textual.worker import get_current_worker

    value = bridge(app)
    worker = get_current_worker()
    app._materialise_convo()
    reservation = reserve(app)
    try:
        producer = register_producer(app, "user_interrupt" if interrupt else "user_queue", review, owner_instance=value.instance,
                                     reservation=reservation, queue_parent=parent)
        bind_producer_worker(app, producer, worker)
        producer.asyncio_task = _task()
        token = _PRODUCER.set((app, producer, _task()))
        try:
            item = dict(payload)
            if claude is not None:
                if interrupt or value.provider != "claude":
                    raise ValueError("Only Claude next-turn queue may prepare native input")
                item.update(claude.prepare(app, payload, review))
            entry = enqueue(app, producer, item)
            if entry.state != "queued" or _read(value.path) != value.raw:
                raise ValueError("Queued composer input lacks an exact persisted ledger record")
            if claude is not None:
                claude.receipt(app, item, review)
            item["bubble"] = app._user_bubble(payload["content"], False, queued=not interrupt)
            if interrupt:
                # These consecutive synchronous steps mirror the host send chord. Signal the
                # soft stop only after exact durable capture, before yielding to any other task.
                app._pending_input.insert(0, item)
                app._stop_requested = True
                from litetui import native_runtime_controls
                native_runtime_controls.signal_stop(app)
            else:
                app._pending_input.append(item)
            return producer.ref, entry.ref
        finally:
            _PRODUCER.reset(token)
    except Exception as exc:
        value.error = "Queued draft publication is uncertain; close and inspect retained Work before sending again"
        raise ValueError(value.error) from exc
    finally:
        release(app, reservation)


def _deliver_job(app, job, item, invocation, reservation=None):
    from textual.worker import get_current_worker

    from litetui import hook_host, tasks
    from litetui.task_supervisor import process_creation_identity
    app._materialise_convo()
    record = register_producer(app, job.kind, job.id, owner_instance=tasks._INSTANCE_ID,
                               owner_created=process_creation_identity(os.getpid()), generation=job.run_count,
                               reservation=reservation)
    bind_producer_worker(app, record, get_current_worker())
    record.asyncio_task = _task()
    token = _PRODUCER.set((app, record, _task()))
    try:
        entry = enqueue(app, record, item)
        if entry.state == "queued":
            app._pending_input.append(item)
            app._user_bubble(item.get("text", item["content"]), False, queued=app._chat_running())
            if not app._chat_running():
                hook_host.start_prompt(app, item)
        return {"delivered": True, "job_id": job.id, "invocation_id": invocation, "run_count": job.run_count,
                "input_id": entry.ref["input_id"]}
    finally:
        _PRODUCER.reset(token)


def _resume_deferred(app, deferred: DeferredJob, value: Bridge):
    if (getattr(app, "_native_work_deferred", None) is not deferred or deferred.claimed
            or bridge(app) is not value or not value.active or value.closing or getattr(app, "_gui_quitting", False)):
        return
    if (app._agent_session is not deferred.session or app.store is not deferred.store
            or Path(app.convo_dir) / DEFERRED_FILENAME != deferred.path
            or _read(deferred.path, LIMITS["reply_bytes"]) != deferred.raw):
        value.error = "Staged scheduled invocation lost exact ownership; no replay"
        return
    deferred.claimed = True
    async def resume():
        try:
            row = deferred.document["record"]
            result = _deliver_job(app, deferred.job, copy.deepcopy(row["payload"]), row["invocation_id"])
            deferred.document.update(state="delivered", input_id=result["input_id"])
            deferred.raw = _write_deferred(app, deferred.document, deferred.raw)
            app._native_work_deferred = None
            return result
        except BaseException:
            deferred.document["state"] = "uncertain"
            deferred.raw = _write_deferred(app, deferred.document, deferred.raw)
            value.error = "Staged scheduled invocation is uncertain; no automatic replay"
            raise
    deferred.worker = app.run_worker(resume(), group="work-jobs", exclusive=False, exit_on_error=False)


def cancel_deferred(app):
    deferred = getattr(app, "_native_work_deferred", None)
    if deferred is None or deferred.claimed:
        return
    if deferred.handle is not None:
        deferred.handle.cancel()
    deferred.claimed = True
    deferred.document["state"] = "cancelled"
    deferred.raw = _write_deferred(app, deferred.document, deferred.raw)
    app._native_work_deferred = None



def prompt_refusal(app):
    from litetui import native_playback
    if native_playback.blocking(app):
        return "Close response playback before submitting a prompt"
    from litetui import native_dictation
    if native_dictation.blocking(app):
        return "Finish or cancel dictation before sending a prompt"
    from litetui import native_runtime_controls, native_work_execution
    if why := native_runtime_controls.mutation_hold(app):
        return why
    if why := native_work_execution.mutation_hold(app):
        return why
    from litetui import native_schedule
    if why := native_schedule.admission_refusal(app):
        return why
    if activation_pending(app):
        return "Work startup capture and activation are required before input admission"
    if getattr(app, "_native_work_recapture_required", None) is not None:
        return "Work identity changed; complete a fresh capture and activation before admission"
    value = bridge(app)
    if value is None or not value.active:
        return "A scheduled invocation awaits Work activation" if getattr(app, "_native_work_deferred", None) is not None else None
    if value.closing or value.error:
        return value.error or "Work admission is closed"
    if value.pending_readbacks:
        return "Work reconciliation awaits its exact published readback"
    if any(not entry.recovered and (entry.claim_task is not None
           or entry.admission_worker is not None and not entry.admission_worker.is_finished
           or entry.worker is not None and not entry.worker.is_finished) for entry in value.inputs.values()):
        return "A source-owned Work admission is active; wait for its exact turn boundary"
    return None



def payload_text(value):
    if type(value) is not str or not value or len(value.encode("utf-8")) > LIMITS["payload_bytes"]:
        raise ValueError("Work payload content must be nonempty bounded text")
    return value


def prepare_job(app, job):
    # _fire_job may refresh this exact object from canonical disk under its
    # scheduler lease after start_job returns. Recheck before its durable stamp.
    reason = _native_job_transition_reason(app, job)
    if reason is not None:
        raise _ScheduledTransitionHold(reason)
    from dataclasses import asdict

    from litetui import conversation_transition, scheduler
    snapshot = encoded(asdict(job))
    item = {"content": job.prompt, "text": job.prompt, "tool_profile": scheduler.level_of(job.tool_profile), "source": "scheduled"}
    text(job.id)
    payload_text(job.prompt)
    if job.kind not in {"cron", "loop"} or len(snapshot) > LIMITS["payload_bytes"] or len(encoded(item)) > LIMITS["payload_bytes"]:
        raise ValueError("Scheduled invocation exceeds captured bounds before its durable stamp")
    transition = None
    from litetui import native_schedule
    pending = native_schedule.accepted_owner(app, job)
    if pending is not None:
        transition = copy.deepcopy(pending.proposal["transition"])
    elif job.new_conversation and not app._chat_running():
        cmd = {"version": VERSION, **identity(app)}
        state = conversation_transition.snapshot(app, cmd)
        transition = conversation_transition.validate(app, {**cmd, "snapshot": state["snapshot"], "action": "new"})
    return {"item": item, "transition": transition}



def strict_json(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("Duplicate Work JSON field")
            result[key] = value
        return result
    def invalid(_value):
        raise ValueError("Nonfinite Work JSON value")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def verify_job_stamp(app, job):
    from dataclasses import asdict

    from litetui import native_work, paths, scheduler
    raw = _read(scheduler.jobs_path(paths.data_root()))
    rows = strict_json(raw) if raw is not None else []
    if (type(rows) is not list
            or [native_work._job(row)["row"] for row in rows if type(row) is dict and row.get("id") == job.id] != [asdict(job)]):
        raise ValueError("Scheduled invocation stamp failed exact durable readback; no input delivered")



def activation_pending(app):
    value = bridge(app)
    return (value is not None and not value.active) or (
        getattr(getattr(app, "_launch_options", None), "work_lifecycle_version", None) == VERSION
        and getattr(app, "_native_work_initial_activated", False) is not True)


def require_recapture_complete(app):
    from litetui import native_schedule
    if why := native_schedule.admission_refusal(app):
        raise ValueError(why)
    if activation_pending(app):
        raise ValueError("Work startup capture and activation are required before execution")
    if getattr(app, "_native_work_recapture_required", None) is not None:
        raise ValueError("Work identity changed; fresh capture and activation are required")



def retain_failed_stream(app, worker, origin):
    """Retain the exact already-created worker if normal registration failed."""
    value = bridge(app)
    if value is None or not value.active:
        return
    worker.cancel()
    existing = value.executions.get(origin.execution_id)
    if existing is not None and existing != (worker, origin):
        value.error = "Work execution binding collided; cleanup requires inspection"
        raise RuntimeError(value.error)
    value.executions[origin.execution_id] = (worker, origin)
    scope = _ADMISSION.get()
    if scope is None or scope[0] is not app or scope[2] is not _task():
        return
    entry = scope[1]
    if entry.admission is None or entry.admission["origin"] != origin.envelope_fields():
        raise RuntimeError("Failed Work stream lost its preannounced admission")
    if entry.worker is not None and entry.worker is not worker:
        raise RuntimeError("Failed Work stream changed its exact worker")
    entry.worker = worker
    if entry.observer is None:
        _watch_admission(app, entry)


def _goal_producer(app, state, reservation=None):
    from litetui import tasks
    from litetui.task_supervisor import process_creation_identity
    value = bridge(app)
    assert value is not None
    generation = max([state.evaluated_turns + 1, *(p.ref["generation"] + 1
        for p in value.producers.values() if p.ref["kind"] == "goal" and p.ref["object_id"] == state.id)])
    return register_producer(app, "goal", state.id, owner_instance=tasks._INSTANCE_ID,
                             owner_created=process_creation_identity(os.getpid()), generation=generation, reservation=reservation)


def goal_input(app, item, *, wake):
    """Keep canonical goal metadata beside the immutable captured payload."""
    if not capturing_completion(app):
        raise ValueError("Goal completion lacks its exact evaluator or delivery worker")
    entry = enqueue(app, _PRODUCER.get()[1], item)
    if entry.state == "queued":
        app._pending_input.append(item)
        defer_action_input(app, entry)
        if wake:
            from litetui import hook_host
            app._user_bubble(item.get("text", item["content"]), False, queued=app._chat_running())
            if not app._chat_running():
                hook_host.start_prompt(app, item)
    return entry


def start_goal(app, state, item, *, reservation=None, execution=None):
    require_recapture_complete(app)
    if not enabled(app):
        return None
    # Size validation happens before producer or worker creation, including the
    # exact caller-supplied objective and instruction; no completion truncation.
    payload = {key: item[key] for key in ("content", "text", "source", "tool_profile")}
    payload_text(payload["content"])
    text(payload["source"])
    if len(encoded(payload)) > LIMITS["payload_bytes"]:
        raise ValueError("Goal input exceeds the captured payload byte limit")
    from litetui import conversation_action as action
    record = _goal_producer(app, state, reservation=reservation)
    async def deliver():
        if execution is not None:
            from litetui import native_work_execution
            if native_work_execution._attempt(app) is not execution:
                raise ValueError("Goal delivery lost its exact execution owner")
            execution.delivery_task = _task()
        return goal_input(app, item, wake=True)
    worker = app.run_worker(producer_runner(app, record, deliver()), group="work-goals",
                            exclusive=False, exit_on_error=False)
    bind_producer_worker(app, record, worker)
    action.track_descendant(app, worker)
    return worker


def start_compact_wake(app, content):
    """Preserve the canonical one-shot compact ping under exact Work ownership."""
    from litetui import conversation_action as action
    from litetui import seat_authority, tasks
    from litetui.task_supervisor import process_creation_identity
    require_new_execution(app)
    if not enabled(app):
        return None
    payload = {"content": content, "source": "compact-wake",
               "tool_profile": seat_authority.seat_profile(app)}
    validate_payload(payload)
    value = bridge(app)
    record = register_producer(app, "compact_wake", ident(), owner_instance=tasks._INSTANCE_ID,
                               owner_created=process_creation_identity(os.getpid()))

    async def deliver():
        # The callback used to act synchronously. A real turn that wins this
        # new worker scheduling gap still wins; never queue a stale ping.
        if (app._chat_running() or app._pending_input or getattr(app, "_turn_abandoned", False)
                or getattr(app, "_stop_requested", False) or getattr(app, "_gui_quitting", False)
                or value.closing or value.error or value.pending_readbacks
                or record.action is not None and record.action.stopped):
            return None
        value.check()
        why = seat_authority.floor_refusal(app, "compact-wake")
        if why is not None:
            app._system(why)
            return None
        item = dict(payload)
        entry = enqueue(app, record, item)
        if entry.state == "queued":
            app._pending_input.append(item)
            defer_action_input(app, entry)
            app._user_bubble(content, False)
            start_prompt(app, item)
        return entry

    worker = app.run_worker(producer_runner(app, record, deliver()), group="work-compact-wake",
                            exclusive=False, exit_on_error=False)
    bind_producer_worker(app, record, worker)
    action.track_descendant(app, worker)
    return worker


async def evaluate_goal(app, state, callback):
    """The evaluator belongs to the exact stream worker running its finalizer."""
    require_recapture_complete(app)
    if not enabled(app):
        return await callback()
    from textual.worker import get_current_worker
    worker = get_current_worker()
    if current_origin(app) is None or worker is None:
        raise ValueError("Goal evaluator lacks its exact current execution")
    record = _goal_producer(app, state)
    bind_producer_worker(app, record, worker)
    record.asyncio_task = _task()
    token = _PRODUCER.set((app, record, _task()))
    try:
        return await callback()
    finally:
        _PRODUCER.reset(token)


def validate_payload(payload):
    from litetui.tool_policy import PROFILES
    if payload.get("delivery") == "committed_history":
        import re
        _keys(payload, ("delivery", "source", "tool_profile", "text", "history"))
        if payload["source"] != "child-result":
            raise ValueError("Committed history must preserve child-result source")
        text(payload["text"], 1024)
        history = payload["history"]
        _keys(history, ("session_id", "transcript_sha256", "context_sha256", "receipt_ids"))
        canonical_id(history["session_id"])
        _hash(history["transcript_sha256"])
        _hash(history["context_sha256"])
        ids = history["receipt_ids"]
        if (type(ids) is not list or not 1 <= len(ids) <= 256
                or any(type(i) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", i) is None for i in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("Malformed committed completion receipt IDs")
    else:
        _keys(payload, ("content", "tool_profile", "source"), ("text",))
        payload_text(payload["content"])
        text(payload["source"])
        if "text" in payload and type(payload["text"]) is not str:
            raise ValueError("Malformed Work display text")
    if payload["tool_profile"] not in PROFILES or len(encoded(payload)) > LIMITS["payload_bytes"]:
        raise ValueError("Work completion payload exceeds its reserved contract")


async def run_committed_wake(app, *, parent, payload, validate, claim, finish, release):
    """Admit already-committed child evidence without appending user content.

    The canonical caller revalidates the exact parent/receipt objects. This
    worker owns the claim and finish/release callbacks; its observer therefore
    cannot retire before the receipt disposition is known.
    """
    from textual.worker import get_current_worker

    from litetui import seat_authority, tasks
    from litetui.task_supervisor import process_creation_identity
    from litetui.turn_deferral import STREAM_DEFERRED
    require_recapture_complete(app)
    value = bridge(app)
    if value is None or not value.active or parent is None:
        raise ValueError("Committed child wake requires captured runtime ownership")
    payload = copy.deepcopy(payload)
    validate_payload(payload)
    if payload.get("delivery") != "committed_history" or payload["history"]["session_id"] != app.convo_id:
        raise ValueError("Committed child wake targets another owned history")
    validate()
    worker = get_current_worker()
    if worker is None:
        raise ValueError("Committed child wake lacks its exact callback worker")
    record = register_producer(app, "child_result", ident(), owner_instance=tasks._INSTANCE_ID,
        owner_created=process_creation_identity(os.getpid()))
    bind_producer_worker(app, record, worker)
    record.asyncio_task = _task()
    token = _PRODUCER.set((app, record, _task()))
    entry = None
    try:
        entry = enqueue(app, record, copy.deepcopy(payload))
        if entry.state != "queued":
            return []
        entry.claim_task = _task()
        value.input_event(entry, "claiming")
        validate()
        ids = claim()
        if ids != payload["history"]["receipt_ids"]:
            raise ValueError("Committed child receipt claim changed after preflight")
        validate()
        if digest(encoded(app.conversation)) != payload["history"]["context_sha256"]:
            raise ValueError("Committed child context changed before scheduling")
        if digest(_read(Path(app.convo_path), 8 * 1024 * 1024)) != payload["history"]["transcript_sha256"]:
            raise ValueError("Committed child transcript changed before scheduling")
        entry.committed_verified = True
        app._active_tool_profile = seat_authority.turn_profile(app, "child-result", payload["tool_profile"])
        app._hooks_suppressed = False
        app._hook_corrections = 0
        app._hook_source = "child-result"
        app._hook_turn_id = ident()
        admission_scope = _ADMISSION.set((app, entry, _task()))
        try:
            execution = app._stream(origin=EventOrigin(session_id=app.convo_id, operation_id=ident(), source="child-result"))
            if entry.admission is None or entry.worker is not execution:
                raise RuntimeError("Committed child wake failed exact stream binding")
        finally:
            _ADMISSION.reset(admission_scope)
        result = await execution.wait()
        disposition = release if result is STREAM_DEFERRED else finish
        if disposition(ids) is not True:
            raise ValueError("Committed child receipt disposition did not persist")
        entry.committed_finished = True
        return [] if result is STREAM_DEFERRED else ids
    except BaseException:
        if entry is not None:
            _uncertain(app, entry)
        raise
    finally:
        if entry is not None:
            entry.claim_task = None
        _PRODUCER.reset(token)


@dataclass
class ProviderLane:
    app: Any
    owner: Any
    origin: EventOrigin
    parent: Any
    bridge: Any
    worker: Any = None
    task: Any = None
    observer: Any = None


def capture_provider_lane(app, owner):
    require_recapture_complete(app)
    if not enabled(app):
        return None
    value = bridge(app)
    value.check()
    origin = current_origin(app)
    if origin is None or value.provider != "codex":
        raise ValueError("Native steering lacks its exact captured parent execution")
    return ProviderLane(app, owner, origin, value.executions[origin.execution_id][0], value)


def bind_provider_lane(app, owner, lane):
    if lane is None:
        return
    from textual.worker import get_current_worker
    if lane.app is not app or lane.owner is not owner or lane.task is not None or bridge(app) is not lane.bridge:
        raise ValueError("Native steering worker identity changed or was rebound")
    lane.worker, lane.task = get_current_worker(), _task()
    if lane.worker is lane.parent or lane.worker is None:
        raise ValueError("Native steering lacks its exact distinct callback worker")
    async def retired():
        from textual.worker import WorkerCancelled, WorkerFailed
        try:
            await lane.worker.wait()
        except (WorkerCancelled, WorkerFailed):
            pass
        if not lane.worker.is_finished:
            raise RuntimeError("Native steering callback is still live")
    lane.observer = _observe(retired())


def _provider_scope(app, lane):
    from textual.worker import get_current_worker
    if (lane is None or lane.app is not app or lane.bridge is not bridge(app)
            or lane.task is not _task() or lane.worker is not get_current_worker()
            or lane.bridge.executions.get(lane.origin.execution_id) != (lane.parent, lane.origin)
            or lane.parent.is_finished):
        raise ValueError("Native steering boundary lacks its exact registered parent and child")
    return _EXECUTION.set((app, _task(), lane.origin))


def bind_provider_input(app, lane, item, native, ledger):
    entry = entry_for(app, item)
    if entry is None:
        return
    token = _provider_scope(app, lane)
    try:
        lane.bridge.check()
        if entry.state != "queued" or not any(row is native for row in ledger.entries):
            raise ValueError("Native steering input is held, uncertain, or foreign")
        bind_native_journal(app, entry, native, ledger)
        entry.provider_lane = lane
    finally:
        _EXECUTION.reset(token)


def provider_input_state(app, lane, item, state):
    entry = entry_for(app, item)
    if entry is None or entry.state != "queued":
        return
    token = _provider_scope(app, lane)
    try:
        if state == "denied":
            lane.bridge.input_event(entry, "held", "Native admission denied; exact input remains retained")
        elif state in {"uncertain", "sending", "admitting"}:
            _begin(app, entry, True)
            _uncertain(app, entry)
            from litetui import restored_input
            restored_input.mark(app, item)
    finally:
        entry.claim_task = None
        _EXECUTION.reset(token)


def accept_provider_input(app, lane, item):
    entry = entry_for(app, item)
    if entry is None:
        return False
    token = _provider_scope(app, lane)
    try:
        native = item["_codex_entry"]
        if (native.get("state") != "accepted" or entry.provider_lane is not lane
                or native.get("work_input_id") != entry.ref["input_id"]
                or native.get("work_payload_sha256") != entry.ref["payload_sha256"]):
            raise ValueError("Native acceptance lacks the exact durable Work binding")
        _begin(app, entry, True)
        if any(row.get("codex_delivery", {}).get("id") == native["id"] for row in app.conversation):
            raise ValueError("Native accepted input was already materialized without this Work claim")
        from litetui import hook_host
        accepted = hook_host.accept_prompt_origin(app, {**item, "_gui_in_turn": True,
            "_codex_metadata": {"provider": "codex", "app_server_thread_id": native["threadId"],
                                "instructions_digest": native.get("instructions_digest")}},
            native_accepted=True, persist_user=lambda message: _persist_user(app, entry, message))
        if accepted is None:
            raise ValueError("Provider accepted input but canonical host materialization was refused")
        app._hook_turn_id = native.get("admission", {}).get("turn_id", app._hook_turn_id)
        return _finish_admission(app, entry, accepted, True)
    except BaseException:
        _uncertain(app, entry)
        raise
    finally:
        entry.claim_task = None
        _EXECUTION.reset(token)


def mark_producer_stop(app, task):
    value = bridge(app)
    if value is None or not value.active:
        raise ValueError("Task Stop requires an active captured runtime")
    value.check(allow_error=True)
    record = next((p for p in value.producers.values() if p.task is task), None)
    if record is None or record.worker is None or record.phase not in {"running", "stop_requested"}:
        raise ValueError("Task Stop lacks its exact active producer worker")
    if record.phase == "stop_requested":
        return
    record.phase = "stop_requested"
    value.persist()
    value.emit("work_producer", phase="stop_requested", producer=record.ref, worker_id=record.worker_id,
               task_state=task.state, reason="Canonical task Stop requested", proof=None)


def register_provider_producer(app, kind, object_id):
    from litetui import tasks
    from litetui.task_supervisor import process_creation_identity
    if kind not in {"provider_question", "provider_recovery"}:
        raise ValueError("Unsupported provider callback kind")
    return register_producer(app, kind, object_id, owner_instance=tasks._INSTANCE_ID,
                             owner_created=process_creation_identity(os.getpid()))


def bind_native_journal(app, entry, native, ledger):
    value = bridge(app)
    if value is None or value.inputs.get(entry.ref["input_id"]) is not entry:
        raise ValueError("Native journal lost its exact captured Work input")
    binding = {"work_instance": value.instance, "work_input_id": entry.ref["input_id"],
               "work_payload_sha256": entry.ref["payload_sha256"]}
    if any(key in native and native[key] != val for key, val in binding.items()):
        raise ValueError("Native delivery journal belongs to another Work input")
    if any(native.get(key) != val for key, val in binding.items()):
        native.update(binding)
        ledger.save()
    from litetui import restored_input
    from litetui.conversation import ConversationRepository
    meta, rows = ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)
    saved = restored_input._records(rows).get(native["id"])
    if meta.get("id") != app.convo_id or saved is None or saved[0] != native:
        raise ValueError("Native Work journal binding failed independent durable readback")


@dataclass
class Reconciliation:
    app: Any
    value: Any
    entry: Input
    item: dict
    native: dict
    task: Any
    evidence: dict
    consumed: bool = False


def validate_reconciliation(evidence, input_ref, epoch):
    _keys(evidence, ("reconciliation", "proof", "history_sha256"))
    r = evidence["reconciliation"]
    _keys(r, ("operation_id", "provider", "delivery_id", "thread_id", "turn_id",
              "provider_history_sha256", "journal_instance", "journal_sha256", "payload_sha256"))
    for key in ("operation_id", "delivery_id", "thread_id", "turn_id"):
        text(r[key])
    for key in ("provider_history_sha256", "journal_instance", "journal_sha256", "payload_sha256"):
        _hash(r[key])
    if r["provider"] != "codex" or r["journal_instance"] != epoch["instance"] or r["payload_sha256"] != input_ref["payload_sha256"]:
        raise ValueError("Work reconciliation changed original epoch or payload")
    _hash(evidence["history_sha256"])
    p = evidence["proof"]
    _keys(p, ("current_workers_retired", "current_callbacks_retired", "input_durable", "no_replay"))
    if any(value is not True for value in p.values()):
        raise ValueError("Work reconciliation lacks exact current retirement and history proof")


async def prepare_reconciliation(app, item, native, operation, history):
    value = bridge(app)
    if value is None or not value.active or "work_input_id" not in native:
        return None
    value.check(allow_error=True)
    if _read(value.path) != value.raw:
        raise ValueError("Work ledger changed before explicit history reconciliation")
    entry = value.inputs.get(native["work_input_id"])
    if (entry is None or entry.state not in {"held", "uncertain"} or entry.admission is not None
            or native.get("work_instance") != entry.epoch["instance"]
            or native.get("work_payload_sha256") != entry.ref["payload_sha256"]
            or any(native.get("item", {}).get(key) != val for key, val in entry.payload.items())
            or item.get("_codex_entry") is not native):
        raise ValueError("Native reconciliation does not match exact unresolved Work input")
    from litetui import codex_steering
    history = copy.deepcopy(history)
    if not codex_steering.accepted_in_history(native, history):
        raise ValueError("Work reconciliation lacks positive matching provider history")
    from litetui.shutdown_wait import join_task
    if not entry.recovered:
        if entry.item is not item or entry.producer.observer is None:
            raise ValueError("Work reconciliation lost its current producer ownership")
        await join_task(entry.producer.observer)
        if entry.provider_lane is not None:
            await join_task(entry.provider_lane.observer)
        if entry.producer.phase != "retired":
            raise ValueError("Work reconciliation producer remains live")
    value.check(allow_error=True)
    evidence = {"operation_id": text(operation), "provider": "codex", "delivery_id": text(native["id"]),
        "thread_id": text(native["threadId"]), "turn_id": text(native["turnId"]),
        "provider_history_sha256": digest(encoded(history)), "journal_instance": entry.epoch["instance"],
        "payload_sha256": entry.ref["payload_sha256"]}
    return Reconciliation(app, value, entry, item, native, _task(), evidence)


def finish_reconciliation(app, ticket):
    if ticket is None:
        return None
    value, entry, native = ticket.value, ticket.entry, ticket.native
    value.check(allow_error=True)
    if (ticket.consumed or ticket.app is not app or ticket.task is not _task() or bridge(app) is not value
            or value.inputs.get(entry.ref["input_id"]) is not entry or entry.state not in {"held", "uncertain"}
            or entry.admission is not None or native.get("state") != "accepted"
            or native.get("work_instance") != entry.epoch["instance"]
            or native.get("work_input_id") != entry.ref["input_id"]
            or native.get("work_payload_sha256") != entry.ref["payload_sha256"]
            or native.get("reconciliation_operation_id") != ticket.evidence["operation_id"]):
        raise ValueError("Work reconciliation lost its exact linear ownership")
    from litetui import restored_input
    from litetui.conversation import ConversationRepository
    meta, rows = ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)
    saved = restored_input._records(rows).get(native["id"])
    delivered = [row for row in rows if row.get("codex_delivery", {}).get("id") == native["id"]]
    if (meta.get("id") != app.convo_id or rows != app.conversation or saved is None or saved[0] != native
            or len(delivered) != 1 or delivered[0].get("content") != entry.payload["content"]
            or delivered[0]["codex_delivery"].get("state") != "accepted"):
        raise ValueError("Reconciled Work input failed exact journal and transcript readback")
    r = {**ticket.evidence, "journal_sha256": digest(encoded(native))}
    proof = {"current_workers_retired": True, "current_callbacks_retired": True, "input_durable": True, "no_replay": True}
    entry.reconciliation = {"reconciliation": r, "proof": proof, "history_sha256": digest(encoded(rows))}
    validate_reconciliation(entry.reconciliation, entry.ref, entry.epoch)
    entry.state, entry.reason = "reconciled", None
    value.persist()
    value.pending_readbacks[entry.ref["input_id"]] = entry.reconciliation
    result = value.emit("work_reconciled", origin=EventOrigin(session_id=app.convo_id,
        operation_id=r["operation_id"], source="rpc"), input=entry.ref,
        **entry.reconciliation, ledger_sha256=digest(value.raw))
    ticket.consumed = True
    value.pending_readbacks[entry.ref["input_id"]] = copy.deepcopy(result)
    return result


@dataclass
class ReconciliationReadback:
    app: Any
    value: Any
    entry: Input
    event: dict
    task: Any
    consumed: bool = False


def prepare_reconciliation_readback(app, cmd, result):
    """Verify a selected receipt, without releasing admission before publication."""
    if "work_reconciliation" not in cmd:
        return None
    _keys(cmd, ("type", "id", "session_id", "work_reconciliation"))
    if cmd["type"] != "gui.restored_input.list":
        raise ValueError("Work readback marker requires the explicit restored-input list")
    text(cmd["id"])
    selected = cmd["work_reconciliation"]
    _keys(selected, ("instance", "input_id", "operation_id", "delivery_id"))
    value = bridge(app)
    if value is None or not value.active:
        raise ValueError("Work readback requires its active captured runtime")
    value.check(allow_error=True)
    canonical_id(selected["input_id"])
    event = value.pending_readbacks.get(selected["input_id"])
    entry = value.inputs.get(selected["input_id"])
    if (cmd["session_id"] != app.convo_id or selected["instance"] != value.instance
            or event is None or entry is None or entry.state != "reconciled"
            or event.get("reconciliation") != entry.reconciliation["reconciliation"]
            or any(selected[key] != event["reconciliation"][key] for key in ("operation_id", "delivery_id"))):
        raise ValueError("Work readback does not name the exact pending reconciliation")
    from litetui import restored_input
    from litetui.conversation import ConversationRepository
    if result != restored_input.inspect(app, app.convo_id):
        raise ValueError("Work readback differs from the independent current queue")
    meta, rows = ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)
    r = event["reconciliation"]
    saved = restored_input._records(rows).get(r["delivery_id"])
    if (meta.get("id") != app.convo_id or rows != app.conversation or saved is None
            or saved[0].get("state") != "accepted" or digest(encoded(saved[0])) != r["journal_sha256"]
            or digest(encoded(rows)) != event["history_sha256"] or _read(value.path) != value.raw):
        raise ValueError("Work reconciliation readback changed its exact durable evidence")
    result["work_reconciliation_readback"] = {
        "kind": "work_reconciliation_readback", "version": VERSION, **selected,
        "journal_instance": r["journal_instance"], "journal_sha256": r["journal_sha256"],
        "payload_sha256": r["payload_sha256"], "history_sha256": event["history_sha256"],
        "event_ledger_sha256": event["ledger_sha256"], "ledger_sha256": digest(value.raw),
        "snapshot": result["snapshot"], "verified": True}
    return ReconciliationReadback(app, value, entry, event, _task())


def finish_reconciliation_readback(app, ticket):
    """Called synchronously only after the exact successful RPC response emits."""
    if ticket is None:
        return
    value, entry = ticket.value, ticket.entry
    value.check(allow_closing=True, allow_error=True)
    if (ticket.consumed or ticket.app is not app or ticket.task is not _task()
            or bridge(app) is not value or value.inputs.get(entry.ref["input_id"]) is not entry
            or value.pending_readbacks.get(entry.ref["input_id"]) is not ticket.event):
        raise ValueError("Work readback release lost its exact single-use ownership")
    ticket.consumed = True
    if value.closing:
        return  # Publication while closing proves history, never resumes admission.
    del value.pending_readbacks[entry.ref["input_id"]]
    value.uncertain_inputs.discard(entry.ref["input_id"])
    if (value.uncertain_error_owned and not value.uncertain_inputs
            and not any(i.state == "uncertain" for i in value.inputs.values())
            and value.error == "Canonical Work admission is uncertain; never replay automatically"):
        value.error = None
        value.uncertain_error_owned = False


def require_new_execution(app):
    require_recapture_complete(app)
    value = bridge(app)
    if value is not None and value.active:
        value.check()


def prepare_publication(app, cmd, result):
    """Prepare one exact known response barrier, never an arbitrary callback."""
    from litetui import native_deferred, native_schedule, native_work_execution
    from litetui import native_runtime_controls
    ticket = native_runtime_controls.prepare_publication(app, cmd, result)
    if ticket is None:
        from litetui import native_dictation
        ticket = native_dictation.prepare_publication(app, cmd, result)
    if (ticket is None and cmd.get("backend") == "claude"
            and cmd.get("type") in {"gui.prompt.native_queue_apply", "gui.prompt.native_send_apply"}):
        from litetui import native_claude_queue
        ticket = native_claude_queue.prepare_publication(app, cmd, result)
    if ticket is None:
        from litetui import native_model_lifecycle
        ticket = native_model_lifecycle.prepare_publication(app, cmd, result)
    if ticket is None:
        from litetui import native_playback
        ticket = native_playback.prepare_publication(app, cmd, result)
    if ticket is None:
        ticket = native_work_execution.prepare_publication(app, cmd, result)
    if ticket is None:
        ticket = native_schedule.prepare_publication(app, cmd, result)
    if ticket is None:
        ticket = native_deferred.prepare_publication(app, cmd, result)
    return ticket if ticket is not None else prepare_reconciliation_readback(app, cmd, result)


def finish_publication(app, ticket):
    from litetui import native_deferred, native_schedule, native_work_execution
    from litetui import native_runtime_controls
    if ticket is not None and getattr(app, "_native_claude_queue_publication", None) is ticket:
        from litetui import native_claude_queue
        native_claude_queue.finish_publication(app, ticket)
        return
    from litetui import native_dictation, native_model_lifecycle, native_playback
    if isinstance(ticket, native_dictation.Publication):
        native_dictation.finish_publication(app, ticket)
    elif isinstance(ticket, native_model_lifecycle.Publication):
        native_model_lifecycle.finish_publication(app, ticket)
    elif isinstance(ticket, native_playback.Publication):
        native_playback.finish_publication(app, ticket)
    elif isinstance(ticket, native_runtime_controls.Publication):
        native_runtime_controls.finish_publication(app, ticket)
    elif isinstance(ticket, native_work_execution.Publication):
        native_work_execution.finish_publication(app, ticket)
    elif isinstance(ticket, (native_schedule.ActivationPublication, native_schedule.DecisionPublication)):
        native_schedule.finish_publication(app, ticket)
    elif isinstance(ticket, native_deferred.Publication):
        native_deferred.finish_publication(app, ticket)
    else:
        finish_reconciliation_readback(app, ticket)
