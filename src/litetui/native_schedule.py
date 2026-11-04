"""Owned scheduled conversation handoff and exact response-publication barriers."""
from __future__ import annotations

import copy
import secrets
from dataclasses import dataclass
from typing import Any

from litetui import background_input as work

VERSION = 1
OPERATIONS = (
    "gui.work.native_schedule_accept", "gui.work.native_schedule_decline", "gui.work.native_schedule_readback",
    "gui.work.native_deferred_validate", "gui.work.native_deferred_discard", "gui.work.native_deferred_readback",
)


@dataclass(eq=False)
class ActivationPublication:
    app: Any
    bridge: work.Bridge
    command: bytes
    result: dict
    encoded_result: bytes
    task: Any
    deferred: Any = None
    prepared: bool = False
    used: bool = False


def enabled(app):
    value = work.bridge(app)
    return value is not None and value.active and value.scheduled_transition_version == VERSION


def activation(app, cmd, value):
    if value.activation_publication is not None:
        raise ValueError("Work activation publication was already reserved")
    result = {"kind": "work_active", "version": work.VERSION, "instance": value.instance,
              "sequence": 0, "identity": dict(value.identity)}
    for key in ("scheduled_transition_version", "execution_version"):
        if key in cmd:
            result[key] = cmd[key]
    value.activation_publication = ActivationPublication(app, value, work.encoded(cmd), result,
                                                        work.encoded(result), work._task(),
                                                        getattr(app, "_native_work_deferred", None))
    return result


def prepare_publication(app: Any, cmd: dict, result: dict) -> ActivationPublication | DecisionPublication | None:
    if cmd.get("type") in {"gui.work.native_schedule_accept", "gui.work.native_schedule_decline", "gui.work.native_schedule_readback"}:
        pending = getattr(app, "_native_schedule_pending", None)
        phase = "readback" if cmd["type"].endswith("_readback") else "result"
        expected = getattr(pending, "readback_result", None) if phase == "readback" else getattr(pending, "result", None)
        expected_bytes = getattr(pending, "readback_bytes", None) if phase == "readback" else getattr(pending, "result_bytes", None)
        expected_command = getattr(pending, "readback_command", None) if phase == "readback" else getattr(pending, "decision_command", None)
        if (not isinstance(pending, PendingScheduledTransition) or pending.app is not app
                or expected is not result or work.encoded(result) != expected_bytes
                or expected_command != work.encoded(cmd)):
            raise ValueError("Scheduled publication lost its exact result")
        if pending.retired:
            _retired_owner(app, pending)
        ticket = DecisionPublication(app, pending, work.encoded(cmd), result, work.encoded(result), work._task(), phase, prepared=True)
        if getattr(pending, "publication", None) is not None:
            raise ValueError("Scheduled response publication is already reserved")
        pending.publication = ticket
        return ticket
    if cmd.get("type") != "gui.work.native_activate" or not {"scheduled_transition_version", "execution_version"} & cmd.keys():
        return None
    value = work.bridge(app)
    activation_ticket = None if value is None else value.activation_publication
    if (not isinstance(activation_ticket, ActivationPublication) or activation_ticket.app is not app or activation_ticket.bridge is not value
            or activation_ticket.task is not work._task() or activation_ticket.command != work.encoded(cmd)
            or activation_ticket.result is not result or activation_ticket.encoded_result != work.encoded(result)
            or activation_ticket.prepared or activation_ticket.used):
        raise ValueError("Activation publication lacks its exact unconsumed owner")
    value.check()
    activation_ticket.prepared = True
    return activation_ticket


