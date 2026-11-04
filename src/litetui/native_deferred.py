"""Reviewed retirement of historical staged invocations, without replay authority."""
from __future__ import annotations

import copy
import secrets
from dataclasses import dataclass
from typing import Any

from litetui import background_input as work
from litetui import native_work as inspection

VERSION = 1
OPERATIONS = tuple("gui.work.native_deferred_" + name for name in ("validate", "discard", "readback"))
CLOSE_REQUIRED = "Deferred discard publication is uncertain; close this runtime and inspect its retained evidence."


@dataclass(eq=False)
class Review:
    capture: inspection.Capture
    bridge: work.Bridge
    raw: bytes
    document: dict
    wire: dict
    wire_bytes: bytes


@dataclass(eq=False)
class Attempt:
    review: Review
    command: bytes
    task: Any
    result: dict | None = None
    result_bytes: bytes | None = None
    after: bytes | None = None
    result_published: bool = False
    readback: dict | None = None
    readback_command: bytes | None = None
    readback_bytes: bytes | None = None
    readback_task: Any = None
    publication: Any = None
    resolved: bool = False


@dataclass(eq=False)
class Publication:
    app: Any
    attempt: Attempt
    command: bytes
    result: dict
    encoded_result: bytes
    task: Any
    phase: str
    used: bool = False


def mutation_hold(app):
    attempt = getattr(app, "_native_deferred_pending", None)
    if isinstance(attempt, Attempt) and not attempt.resolved:
        return CLOSE_REQUIRED if attempt.result and attempt.result["close_required"] else "Deferred discard awaits its exact published readback"
    return None


def _owner(app, value):
    from litetui import native_schedule

    if not native_schedule.enabled(app) or work.bridge(app) is not value:
        raise ValueError("Deferred recovery requires the exact negotiated scheduled capability")
    value.check(allow_error=True)
    if app.store.pending or not app.store.owned:
        raise ValueError("Deferred recovery requires its materialized owned target")
    app._validate_owned_identity()
    if getattr(app, "_gui_quitting", False):
        raise ValueError("Deferred recovery is unavailable while closing")


def _guard(app, value, *, current=None):
    from litetui import conversation_action, conversation_transition
    from litetui.gui_rpc import _idle, active_work

    _owner(app, value)
    _idle(app)
    conversation_action.require_open(app)
    conversation_transition.require_execution(app)
    if mutation_hold(app):
        raise ValueError(mutation_hold(app))
    if getattr(app, "_native_work_deferred", None) is not None:
        raise ValueError("A live or claimed DeferredJob owns this invocation")
    if getattr(app, "_native_schedule_pending", None) is not None:
        raise ValueError("A scheduled transition owns this runtime")
    if getattr(app, "_native_work_recapture_required", None) is not None:
        raise ValueError("Work identity recapture is still pending")
    if any(row["kind"] != "management" for row in active_work(app)):
        raise ValueError("An active worker owns this runtime")
    if getattr(app, "_pending_input", ()) or getattr(app, "_gui_host_pending", {}):
        raise ValueError("Input admission or a tool callback is active")
    if any(not worker.is_finished for worker in getattr(app, "_native_work_jobs", {}).values()):
        raise ValueError("A scheduled worker owns this runtime")
    if getattr(app, "_native_work_job_owners", {}):
        raise ValueError("A scheduled callback owner is still registered")
    if any(p.phase != "retired" for p in value.producers.values()):
        raise ValueError("Work producers have not retired")
    if any(not call.retired.is_set() for call in value.calls):
        raise ValueError("Work callbacks have not retired")
    if any(not worker.is_finished for worker, _origin in value.executions.values()) or value.pending_readbacks:
        raise ValueError("Work execution or reconciliation publication is active")
    for entry in value.inputs.values():
        if (entry.claim_task is not None
                or any(worker is not None and not worker.is_finished
                       for worker in (entry.worker, entry.admission_worker))
                or entry.observer is not None and not entry.observer.done()
                or not entry.recovered and entry.state not in {*work.TERMINAL, "held", "uncertain"}):
            raise ValueError("A Work input still has active admission ownership")
    if (getattr(app, "_native_work_stop_pending", False)
            or getattr(app, "_native_work_stop_review", None) is not None):
        raise ValueError("A Task Stop review or mutation conflicts with discard")
    captured = current or getattr(app, "_native_work_inspection", None)
    if isinstance(captured, inspection.Capture) and captured.review is not None:
        raise ValueError("Another current Work review conflicts with discard")
    if (getattr(app, "_hook_lifecycle", ())
            or getattr(app, "_hook_lock", None) and app._hook_lock.locked()
            or getattr(getattr(app, "goal_runtime", None), "_evaluating", False)
            or getattr(app, "_gui_model_pending", {})
            or any(not future.done() for future in getattr(app, "_approval_waiters", {}).values())
            or any(not item[0].is_set() for item in getattr(app, "_rpc_pending_asks", {}).values())):
        raise ValueError("A lifecycle, goal, approval or question owner is active")


