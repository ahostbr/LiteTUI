"""One-shot host decisions before an owned F6 replacement is published."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from litetui import (
    approval_authority,
    approval_relay,
    backend_catalog,
    seat_authority,
    tool_approval,
)
from litetui import conversation_action as action
from litetui import history_projection as projection
from litetui.agent_store import TRANSCRIPT_NAME, _unlinked
from litetui.event_origin import EventOrigin


def capture(app, document):
    attempt = action._attempt(app)
    if (attempt is not None and attempt.kind == "rewrite" and attempt.pending and attempt.prewrite_enabled
            and not attempt.mutated and attempt.execution is None
            and action._authorized(app) and attempt.owner is asyncio.current_task()
            and document.get("event") == "prompt_before" and document.get("source") == "rpc"
            and document.get("conversation_id") == app.convo_id):
        return attempt, document["turn_id"]
    return None


def route_allowed(app, route):
    return (app._rpc and getattr(app, "_gui_rpc_enabled", False)
            and route in {"host", "hand", "own"} and route == seat_authority.confirm_route(app)
            and approval_relay.current_spawner(app) is None)


@dataclass
class Request:
    attempt: action.Attempt
    origin: EventOrigin
    marker: dict
    ident: str
    directory: Path
    route: str
    future: asyncio.Future
    deadline: float | None
    audit: dict
    proofs: dict = field(default_factory=dict)
    outcome: str | None = None

    def owner(self, app):
        if (action._attempt(app) is not self.attempt or not self.attempt.pending
                or app.store is not self.attempt.review.objects[1]
                or app._agent_session is not self.attempt.review.objects[0]
                or app.store.convo_id != self.origin.session_id
                or app.convo_dir != self.directory or app.store.convo_path != self.directory / TRANSCRIPT_NAME):
            raise ValueError("Prewrite approval lost its reviewed conversation")

    def ready(self, app, *, decision_only=False):
        self.owner(app)
        if (app.store.persist_error or app.store.pending or app.store.loading
                or self.attempt.stopped or getattr(app, "_stop_requested", False)
                or getattr(app, "_conversation_rewrite_closing", False)
                or not route_allowed(app, self.route)):
            raise ValueError("Prewrite approval was stopped or lost its reviewed decider")
        action._stable(app, self.attempt.review, original=True, decision_only=decision_only)

    def verify(self, app):
        self.owner(app)
        for path, expected in self.proofs.items():
            if _unlinked(path).stat().st_nlink != 1 or backend_catalog._disk_hash(path, 4096) != expected:
                raise OSError("Prewrite approval audit readback differs from its publication")

    def publish(self, app, outcome):
        self.owner(app)
        self.verify(app)
        stage = "pending" if outcome == "pending" else "closed"
        path = _unlinked(self.directory / f"rewrite-approval-{self.origin.execution_id}.{stage}.json")
        data = projection.encoded({**self.audit, "outcome": outcome})
        if len(data) > 4096:
            raise ValueError("Prewrite approval audit exceeds its bound")
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.proofs[path] = projection.digest(data)
        self.verify(app)
        if stage == "closed":
            self.outcome = outcome


def verify_publication(app, attempt):
    for request in attempt.prewrite_approvals:
        try:
            request.verify(app)
        except (OSError, ValueError, RuntimeError):
            attempt.prewrite_audit_error = True
            raise
        if request.outcome != "approved" or len(request.proofs) != 2:
            raise ValueError("Prewrite approval was not durably granted")


def settled(app, attempt):
    rows = []
    for request in attempt.prewrite_approvals:
        request.verify(app)
        if request.outcome is None or len(request.proofs) != 2:
            raise OSError("Prewrite approval lacks its terminal audit")
        rows.append({"id": request.ident, "execution_id": request.origin.execution_id, "outcome": request.outcome})
    return rows


def resolve(app, ident, marker, allow, remember):
    """None delegates a normal request to the existing approval authority."""
    attempt = action._attempt(app)
    request = next((row for row in attempt.prewrite_approvals if row.ident == ident), None) if attempt else None
    if request is None:
        return False if marker is not None else None
    if (marker != request.marker or remember or request.outcome is not None or request.future.done()
            or (request.deadline is not None and time.monotonic() >= request.deadline)):
        return False
    try:
        # The RPC decider may record this answer. Only the captured admission task
        # may pass execution validation and proceed to the hook.
        request.ready(app, decision_only=True)
        request.publish(app, "approved" if allow else "denied")
    except (OSError, ValueError, RuntimeError) as exc:
        request.future.set_exception(exc)
        return False
    request.future.set_result(tool_approval.ONCE if allow else tool_approval.DENIED)
    return True


async def ask(app, scope, route, name, args, decision):
    attempt, hook = scope
    if len(attempt.prewrite_approvals) >= 32:
        raise ValueError("Prewrite approval request limit exceeded")
    wire = json.loads(attempt.review.wire)
    origin = EventOrigin(session_id=wire["session_id"], operation_id=attempt.operation,
                         source="rpc", hook_id=hook).scheduled()
    marker = {"session_id": origin.session_id, "operation_id": origin.operation_id,
              "action_id": wire["action_id"], "execution_id": origin.execution_id}
    ident = "appr-" + uuid4().hex
    timeout = tool_approval.approval_timeout_s()
    if timeout is not None and not math.isfinite(timeout):
        raise ValueError("Prewrite approval requires a valid host timeout")
    future = asyncio.get_running_loop().create_future()
    audit = {"version": 1, "kind": "rewrite_hook_approval", **marker, "hook_id": hook,
             "agent_id": wire["agent_id"], "approval_id": ident, "route": route,
             "review": wire["review"], "tool": name, "profile": decision.profile,
             "input_sha256": projection.digest(projection.encoded(args))}
    request = Request(attempt, origin, marker, ident, app.convo_dir, route, future,
                      None if timeout is None else time.monotonic() + timeout, audit)
    request.ready(app)
    attempt.prewrite_approvals.append(request)
    requested = False
    reason = "error"
    try:
        request.publish(app, "pending")
        tool_approval._approval_registry(app)[ident] = future
        app._rpc_emit({"type": "tool_approval_requested", "id": ident, "tool": name, "input": args,
                       "profile": decision.profile, "why": "Before saving the reviewed edit/retry: " + decision.reason,
                       "timeout_s": timeout, "rewrite_prewrite": marker}, origin=origin)
        requested = True
        while not future.done():
            remaining = None if request.deadline is None else request.deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                reason = "timeout"
                return None
            if attempt.stopped or getattr(app, "_conversation_rewrite_closing", False):
                reason = "cancelled"
                return None
            request.owner(app)
            if (app.store.persist_error or app.store.pending or app.store.loading
                    or app.conversation is not attempt.review.conversation or not route_allowed(app, route)):
                reason = "revoked"
                return None
            await asyncio.wait({future}, timeout=0.25 if remaining is None else min(0.25, remaining))
        answer = future.result()
        approval_authority.raise_if_cancelled()
        if request.deadline is not None and time.monotonic() >= request.deadline:
            reason = "timeout"
            return None
        if (request.outcome != ("approved" if answer else "denied")
                or len(request.proofs) != 2):
            reason = "revoked"
            return None
        request.ready(app)
        request.verify(app)
        reason = "answered"
        return answer
    except asyncio.CancelledError:
        reason = "cancelled"
        return None
    except (OSError, ValueError, RuntimeError):
        reason = "revoked"
        return None
    finally:
        tool_approval._approval_registry(app).pop(ident, None)
        try:
            if request.outcome is None:
                request.publish(app, reason)
            request.verify(app)
            if len(request.proofs) != 2:
                raise OSError("Incomplete prewrite approval audit")
        except (OSError, ValueError, RuntimeError):
            attempt.prewrite_audit_error = True
            reason = "audit-error"
        if requested:
            app._rpc_emit({"type": "tool_approval_closed", "id": ident, "outcome": request.outcome,
                           "reason": reason, "rewrite_prewrite": marker}, origin=origin)
        getattr(app, "_gui_approval_details", {}).pop(ident, None)
        if reason == "audit-error":
            raise approval_authority.ApprovalAuditError("Prewrite hook approval audit could not be verified")
