"""Urgent approval scheduling and notification-only escalation.

Only envelope-bound structured fields confer priority, never body text. Local
maildir writers are forgeable; scheduling confers no approval authority. Remote
frozen snapshots do not expand priority: grandparent and reparented-ancestor
notifications may be normal-priority mail. Answer eligibility remains tied to
the local creation clock regardless of delivery or model receipt.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from litetui import harness, runtime_log

ESCALATE_AFTER_S = 60.0
REQUEST_TYPE = "approval-request"
_IDENT = re.compile(r"appr-[0-9a-f]{12}")


def request_id(msg: dict, *, outbound_ancestors: tuple[str, ...] | None = None) -> str | None:
    """Receive priority uses legacy registry binding, never a supplied snapshot.

    Only Seat.send supplies trusted locally frozen outbound targets; remote
    snapshot metadata cannot prioritize an arbitrary ancestor recipient.
    """
    if msg.get("type") != "QUESTION" or harness._expired(msg):
        return None
    thread = msg.get("thread_id")
    if not isinstance(thread, str) or len(thread) > 1024:
        return None
    try:
        metadata = json.loads(thread)
    except (TypeError, ValueError):
        return None
    if (not isinstance(metadata, dict) or metadata.get("kind") != REQUEST_TYPE
            or set(metadata) not in ({"kind", "id", "requester", "approver"},
                                     {"kind", "id", "requester", "approver", "frozen_ancestors"})):
        return None
    ident, requester, approver = metadata.get("id"), metadata.get("requester"), metadata.get("approver")
    if not isinstance(ident, str) or not _IDENT.fullmatch(ident):
        return None
    from litetui.approval_authority import _id
    if not _id(requester) or not _id(approver):
        return None
    recipient = msg.get("to")
    if msg.get("from") != requester or not recipient:
        return None
    if "frozen_ancestors" in metadata:
        ancestors = metadata["frozen_ancestors"]
        if (not isinstance(ancestors, list) or len(ancestors) > 2
                or any(not _id(ancestor) for ancestor in ancestors)
                or len({requester, approver, *ancestors}) != len(ancestors) + 2
                or recipient not in [approver, *ancestors]):
            return None
        if outbound_ancestors is not None and tuple(ancestors) != outbound_ancestors:
            return None
    if outbound_ancestors is not None:
        if recipient not in (approver, *outbound_ancestors):
            return None
    elif recipient != approver and harness.registered_spawner(approver) != recipient:
        return None
    return ident


def stage(ident: str, status: str) -> None:
    runtime_log.record("approval_delivery", site="approval.delivery", component="relay",
                       id=ident, status=status)


def enqueue(queue: list, item: dict, ident: str | None) -> None:
    if ident is None:
        queue.append(item)
        return
    item["approval_request_id"] = ident
    # Do not overtake native ownership/admission journals or an interrupt.
    index = 0
    for position, pending in enumerate(queue):
        if (pending.get("approval_request_id") or pending.get("_claude_entry")
                or pending.get("_claude_segment") or pending.get("_codex_entry")
                or pending.get("source") == "interrupted"):
            index = position + 1
    queue.insert(index, item)
    stage(ident, "queued")


def visible_state(app, ident: str, status: str, text: str) -> None:
    """Persist state on the request's conversation, and show it without a turn."""
    conversation = getattr(app, "conversation", [])
    record = getattr(app, "_approval_authority_records", {}).get(ident)
    if record:
        from litetui.approval_authority import persistence_ready
        if not persistence_ready(app, ident):
            return
    indices = ([record["conversation_index"]] if record and record.get("conversation_index") is not None
               else range(len(conversation) - 1, -1, -1))
    for index in indices:
        message = conversation[index]
        if message.get("role") == "user":
            message.setdefault("approval_delivery", {}).setdefault(ident, {}).update(state=status, notice=text)
            app._edit(index, "Approval delivery state updated")
            break
    stage(ident, status)
    app._system(text)