def _projection(raw, document, reason):
    return {"record": copy.deepcopy(document["record"]), "journal_sha256": work.digest(raw),
            "document_state": document["state"], "input_id": document["input_id"],
            "discard": {"eligible": reason is None, "reason": reason}}


def snapshot_evidence(app):
    """Expose exact historical D; never transform it into proof of nondelivery."""
    evidence = work.deferred_document(app)
    pending = getattr(app, "_native_deferred_pending", None)
    if isinstance(pending, Attempt) and not pending.resolved:
        return _projection(pending.review.raw, pending.review.document, mutation_hold(app))
    if evidence is None or evidence[1]["state"] not in {"pending", "uncertain"}:
        return None
    raw, document = evidence
    reason = None
    try:
        _guard(app, work.bridge(app))
    except (ValueError, TypeError, OSError) as exc:
        reason = (str(exc) or type(exc).__name__)[:2048]
    return _projection(raw, document, reason)


def _capture(app, cmd, fields):
    current = inspection._capture(app, cmd, ("deferred_version", *fields))
    if type(cmd["deferred_version"]) is not int or cmd["deferred_version"] != VERSION:
        raise ValueError("Deferred recovery requires exact version 1")
    return current


def validate(app, cmd):
    current = _capture(app, cmd, ("invocation_id", "journal_sha256"))
    value = work.bridge(app)
    _guard(app, value, current=current)
    previous = getattr(app, "_native_deferred_review", None)
    if isinstance(previous, Review) and previous.capture is current:
        raise ValueError("An unconsumed deferred review already owns this snapshot")
    app._native_deferred_review = None
    selected = current.snapshot.get("deferred_job")
    evidence = work.deferred_document(app)
    if evidence is None:
        raise ValueError("Deferred journal is absent")
    raw, document = evidence
    expected = _projection(raw, document, None)
    if (document["state"] not in {"pending", "uncertain"} or selected != expected
            or cmd["invocation_id"] != document["record"]["invocation_id"]
            or cmd["journal_sha256"] != work.digest(raw)):
        raise ValueError("Deferred selected evidence changed; refresh its exact snapshot")
    result = {"kind": "work_deferred_review", "version": 1, "deferred_version": 1,
              "identity": work.identity(app), "instance": current.instance,
              "snapshot": current.snapshot["snapshot"], "review": secrets.token_hex(32),
              "invocation_id": cmd["invocation_id"], "journal_sha256": cmd["journal_sha256"],
              "record": copy.deepcopy(document["record"]), "document_state": document["state"],
              "input_id": document["input_id"], "action": "discard", "no_replay": True}
    inspection._wire(cmd, result)
    app._native_deferred_review = Review(current, value, raw, document, result, work.encoded(result))
    return result


def _receipt(attempt, cmd, status, *, reason=None):
    review = attempt.review.wire
    result = {"kind": "work_deferred_result", "version": 1, "deferred_version": 1,
              "identity": copy.deepcopy(review["identity"]), "instance": review["instance"],
              "operation_id": cmd["id"], "review": review["review"],
              "invocation_id": review["invocation_id"], "status": status,
              "before_sha256": review["journal_sha256"],
              "after_sha256": work.digest(attempt.after) if attempt.after is not None else None,
              "close_required": status == "uncertain", "reason": reason}
    inspection._wire(cmd, result)
    attempt.result, attempt.result_bytes = result, work.encoded(result)
    return result