def finish_publication(app, ticket):
    if isinstance(ticket, DecisionPublication):
        pending = ticket.pending
        if (ticket.app is not app or not ticket.prepared or ticket.used or ticket.task is not work._task()
                or getattr(app, "_native_schedule_pending", None) is not pending
                or pending.publication is not ticket or work.encoded(ticket.result) != ticket.encoded_result):
            raise ValueError("Scheduled publication is foreign, changed or consumed")
        if pending.retired:
            _retired_owner(app, pending)
        ticket.used = True
        pending.publication = None
        if ticket.phase == "result":
            pending.result_published = True
        else:
            pending.readback_published = True
            if pending.state in {"declined", "refused"}:
                app._native_schedule_last, app._native_schedule_pending = pending, None
        return
    if (not isinstance(ticket, ActivationPublication) or ticket.app is not app or not ticket.prepared
            or ticket.used or ticket.task is not work._task() or work.bridge(app) is not ticket.bridge
            or ticket.bridge.activation_publication is not ticket
            or ticket.encoded_result != work.encoded(ticket.result)):
        raise ValueError("Activation publication is foreign, changed or consumed")
    ticket.used = True
    value = ticket.bridge
    try:
        value.check()
        if getattr(app, "_gui_quitting", False):
            raise ValueError("Quit closed activation before response publication")
        deferred = getattr(app, "_native_work_deferred", None)
        if deferred is not ticket.deferred:
            raise ValueError("Activation publication lost its exact deferred owner")
        schedule_owner = getattr(deferred, "schedule_owner", None)
        if schedule_owner is not None:
            if not schedule_owner.readback_published or getattr(app, "_native_schedule_pending", None) is not schedule_owner:
                raise ValueError("Scheduled activation lacks exact published readback")
            app._native_schedule_last, app._native_schedule_pending = schedule_owner, None
        work._activate(app, value, scheduled_transition_version=ticket.result.get("scheduled_transition_version"),
                       execution_version=ticket.result.get("execution_version"))
    except BaseException:
        value.closing = True
        value.error = "Work activation publication is uncertain; close without replay"
        raise

@dataclass(eq=False)
class PendingScheduledTransition:
    app: Any
    bridge: work.Bridge
    review: Any
    job: Any
    proposal: dict
    proposal_bytes: bytes
    state: str = "proposed"
    retired: bool = False
    operation: str | None = None
    worker: Any = None
    task: Any = None
    result: dict | None = None
    result_bytes: bytes | None = None
    stamp: dict | None = None
    stamp_attempted: bool = False
    result_published: bool = False
    readback_published: bool = False
    decision_command: bytes | None = None
    readback_command: bytes | None = None
    readback_result: dict | None = None
    readback_bytes: bytes | None = None
    publication: Any = None
    transition: dict | None = None


def _retired_owner(app, pending):
    """Historical proposal facts never restore an old epoch's execution authority."""
    if (pending.app is not app or pending.bridge.app is not app or not pending.retired
            or pending.state not in {"proposed", "declined", "refused"}
            or app._agent_session is not pending.review.context.session
            or app._agent_session is not pending.bridge.owner[0]
            or work.identity(app)["agent_id"] != pending.bridge.identity["agent_id"]
            or pending.stamp_attempted or pending.stamp is not None or pending.transition is not None
            or getattr(getattr(app, "_native_work_deferred", None), "schedule_owner", None) is pending
            or work.encoded(pending.proposal) != pending.proposal_bytes):
        raise ValueError("Retired scheduled proposal lost its exact no-effect owner")
    app._validate_owned_identity()


def retire_proposal(app, value):
    """Keep one inert proposal until its late no-effect decision is published."""
    pending = getattr(app, "_native_schedule_pending", None)
    if (not isinstance(pending, PendingScheduledTransition) or pending.app is not app
            or pending.bridge is not value or pending.state not in {"proposed", "declined", "refused"}
            or pending.stamp_attempted):
        return
    pending.retired = True
    _retired_owner(app, pending)