def answer_open(app, ident: str) -> bool:
    deadline = getattr(app, "_relay_answer_deadlines", {}).get(ident)
    return deadline is None or time.monotonic() < deadline


async def _answer_before(future, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    result = await asyncio.wait_for(asyncio.shield(future), remaining)
    if time.monotonic() >= deadline:
        raise TimeoutError
    return result


async def wait_for_answer(app, future, *, approver: str, ident: str,
                          message: str, timeout: float, created_at: float | None = None) -> bool:
    """Reserve transport/state budget INSIDE 60s; never extend answer expiry.

    Production: begin at49s, bounded CLI up to10s, leave1s to save/show state.
    Shorter configured answer waits expire normally, without a post-expiry send.
    A blocked filesystem/thread may finish late; no receipt is inferred or retried.
    """
    started = time.monotonic() if created_at is None else created_at
    answer_deadline = started + timeout
    deadlines = getattr(app, "_relay_answer_deadlines", None)
    if deadlines is None:
        deadlines = app._relay_answer_deadlines = {}
    deadlines[ident] = answer_deadline
    async def before(deadline):
        if ident in getattr(app, "_approval_authority_records", {}):
            from litetui import approval_authority
            return await approval_authority.wait_for_answer(app, ident, future, limit=deadline)
        return await _answer_before(future, deadline)
    try:
        if timeout < ESCALATE_AFTER_S:
            return await before(answer_deadline)
        authority_record = getattr(app, "_approval_authority_records", {}).get(ident)
        frozen = authority_record["authority"] if authority_record else None
        # Standalone legacy delivery remains notification-only. Production
        # requests always supply a frozen authority record at creation.
        targets = frozen.ancestors if frozen else (harness.registered_spawner(approver),)
        if not targets:
            targets = (None,)
        transport_budget = min(10.0, ESCALATE_AFTER_S / 6)
        state_budget = min(1.0, ESCALATE_AFTER_S / 60)
        for index, target in enumerate(targets):
            escalation_at = started + ESCALATE_AFTER_S * (index + 1)
            if escalation_at > answer_deadline:
                break
            transport_deadline = min(answer_deadline, escalation_at - state_budget)
            dispatch_at = transport_deadline - transport_budget
            try:
                return await before(dispatch_at)
            except TimeoutError:
                pass
            if time.monotonic() >= answer_deadline:
                raise TimeoutError
            if frozen:
                from litetui.approval_authority import persistence_ready
                if not persistence_ready(app, ident):
                    return False
            if future.done():
                return future.result()
            if target in (approver, getattr(app.seat, "agent_id", None)):
                target = None
            if target is None:
                visible_state(app, ident, "escalation_absent",
                              f"Approval delivery failure ({ident}): no frozen escalation recipient. "
                              "Request remains gated.")
                continue
            notice = (f"Frozen ancestor may answer at {ESCALATE_AFTER_S * (index + 1):.0f}s from creation; "
                      if frozen else "Notification only; reply authority remains with original approver; ")
            notification = (message + "\n[ESCALATION: unanswered approval] " + notice
                            + "mailbox delivery does not prove model receipt.")
            sent = False
            remaining = transport_deadline - time.monotonic()
            send_options = {"approval_request": (ident, approver), "deadline": transport_deadline}
            if frozen:
                send_options["approval_ancestors"] = frozen.ancestors
            if remaining > 0:
                try:
                    sent = await asyncio.wait_for(asyncio.to_thread(
                        app.seat.send, target, notification, **send_options), remaining)
                except (TimeoutError, OSError):
                    pass
            sent = sent and time.monotonic() < transport_deadline
            visible_state(app, ident, "escalation_sent" if sent else "escalation_failed",
                          f"Approval {ident}: escalation notification "
                          + (f"sent to {target[:8]} (transport only; model receipt unconfirmed)."
                             if sent else "delivery failed or unconfirmed at deadline; request remains gated."))
        return await before(answer_deadline)
    finally:
        deadlines.pop(ident, None)
