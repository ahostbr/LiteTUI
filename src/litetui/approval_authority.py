"""T0340 active approval authority: frozen registered IDs, never message prose.

Audit records persist in the conversation; pending authority does not survive a
process restart. Local maildir identity is the existing exact-envelope boundary,
not cryptographic authentication. Scheduling metadata grants no answer rights.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TypeGuard

from litetui import harness

ESCALATION_STEP_S = 60.0


def _id(value: object) -> TypeGuard[str]:
    return (isinstance(value, str) and 0 < len(value) <= 128 and not value.startswith('-')
            and not any(ch.isspace() or ch in '/\\:' for ch in value))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate registry key')
        result[key] = value
    return result


def _registered(ident: str) -> bool:
    try:
        row = json.loads((harness.AGENTS_DIR / f'{ident}.json').read_text(encoding='utf-8'),
                         object_pairs_hook=_unique_object)
    except (OSError, ValueError):
        return False
    return isinstance(row, dict) and row.get('agent_id') == ident


def frozen_ancestors(approver: str, requester: str) -> tuple[str, ...]:
    """At most two validated registry edges. Any corrupt/cyclic chain grants none."""
    found: list[str] = []
    seen = {approver, requester}
    current = approver
    for _ in range(2):
        try:
            row = json.loads((harness.AGENTS_DIR / f'{current}.json').read_text(encoding='utf-8'),
                             object_pairs_hook=_unique_object)
        except (OSError, ValueError):
            return ()
        if not isinstance(row, dict) or row.get('agent_id') != current:
            return ()
        parent = row.get('spawned_by')
        if parent is None:
            return tuple(found)
        if not _id(parent) or parent in seen:
            return ()
        try:
            destination = json.loads((harness.AGENTS_DIR / f'{parent}.json').read_text(encoding='utf-8'),
                                     object_pairs_hook=_unique_object)
        except (OSError, ValueError):
            return ()
        if not isinstance(destination, dict) or destination.get('agent_id') != parent:
            return ()
        found.append(parent)
        seen.add(parent)
        current = parent
    # Validate the terminal edge too: a visible cycle must not grant its prefix.
    tail = destination.get('spawned_by')
    if tail is not None and (not _id(tail) or tail in seen):
        return ()
    return tuple(found)


@dataclass(frozen=True)
class ApprovalAuthority:
    requester: str
    approver: str | None
    ancestors: tuple[str, ...]
    created_at: float
    deadline: float | None
    human_only: bool


def _records(app) -> dict:
    records = getattr(app, '_approval_authority_records', None)
    if records is None:
        records = app._approval_authority_records = {}
    return records


class ApprovalAuditError(RuntimeError):
    """Persistence failed: the pending action must not run."""


def context_valid(app, ident: str) -> bool:
    """Strict origin binding. Append is safe; replacement/reordering is not."""
    record = _records(app).get(ident)
    if record is None or record['outcome'] == 'audit-error':
        return False
    store = getattr(app, '_store', None)
    if (getattr(app, 'conversation', None) is not record['conversation']
            or store is not record['store']
            or (getattr(store, 'convo_id', None), getattr(store, 'convo_path', None)) != record['store_identity']):
        return False
    index = record['conversation_index']
    conversation = record['conversation']
    return (index is not None and 0 <= index < len(conversation)
            and conversation[index] is record['message'])


def persistence_ready(app, ident: str) -> bool:
    """Existing repository's error latch and no-write modes are load-bearing."""
    if not context_valid(app, ident) or not callable(getattr(app, '_edit', None)):
        return False
    store = _records(app)[ident]['store']
    return (store is not None and getattr(store, 'persist_error', 'missing') is None
            and getattr(store, 'convo_path', None) is not None
            and getattr(store, 'pending', None) is False
            and getattr(store, 'loading', None) is False)


def raise_if_cancelled() -> None:
    """Python3.11 wait_for can consume cancellation when its future is done."""
    task = asyncio.current_task()
    if task is not None and task.cancelling() > 0:
        raise asyncio.CancelledError


async def wait_for_answer(app, ident: str, future, *, limit: float | None = None):
    """Observe revocation even without a deadline; never cancel a cadence wait.

    One local creator loop, no background watcher. The original monotonic
    deadline is absolute; a valid human timeout=0 wait remains indefinite.
    """
    record = _records(app)[ident]
    deadline = record['authority'].deadline
    if limit is not None:
        deadline = limit if deadline is None else min(deadline, limit)
    while True:
        raise_if_cancelled()
        if not persistence_ready(app, ident):
            return None
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError
        if future.done():
            result = future.result()
            if not persistence_ready(app, ident):
                return None
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError
            raise_if_cancelled()
            return result
        interval = 0.05 if deadline is None else min(0.05, deadline - time.monotonic())
        if interval <= 0:
            raise TimeoutError
        try:
            await asyncio.wait_for(asyncio.shield(future), interval)
        except TimeoutError:
            continue


