"""Urgent approval scheduling and notification-only escalation.

Only relay-written structured fields confer priority, never body text. Local
maildir writers are forgeable; scheduling confers no approval authority.
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


def request_id(msg: dict) -> str | None:
    if msg.get("type") != "QUESTION":
        return None
    thread = msg.get("thread_id")
    if not isinstance(thread, str) or len(thread) > 1024:
        return None
    try:
        metadata = json.loads(thread)
    except (TypeError, ValueError):
        return None
    if (not isinstance(metadata, dict) or metadata.get("kind") != REQUEST_TYPE
            or set(metadata) != {"kind", "id", "requester", "approver"}):
        return None
    ident, requester, approver = metadata.get("id"), metadata.get("requester"), metadata.get("approver")
    if not isinstance(ident, str) or not _IDENT.fullmatch(ident):
        return None
    for address in (requester, approver):
        if (not isinstance(address, str) or not address or len(address) > 128
                or address.startswith("-")
                or any(char.isspace() or char in "/\\\\:" for char in address)):
            return None
    recipient = msg.get("to")
    if msg.get("from") != requester or not recipient:
        return None
    if recipient != approver and harness.registered_spawner(approver) != recipient:
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
    for index in range(len(conversation) - 1, -1, -1):
        message = conversation[index]
        if message.get("role") == "user":
            message.setdefault("approval_delivery", {})[ident] = {"state": status, "notice": text}
            app._edit(index, "Approval delivery state updated")
            break
    stage(ident, status)
    app._system(text)


async def wait_for_answer(app, future, *, approver: str, ident: str,
                          message: str, timeout: float) -> bool:
    """Escalate once, keeping the original future and frozen reply authority."""
    deadline = time.monotonic() + timeout
    try:
        return await asyncio.wait_for(asyncio.shield(future), min(ESCALATE_AFTER_S, timeout))
    except TimeoutError:
        pass
    if future.done():
        return future.result()
    target = harness.registered_spawner(approver)
    if target in (approver, getattr(app.seat, "agent_id", None)):
        target = None
    if target is None:
        visible_state(app, ident, "escalation_absent",
                      f"Approval delivery failure ({ident}): approver has no registered spawner; "
                      "no escalation recipient or new reply authority. Request remains gated.")
    else:
        notification = (message + f"\n[ESCALATION: unanswered approval after {ESCALATE_AFTER_S:.0f}s] "
                        f"Notification only to approver {approver}'s registered spawner. "
                        "Reply authority remains with the original approver; "
                        "mailbox delivery does not prove model receipt.")
        sent = await asyncio.to_thread(app.seat.send, target, notification,
                                       approval_request=(ident, approver))
        visible_state(app, ident, "escalation_sent" if sent else "escalation_failed",
                      f"Approval {ident}: escalation notification "
                      + (f"sent to {target[:8]} (transport only; model receipt unconfirmed)."
                         if sent else "failed delivery; request remains gated."))
    remaining = max(0.0, deadline - time.monotonic())
    return await asyncio.wait_for(asyncio.shield(future), remaining)