def offer(app, job, *, manual=False):
    """Propose from canonical lease-protected PRE state; never prepare or stamp."""
    if manual or not enabled(app):
        return False
    from copy import deepcopy
    from dataclasses import asdict
    from datetime import datetime

    from litetui import (
        conversation_transition,
        native_work_controls,
        paths,
        scheduler,
        seat_authority,
    )
    from litetui.event_origin import EventOrigin
    from litetui.shared_state import Lease, OwnershipError

    value = work.bridge(app)
    if value is None:
        return False
    try:
        value.check()
        app._validate_owned_execution()
        if why := admission_refusal(app):
            raise ValueError(why)
        if getattr(app, "_gui_schedules_paused", False):
            return True
        if getattr(app, "_native_work_deferred", None) is not None or work.deferred_evidence(app):
            raise ValueError("A staged invocation must be resolved before another proposal")
        pending = getattr(app, "_native_schedule_pending", None)
        if pending is not None:
            return True  # Keep its exact token identifiable through a safe decision.
        path = scheduler.jobs_path(paths.data_root())
        with Lease(paths.data_root() / ".scheduler.lease"):
            rows, revision = native_work_controls.read_jobs(path)
            if not any(row["id"] == job.id for row in rows):
                return True
            now = datetime.now()  # noqa: DTZ005 - canonical scheduler local wall time
            candidate = app._fire_job_candidate_owned(deepcopy(job), manual=False, now=now)
            if candidate is None:
                return True
            if not candidate.new_conversation:
                return False
            if seat_authority.withheld(app, scheduler.level_of(candidate.tool_profile)):
                return True
            snapshot = work.encoded(asdict(candidate))
            payload = {"content": candidate.prompt, "text": candidate.prompt,
                       "tool_profile": scheduler.level_of(candidate.tool_profile), "source": "scheduled"}
            work.payload_text(candidate.prompt)
            if len(snapshot) > work.LIMITS["payload_bytes"] or len(work.encoded(payload)) > work.LIMITS["payload_bytes"]:
                raise ValueError("Scheduled proposal exceeds its exact PRE or payload byte bound")
            previous = getattr(app, "_native_schedule_last", None)
            slot = scheduler.slot_of(now)
            job_hash = work.digest(snapshot)
            if previous is not None and (previous.proposal["job_id"], previous.proposal["due_slot"], previous.proposal["job_sha256"]) == (job.id, slot, job_hash):
                return True
            common = {"version": work.VERSION, **work.identity(app)}
            state = conversation_transition.snapshot(app, common)
            review, wire = conversation_transition.build_review(app, {**common, "snapshot": state["snapshot"], "action": "new"})
            if native_work_controls.read_jobs(path)[1] != revision:
                raise ValueError("Canonical scheduler file changed during proposal review")
            proposal = {"proposal_id": work.ident(), "proposal_token": secrets.token_hex(32),
                        "trigger": "automatic", "job_id": candidate.id, "kind": candidate.kind,
                        "source": "scheduled", "requested_profile": scheduler.level_of(candidate.tool_profile),
                        "due_slot": slot, "prior_run_count": candidate.run_count,
                        "job_snapshot": snapshot.decode("utf-8"), "job_sha256": job_hash, "transition": wire}
            row = {"type": "work_transition_proposed", "version": work.VERSION, "instance": value.instance,
                   "sequence": value.sequence + 1, "identity": value.identity,
                   "scheduled_transition_version": VERSION, "proposal": proposal}
            envelope = {"type": "gui.event", "event_id": work.ident(),
                        **EventOrigin(session_id=app.convo_id, source="work").envelope_fields(), "event": row}
            if len(work.encoded(envelope)) > work.LIMITS["envelope_bytes"]:
                raise ValueError("Scheduled proposal exceeds the negotiated event byte bound")
            pending = PendingScheduledTransition(app, value, review, job, proposal, work.encoded(proposal))
        app._native_schedule_pending = pending
        value.emit("work_transition_proposed", scheduled_transition_version=VERSION, proposal=proposal)
        return True
    except OwnershipError:
        return True
    except (ValueError, OSError) as error:
        reason = (str(error) or type(error).__name__).encode("utf-8")[:384].decode("utf-8", "ignore")
        work._hold_scheduled_job(app, job, "Scheduled transition is held: " + reason, manual=False)
        return True

@dataclass(eq=False)
class DecisionPublication:
    app: Any
    pending: PendingScheduledTransition
    command: bytes
    result: dict
    encoded_result: bytes
    task: Any
    phase: str
    prepared: bool = False
    used: bool = False


def accepted_owner(app, job=None):
    from textual.worker import NoActiveWorker, get_current_worker
    pending = getattr(app, "_native_schedule_pending", None)
    if not isinstance(pending, PendingScheduledTransition) or pending.app is not app or pending.state != "accepting":
        return None
    try:
        worker = get_current_worker()
    except NoActiveWorker:
        return None
    owner = getattr(app, "_native_work_job_owners", {}).get(worker)
    if (pending.worker is not worker or pending.task is not work._task()
            or owner is None or owner["task"] is not work._task()
            or getattr(app, "_native_work_jobs", {}).get(pending.job.id) is not worker
            or job is not None and pending.job is not job):
        return None
    return pending


def admission_refusal(app):
    from litetui import native_deferred
    if why := native_deferred.mutation_hold(app):
        return why
    pending = getattr(app, "_native_schedule_pending", None)
    if (isinstance(pending, PendingScheduledTransition)
            and pending.state not in {"proposed", "declined", "refused"} and accepted_owner(app) is None):
        return "A scheduled conversation transition owns admission until its published capture"
    return None