def discard(app, cmd):
    review = getattr(app, "_native_deferred_review", None)
    if not isinstance(review, Review):
        raise ValueError("No unconsumed deferred discard review")  # noqa: TRY004 - missing one-use state, not a caller type error
    # Even a malformed invocation cannot reuse a review after attempting an effect.
    app._native_deferred_review = None
    attempt = Attempt(review, work.encoded(cmd), work._task())
    app._native_deferred_last = attempt
    try:
        current = _capture(app, cmd, ("review", "invocation_id", "journal_sha256"))
        if (current is not review.capture or work.encoded(review.wire) != review.wire_bytes
                or any(cmd[key] != review.wire[key] for key in ("review", "invocation_id", "journal_sha256"))):
            raise ValueError("Deferred discard does not match the exact retained review")
        _guard(app, review.bridge, current=current)
        evidence = work.deferred_document(app)
        if evidence is None or evidence[0] != review.raw or evidence[1] != review.document:
            raise ValueError("Deferred journal changed after review")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return _receipt(attempt, cmd, "refused", reason=(str(exc) or type(exc).__name__)[:2048])
    terminal = {"version": 2, "state": "discarded", "input_id": review.document["input_id"],
                "record": copy.deepcopy(review.document["record"]),
                "retirement": {"operation_id": cmd["id"], "review": review.wire["review"],
                    "previous_sha256": review.wire["journal_sha256"],
                    "previous_state": review.document["state"], "identity": work.identity(app),
                    "reason": "user_discarded", "no_replay": True, "effects_undone": False}}
    # Reserve before the first possible write and retain across all ambiguity.
    app._native_deferred_pending = attempt
    try:
        attempt.after = work._write_deferred(app, terminal, review.raw)
        evidence = work.deferred_document(app)
        if evidence is None or evidence[0] != attempt.after or evidence[1] != terminal:
            raise OSError("Deferred terminal audit readback differs")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return _receipt(attempt, cmd, "uncertain", reason=(str(exc) or type(exc).__name__)[:2048])
    return _receipt(attempt, cmd, "discarded")


def _retained(app, attempt):
    _owner(app, attempt.review.bridge)
    if inspection._owner(app, inspection._identity(app)) != attempt.review.capture.owner:
        raise ValueError("Deferred discard runtime references changed")
    evidence = work.deferred_document(app)
    if evidence is None or attempt.after is None or evidence[0] != attempt.after:
        raise ValueError("Deferred terminal audit changed after discard")


def readback(app, cmd):
    inspection._common(app, cmd, ("deferred_version", "instance", "operation_id", "review", "invocation_id", "after_sha256"))
    attempt = getattr(app, "_native_deferred_pending", None)
    if (type(cmd["deferred_version"]) is not int or cmd["deferred_version"] != VERSION
            or not isinstance(attempt, Attempt) or attempt.resolved or attempt.result is None
            or attempt.result["status"] != "discarded" or not attempt.result_published
            or any(cmd[key] != attempt.result[key] for key in ("instance", "operation_id", "review", "invocation_id", "after_sha256"))):
        raise ValueError("Deferred readback lacks its exact published discard operation")
    _retained(app, attempt)
    result = {"kind": "work_deferred_readback", "version": 1, "deferred_version": 1,
              "identity": work.identity(app),
              **{key: cmd[key] for key in ("instance", "operation_id", "review", "invocation_id", "after_sha256")},
              "verified": True, "effects_undone": False, "no_replay": True}
    inspection._wire(cmd, result)
    if attempt.readback is not None:
        raise ValueError("Deferred readback is already reserved for publication")
    attempt.readback, attempt.readback_command, attempt.readback_task = result, work.encoded(cmd), work._task()
    attempt.readback_bytes = work.encoded(result)
    return result


def prepare_publication(app, cmd, result):
    if cmd.get("type") not in OPERATIONS[1:]:
        return None
    attempt = getattr(app, "_native_deferred_last", None)
    phase = "readback" if cmd["type"] == OPERATIONS[2] else "result"
    if not isinstance(attempt, Attempt):
        raise ValueError("Deferred publication has no exact operation owner")  # noqa: TRY004 - missing runtime state
    expected, command, task = ((attempt.readback, attempt.readback_command, attempt.readback_task)
                              if phase == "readback" else (attempt.result, attempt.command, attempt.task))
    if (expected is None or command is None or expected is not result
            or command != work.encoded(cmd) or task is not work._task()
            or attempt.publication is not None or attempt.resolved
            or phase == "result" and (attempt.result_published or work.encoded(result) != attempt.result_bytes)
            or phase == "readback" and work.encoded(result) != attempt.readback_bytes):
        raise ValueError("Deferred publication lost its exact unconsumed response")
    ticket = Publication(app, attempt, command, expected, work.encoded(result), task, phase)
    attempt.publication = ticket
    return ticket


def finish_publication(app, ticket):
    if (not isinstance(ticket, Publication) or ticket.app is not app or ticket.used
            or ticket.task is not work._task() or ticket.attempt.publication is not ticket
            or work.encoded(ticket.result) != ticket.encoded_result
            or getattr(app, "_native_deferred_last", None) is not ticket.attempt):
        raise ValueError("Deferred publication is foreign, changed or consumed")
    ticket.used = True
    attempt: Attempt = ticket.attempt
    _owner(app, attempt.review.bridge)
    if ticket.phase == "result":
        attempt.result_published = True
        attempt.publication = None
        return
    if getattr(app, "_native_deferred_pending", None) is not attempt:
        raise ValueError("Deferred readback no longer owns its admission barrier")
    _retained(app, attempt)
    attempt.resolved = True
    attempt.publication = None
    app._native_deferred_pending = None