def _audit(app, ident: str, record: dict) -> None:
    # Never persist an old action through a new conversation's store.
    if not persistence_ready(app, ident):
        raise ApprovalAuditError('Approval audit origin or persistence signal is unavailable')
    index = record['conversation_index']
    message = record['message']
    states = message.setdefault('approval_delivery', {})
    previous = states.get(ident)
    authority = record['authority']
    states[ident] = {**(previous or {}), 'approver_id': authority.approver,
                    'frozen_ancestor_ids': list(authority.ancestors),
                    'human_only': authority.human_only,
                    'created_wall_time': record['created_wall_time'],
                    'answerer_id': record.get('answerer_id'), 'outcome': record['outcome']}
    try:
        app._edit(index, 'Approval authority state updated')
        if not persistence_ready(app, ident):
            raise ApprovalAuditError('Approval audit was not durably accepted by its bound repository')
    except (OSError, ValueError, ApprovalAuditError):
        if previous is None:
            states.pop(ident, None)
        else:
            states[ident] = previous
        raise


def create(app, ident: str, *, approver: str | None, route: str | None,
           timeout: float | None, created_at: float | None = None) -> ApprovalAuthority:
    """Only explicit existing relay/host routes grant agent answer authority."""
    created = time.monotonic() if created_at is None else created_at
    requester = getattr(getattr(app, 'seat', None), 'agent_id', None)
    human_only = (route not in {'spawner', 'host'} or not _id(requester) or not _id(approver)
                  or not _registered(approver) or requester == approver)
    ancestors = frozen_ancestors(approver, requester) if not human_only and _id(approver) and _id(requester) else ()
    authority = ApprovalAuthority(requester or '', None if human_only else approver, ancestors,
                                  created, None if timeout is None else created + timeout, human_only)
    conversation = getattr(app, 'conversation', None)
    if conversation is None:
        conversation = app.conversation = []
    # Materialize the existing lazy repository before binding; _edit otherwise
    # creates it during the first audit and would invalidate a None binding.
    store = getattr(app, 'store', getattr(app, '_store', None))
    index = next((i for i in range(len(conversation) - 1, -1, -1)
                  if conversation[i].get('role') == 'user'), None)
    record = {'authority': authority, 'outcome': 'pending', 'created_wall_time': time.time(),
              'conversation_index': index, 'conversation': conversation,
              'message': conversation[index] if index is not None else None,
              'store': store, 'store_identity': (getattr(store, 'convo_id', None), getattr(store, 'convo_path', None))}
    _records(app)[ident] = record
    try:
        _audit(app, ident, record)
    except (OSError, ValueError, ApprovalAuditError) as exc:
        _records(app).pop(ident, None)
        raise ApprovalAuditError('Approval creation audit failed; action remains gated') from exc
    return authority


def can_answer(app, ident: str, msg: dict) -> bool:
    record = _records(app).get(ident)
    if record is None or record['outcome'] != 'pending' or not persistence_ready(app, ident) or harness._expired(msg):
        return False
    authority = record['authority']
    now = time.monotonic()
    if (authority.human_only or msg.get('to') != authority.requester
            or (authority.deadline is not None and now >= authority.deadline)):
        return False
    sender = msg.get('from')
    if sender == authority.approver:
        return True
    return any(sender == ancestor and now >= authority.created_at + ESCALATION_STEP_S * (index + 1)
               for index, ancestor in enumerate(authority.ancestors))


def settle(app, ident: str, *, outcome: str, answerer_id: str | None = None) -> bool:
    """Called on the event loop together with future resolution; one answer wins."""
    record = _records(app).get(ident)
    if record is None or record['outcome'] != 'pending' or not persistence_ready(app, ident):
        return False
    record.update(outcome=outcome, answerer_id=answerer_id)
    try:
        _audit(app, ident, record)
    except (OSError, ValueError, ApprovalAuditError):
        # Do not approve the future after a failed durable edit. The creator's
        # normal timeout/cancellation finally removes this revoked record.
        record.update(outcome='audit-error', answerer_id=None)
        return False
    return True


def close(app, ident: str, outcome: str) -> None:
    try:
        settle(app, ident, outcome=outcome)
    finally:
        _records(app).pop(ident, None)