# Reads used for independently verifying the old and target conversations grant
# no execution exemption. Keep this exact list separate from generic engine
# recovery operations, some of which are mutations despite being inspectable.
INSPECTION = frozenset({
    "gui.hello", "gui.state", "gui.conversations.native_snapshot",
    "gui.conversations.read", "gui.conversations.list", "gui.conversations.page", "gui.conversations.preview",
    "gui.history.native_snapshot", "gui.history.native_page", "gui.restored_input.list",
    "gui.work.native_snapshot", "gui.work.native_page", "gui.work.native_tail", "gui.work.native_calendar",
    "gui.settings.get", "gui.settings.native_snapshot", "gui.settings.native_compact_snapshot",
    "gui.models.list", "gui.models.capabilities", "gui.backends.list", "gui.jobs.list", "gui.tasks.list",
    "gui.goals.get", "gui.tools.list", "gui.skills.list", "gui.hooks.get", "gui.mcp.list",
    "gui.commands.list", "gui.extensions.list", "gui.system.get", "gui.thinking.get", "gui.tool_policy.get",
    "list_models", "get_settings", "list_commands", "list_skills", "jobs.list", "tasks.list",
})


def operation_allowed(app, operation):
    from litetui import native_deferred
    why = admission_refusal(app)
    if why is None or operation in INSPECTION or operation in {"shutdown", "abort", "gui.work.native_settled"}:
        return
    if native_deferred.mutation_hold(app):
        if operation == "gui.work.native_deferred_readback":
            return
    elif operation in {"gui.work.native_schedule_readback", "gui.work.native_capture", "gui.work.native_activate"}:
        return
    raise ValueError(why)


def capture_allowed(app):
    pending = getattr(app, "_native_schedule_pending", None)
    if isinstance(pending, PendingScheduledTransition) and pending.state == "staged" and not pending.readback_published:
        raise ValueError("Scheduled handoff requires its exact published readback before capture")


def preflight(app, job, *, manual, now):
    """Called under the existing scheduler lease, before prepare_fire mutates a loop."""
    from dataclasses import asdict

    from litetui import (
        conversation_transition,
        native_work,
        native_work_controls,
        paths,
        scheduler,
    )
    pending = accepted_owner(app, job)
    if pending is None:
        return None
    value = pending.bridge
    value.check()
    if manual or work.bridge(app) is not value or not enabled(app) or getattr(app, "_gui_quitting", False):
        raise ValueError("Scheduled acceptance lost its exact automatic runtime")
    proposal = pending.proposal
    if (pending.proposal_bytes != work.encoded(proposal) or scheduler.slot_of(now) != proposal["due_slot"]
            or work.encoded(asdict(job)).decode("utf-8") != proposal["job_snapshot"]):
        raise ValueError("Scheduled proposal expired or canonical PRE job changed")
    rows, _revision = native_work_controls.read_jobs(scheduler.jobs_path(paths.data_root()))
    row = next((row for row in rows if row["id"] == job.id), None)
    if row is None or work.encoded(native_work._job(row)["row"]).decode("utf-8") != proposal["job_snapshot"]:
        raise ValueError("Scheduled canonical PRE row changed")
    if not scheduler.due([job], now) or not job.new_conversation:
        raise ValueError("Scheduled automatic job is no longer due")
    state = conversation_transition.snapshot(app, {"version": 1, **work.identity(app)})
    if state["snapshot"] != proposal["transition"]["snapshot"] or not state["actions"]["new"]["eligible"]:
        raise ValueError("Scheduled conversation review is stale or no longer quiet")
    conversation_transition._unchanged(app, pending.review)
    return pending


def stamped(app, job):
    from dataclasses import asdict
    pending = accepted_owner(app, job)
    if pending is not None:
        snapshot = work.encoded(asdict(job)).decode("utf-8")
        pending.stamp = {"slot": job.last_fired_slot, "run_count": job.run_count,
                         "job_snapshot": snapshot, "job_sha256": work.digest(snapshot.encode("utf-8"))}


def _command(app, cmd, action, extras, pending=None):
    expected_identity = None
    if isinstance(pending, PendingScheduledTransition) and pending.retired:
        _retired_owner(app, pending)
        expected_identity = pending.bridge.identity
    work._command(app, cmd, "schedule_" + action, ("schedule_version", *extras),
                  admission=False, expected_identity=expected_identity)
    if type(cmd["schedule_version"]) is not int or cmd["schedule_version"] != VERSION:
        raise ValueError("Scheduled handoff requires exact version1")


