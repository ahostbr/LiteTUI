"""A host can retire an approval without guessing from ACKs or local timers."""
import asyncio
from types import SimpleNamespace

import pytest
from approval_store_fixture_t0340 import bind_origin

from litetui import approval_authority, approval_relay, seat_authority, tool_approval


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_relay, 'current_spawner', lambda app: None)
    monkeypatch.setattr(seat_authority, 'confirm_route', lambda app: 'host')
    app = SimpleNamespace(seat=SimpleNamespace(agent_id='worker'), emitted=[], _edit=lambda *args: None,
                          conversation=[{'role': 'user', 'content': 'origin'}])
    bind_origin(app, tmp_path / 'origin')
    app._rpc_emit = app.emitted.append
    return app


async def start(app, timeout=0):
    task = asyncio.create_task(tool_approval.approve_over_rpc(
        app, 'shell', {'command': 'not executed'}, SimpleNamespace(reason='writes'), timeout=timeout))
    await asyncio.sleep(0)
    request = app.emitted[-1]
    assert request['type'] == 'tool_approval_requested'
    return task, request['id']


def closed(app, ident, outcome, reason=None):
    reason = reason or ('answered' if outcome in {'approved', 'denied'} else outcome)
    assert [e for e in app.emitted if e['type'] == 'tool_approval_closed' and e['id'] == ident] == [
        {'type': 'tool_approval_closed', 'id': ident, 'outcome': outcome, 'reason': reason}]
    assert ident not in app._approval_waiters
    assert ident not in app._rpc_approval_spawners
    assert ident not in app._approval_authority_records
    assert not tool_approval.resolve_over_rpc(app, ident, True)


@pytest.mark.asyncio
@pytest.mark.parametrize('allow,remember', [(False, False), (True, False), (True, True)])
async def test_closure_reports_settled_authority_not_creator_cancelled_default(app, allow, remember):
    task, ident = await start(app)
    assert tool_approval.resolve_over_rpc(app, ident, allow, remember)
    assert not tool_approval.resolve_over_rpc(app, ident, not allow)
    answer = await task
    assert answer.approved is allow and answer.remember is remember
    closed(app, ident, 'approved' if allow else 'denied')


@pytest.mark.asyncio
async def test_timeout_is_not_human_denial(app):
    task, ident = await start(app, timeout=0.01)
    assert await task is None
    closed(app, ident, 'timeout')


@pytest.mark.asyncio
async def test_cancel_closes_unbounded_request_without_implicit_answer(app):
    task, ident = await start(app)
    assert app.emitted[0]['timeout_s'] is None
    task.cancel()
    assert await task is None
    closed(app, ident, 'cancelled')


@pytest.mark.asyncio
async def test_cancel_after_accepted_answer_does_not_rewrite_authority(app):
    task, ident = await start(app)
    assert tool_approval.resolve_over_rpc(app, ident, True)
    task.cancel()
    assert await task is None  # closure is not a tool-execution receipt
    closed(app, ident, 'approved', 'cancelled')


@pytest.mark.asyncio
async def test_origin_revocation_closes_without_persisting_to_foreign_origin(app):
    task, ident = await start(app)
    origin = app.conversation
    app.conversation = [{'role': 'user', 'content': 'replacement'}]
    assert await task is None
    closed(app, ident, 'revoked')
    assert origin[0]['approval_delivery'][ident]['outcome'] == 'pending'
    assert 'approval_delivery' not in app.conversation[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('at_settle', [False, True])
async def test_failed_audit_closes_as_audit_error_not_approval_or_timeout(app, at_settle):
    task, ident = await start(app, timeout=0.01)
    def fail(*args):
        raise OSError('injected audit failure')
    app._edit = fail
    if at_settle:
        assert not tool_approval.resolve_over_rpc(app, ident, True)
    assert await task is None
    closed(app, ident, 'audit-error')


@pytest.mark.asyncio
async def test_failed_creation_emits_neither_request_nor_orphan_closure(app):
    def fail(*args):
        raise OSError('injected creation failure')
    app._edit = fail
    assert await tool_approval.approve_over_rpc(app, 'shell', {}, SimpleNamespace(reason='x')) is None
    assert app.emitted == []
    assert not app._approval_waiters and not app._approval_authority_records


@pytest.mark.asyncio
async def test_concurrent_requests_close_independently_and_in_completion_order(app):
    first, a = await start(app)
    second, b = await start(app)
    assert a != b
    assert tool_approval.resolve_over_rpc(app, b, False)
    assert await second is tool_approval.DENIED
    closed(app, b, 'denied')
    assert not first.done() and a in app._approval_waiters
    assert tool_approval.resolve_over_rpc(app, a, True)
    assert await first is tool_approval.ONCE
    closed(app, a, 'approved')
    assert [e['id'] for e in app.emitted if e['type'] == 'tool_approval_closed'] == [b, a]


@pytest.mark.asyncio
async def test_closure_transport_failure_propagates_after_authority_cleanup(app):
    task, ident = await start(app)
    def fail(event):
        raise OSError('closure transport failed')
    app._rpc_emit = fail
    assert tool_approval.resolve_over_rpc(app, ident, True)
    with pytest.raises(OSError, match='closure transport failed'):
        await task
    assert not app._approval_waiters and not app._approval_authority_records
    assert not app._rpc_approval_spawners


def test_close_absent_authority_does_not_invent_outcome(app):
    assert approval_authority.close(app, 'absent', 'cancelled') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['origin', 'deadline'])
async def test_accepted_decision_can_close_unusable_without_becoming_execution_receipt(app, monkeypatch, change):
    # Keep asyncio's clock real; replace only the authority module's clock object.
    clock = SimpleNamespace(monotonic=lambda: 100.0, time=lambda: 100.0)
    monkeypatch.setattr(approval_authority, 'time', clock)
    task, ident = await start(app, timeout=1)
    assert tool_approval.resolve_over_rpc(app, ident, True)
    if change == 'origin':
        app.conversation = list(app.conversation)
    else:
        clock.monotonic = lambda: 101.0
    assert await task is None
    closed(app, ident, 'approved', 'revoked' if change == 'origin' else 'timeout')


@pytest.mark.asyncio
async def test_answer_at_authoritative_deadline_is_refused(app, monkeypatch):
    clock = SimpleNamespace(monotonic=lambda: 100.0, time=lambda: 100.0)
    monkeypatch.setattr(approval_authority, 'time', clock)
    task, ident = await start(app, timeout=1)
    clock.monotonic = lambda: 101.0
    assert not tool_approval.resolve_over_rpc(app, ident, True)
    assert await task is None
    closed(app, ident, 'timeout')


@pytest.mark.asyncio
async def test_rpc_command_ack_and_closure_keep_independent_ids(app, monkeypatch):
    from litetui import rpc
    replies = []
    monkeypatch.setattr(rpc, 'rpc_emit', replies.append)
    task, ident = await start(app)
    rpc._dispatch(app, {'type': 'approve', 'id': 'command-independent',
                        'approval_id': ident, 'allow': False, 'remember': False})
    assert await task is tool_approval.DENIED
    assert replies == [{'type': 'response', 'id': 'command-independent', 'ok': True,
                        'result': {'approved': ident, 'allow': False, 'id_key': 'approval_id'}}]
    closed(app, ident, 'denied')