def _selected(app, cmd, action):
    pending = getattr(app, "_native_schedule_pending", None)
    _command(app, cmd, action, ("instance", "proposal_id", "proposal_token", *(("reason",) if action == "decline" else ())), pending)
    if (not isinstance(pending, PendingScheduledTransition) or pending.app is not app
            or pending.state != "proposed" or cmd["instance"] != pending.bridge.instance
            or any(cmd[key] != pending.proposal[key] for key in ("proposal_id", "proposal_token"))
            or work.encoded(pending.proposal) != pending.proposal_bytes):
        raise ValueError("Unknown, changed or consumed scheduled proposal")
    if not pending.retired and (not enabled(app) or work.bridge(app) is not pending.bridge
            or work.identity(app) != pending.bridge.identity
            or app._agent_session is not pending.review.context.session
            or app.store is not pending.review.context.store or app.backend is not pending.review.context.backend):
        raise ValueError("Unknown, changed or consumed scheduled proposal")
    return pending


def _decision(pending, operation, status, *, reason=None):
    deferred = getattr(pending.app, "_native_work_deferred", None)
    deferred_record = deferred_hash = None
    if deferred is not None and getattr(deferred, "schedule_owner", None) is pending:
        deferred_record = copy.deepcopy(deferred.document["record"])
        deferred_hash = work.digest(deferred.raw)
    result = {"kind": "work_schedule_result", "version": VERSION, "schedule_version": VERSION,
        "operation_id": operation, "old_instance": pending.bridge.instance,
        "proposal_id": pending.proposal["proposal_id"], "proposal_token": pending.proposal["proposal_token"],
        "pre_job_sha256": pending.proposal["job_sha256"], "status": status, "reason": reason,
        "stamp": pending.stamp, "transition": getattr(pending, "transition", None),
        "deferred": deferred_record, "deferred_journal_sha256": deferred_hash,
        "close_required": status == "uncertain"}
    pending.state, pending.operation = status, operation
    pending.result, pending.result_bytes = result, work.encoded(result)
    return result


def decline(app, cmd):
    pending = _selected(app, cmd, "decline")
    reason = work.text(cmd["reason"], 2048)
    pending.decision_command = work.encoded(cmd)
    return _decision(pending, cmd["id"], "declined", reason=reason)


async def accept(app, cmd):
    import asyncio

    from textual.worker import get_current_worker

    from litetui.shutdown_wait import join_task
    pending = _selected(app, cmd, "accept")
    pending.decision_command = work.encoded(cmd)
    if pending.retired:
        return _decision(pending, cmd["id"], "refused", reason="Scheduled proposal was retired by a human identity transition before effects")
    pending.state, pending.operation = "accepting", cmd["id"]
    if (getattr(app, "_gui_quitting", False) or getattr(app, "_conversation_transition_shutdown_requested", False)
            or getattr(app, "_conversation_rewrite_closing", False) or pending.bridge.closing):
        return _decision(pending, cmd["id"], "refused", reason="Scheduled acceptance stopped before effects because the runtime is closing")
    async def execute():
        pending.task = work._task()
        try:
            app._validate_owned_execution()
            result = await work.run_job(app, pending.job, manual=False)
            deferred = getattr(app, "_native_work_deferred", None)
            if (not isinstance(result, dict) or result.get("awaiting_work_capture") is not True
                    or deferred is None or pending.stamp is None or getattr(pending, "transition", None) is None):
                raise ValueError("Accepted scheduled invocation did not produce its exact staged proof")
            deferred.schedule_owner = pending
            return _decision(pending, cmd["id"], "staged")
        except BaseException as exc:  # noqa: BLE001 - cancelled effects retain exact ownership and evidence
            reason = (str(exc) or type(exc).__name__).encode("utf-8")[:2048].decode("utf-8", "ignore")
            if pending.stamp_attempted:
                pending.bridge.error = "Scheduled execution is uncertain; close without replay"
                pending.bridge.closing = True
                app._conversation_transition_error = pending.bridge.error
            return _decision(pending, cmd["id"], "uncertain" if pending.stamp_attempted else "refused", reason=reason)
    worker = app.run_worker(execute(), group="work-jobs", exclusive=False, exit_on_error=False)
    pending.worker = worker
    if not hasattr(app, "_native_work_jobs"):
        app._native_work_jobs = {}
    if not hasattr(app, "_native_work_job_owners"):
        app._native_work_job_owners = {}
    app._native_work_jobs[pending.job.id] = worker
    app._native_work_job_owners[worker] = {"parent": get_current_worker(), "parent_task": work._task(), "task": None}
    return await join_task(asyncio.create_task(worker.wait()))


def readback(app, cmd):

    from litetui import native_work, native_work_controls, paths, scheduler
    from litetui.shared_state import Lease
    candidate = getattr(app, "_native_schedule_pending", None)
    _command(app, cmd, "readback", ("old_instance", "proposal_id", "proposal_token", "accepted_operation_id", "result_sha256"), candidate)
    if not isinstance(candidate, PendingScheduledTransition):
        raise ValueError("Scheduled readback lacks its exact published decision")  # noqa: TRY004 - unknown live capability is a protocol refusal
    pending: PendingScheduledTransition = candidate
    if (pending.app is not app or not pending.result_published
            or pending.result is None or pending.state not in {"staged", "refused", "declined"}
            or pending.result_bytes != work.encoded(pending.result)
            or cmd["old_instance"] != pending.bridge.instance or cmd["accepted_operation_id"] != pending.operation
            or cmd["result_sha256"] != work.digest(pending.result_bytes)
            or any(cmd[key] != pending.proposal[key] for key in ("proposal_id", "proposal_token"))):
        raise ValueError("Scheduled readback lacks its exact published decision")
    expected_identity = dict(pending.bridge.identity)
    if pending.state == "staged":
        expected_identity["session_id"] = pending.proposal["transition"]["target_session_id"]
    context = pending.review.context
    if not pending.retired and (work.identity(app) != expected_identity or app._agent_session is not context.session
            or app.store is not context.store or app.backend is not context.backend):
        raise ValueError("Scheduled readback lost its exact expected runtime and identity")
    if pending.readback_result is not None:
        raise ValueError("Scheduled readback was already reserved; never replay its publication")
    receipt = pending.result
    stamp = pending.stamp
    job_readback = deferred_readback = None
    if pending.state == "staged":
        deferred = getattr(app, "_native_work_deferred", None)
        if (deferred is None or id(getattr(deferred, "schedule_owner", None)) != id(pending) or deferred.claimed
                or stamp is None or receipt is None
                or deferred.session is not app._agent_session or deferred.store is not app.store
                or work._read(deferred.path, work.LIMITS["reply_bytes"]) != deferred.raw):
            raise ValueError("Staged scheduled ownership or durable evidence changed")
        with Lease(paths.data_root() / ".scheduler.lease"):
            rows, _revision = native_work_controls.read_jobs(scheduler.jobs_path(paths.data_root()))
            row = next((row for row in rows if row["id"] == pending.proposal["job_id"]), None)
            if row is None:
                raise ValueError("Staged job was removed before readback")
            snapshot = work.encoded(native_work._job(row)["row"]).decode("utf-8")
        job_readback = {"job_snapshot": snapshot, "job_sha256": work.digest(snapshot.encode("utf-8"))}
        if any(job_readback[key] != stamp[key] for key in job_readback):
            raise ValueError("Staged canonical POST job changed")
        deferred_readback = {"document_sha256": work.digest(deferred.raw), "record": deferred.document["record"],
                             "state": deferred.document["state"], "input_id": deferred.document["input_id"]}
        if (deferred_readback["record"] != receipt["deferred"]
                or deferred_readback["document_sha256"] != receipt["deferred_journal_sha256"]
                or deferred_readback["state"] != "pending" or deferred_readback["input_id"] is not None):
            raise ValueError("Staged deferred evidence differs from its receipt")
    result = {"kind": "work_schedule_readback", "version": VERSION, "schedule_version": VERSION,
              "identity": expected_identity, "old_instance": pending.bridge.instance,
              "proposal_id": pending.proposal["proposal_id"], "accepted_operation_id": pending.operation,
              "result_sha256": cmd["result_sha256"], "result": pending.result,
              "job_readback": job_readback, "deferred_readback": deferred_readback, "verified": True}
    pending.readback_result = result
    pending.readback_bytes, pending.readback_command = work.encoded(result), work.encoded(cmd)
    return result
