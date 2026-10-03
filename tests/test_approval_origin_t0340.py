"""R1/R2 real creation: switched origins and failed audits cannot approve."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from litetui import (
    approval_delivery,
    approval_relay,
    harness,
    seat_authority,
    tool_approval,
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'AGENTS_DIR', tmp_path)
    (tmp_path / 'leader.json').write_text(json.dumps({'agent_id': 'leader'}))
    monkeypatch.setattr(seat_authority, 'confirm_route', lambda app: 'spawner')
    monkeypatch.setattr(approval_relay, 'current_spawner', lambda app: 'leader')
    writes = []
    app = SimpleNamespace(seat=SimpleNamespace(agent_id='worker', name='Worker', registered=True),
        conversation=[{'role': 'user', 'content': 'origin'}],
        _store=SimpleNamespace(convo_id='origin', convo_path='origin/convo.jsonl'),
        _edit=lambda *args: writes.append(args), writes=writes,
        settings=SimpleNamespace(relay_approval_timeout_s=0.15),
        _begin_wait=lambda *args: 1, _end_wait=lambda *args: None, _system=lambda *args: None)
    from approval_store_fixture_t0340 import bind_origin
    bind_origin(app, tmp_path / 'origin')
    return app


async def launch(app, channel):
    events = []
    if channel == 'rpc':
        app._rpc_emit = events.append
        task = asyncio.create_task(tool_approval.approve_over_rpc(
            app, 'shell', {}, SimpleNamespace(reason='test'), timeout=0.15))
    else:
        def send(to, body, **metadata):
            events.append({'id': metadata['approval_request'][0]})
            return True
        app.seat.send = send
        task = asyncio.create_task(approval_relay.ask_spawner(
            app, 'shell', {}, SimpleNamespace(reason='test', danger='test'), 'harness'))
    for _ in range(100):
        if events or task.done():
            break
        await asyncio.sleep(0.002)
    return task, events[0]['id'] if events else None


def replace_origin(app, change):
    if change == 'append':
        app.conversation.append({'role': 'user', 'content': 'next'})
    elif change == 'clear':
        app.conversation.clear()
    elif change == 'resume':
        app.conversation = [{'role': 'user', 'content': 'foreign'}]
        app._store.convo_id = 'foreign'
        app._store.convo_path = 'foreign/convo.jsonl'
    elif change == 'replace-list':
        app.conversation = list(app.conversation)
    elif change == 'replace-message':
        app.conversation[0] = dict(app.conversation[0])
    elif change == 'truncate':
        app.conversation[:] = []
    elif change == 'reused-index':
        app.conversation[:] = [{'role': 'user', 'content': 'replacement'}]
    elif change == 'store':
        app._store = SimpleNamespace(convo_id='origin', convo_path='origin/convo.jsonl')
    elif change == 'store-id':
        app._store.convo_id = 'new'


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['relay', 'rpc'])
@pytest.mark.parametrize('change', ['append', 'clear', 'resume', 'replace-list', 'replace-message',
                                    'truncate', 'reused-index', 'store', 'store-id'])
async def test_real_creation_context_binding(app, channel, change):
    task, ident = await launch(app, channel)
    origin = app.conversation[0]
    replace_origin(app, change)
    writes = len(app.writes)
    take = approval_relay.take_answer if channel == 'relay' else tool_approval.take_spawner_answer
    accepted = take(app, {'to': 'worker', 'from': 'leader', 'body': f'APPROVE {ident}'})
    assert accepted is (change == 'append')
    if change != 'append':
        human = approval_relay.take_human_answer if channel == 'relay' else tool_approval.resolve_over_rpc
        assert not human(app, ident, True)
        assert approval_delivery.visible_state(app, ident, 'test', 'test') is None
        assert len(app.writes) == writes
        assert origin['approval_delivery'][ident]['outcome'] == 'pending'
        # Exercise awaiting-return gate even if a separate lifecycle directly
        # completed the old future before it observed the switched context.
        future = app._relay_pending[ident][0] if channel == 'relay' else app._approval_waiters[ident]
        future.set_result(True if channel == 'relay' else tool_approval.ONCE)
    result = await task
    assert (result == 'approved' if channel == 'relay' else result is tool_approval.ONCE) is (change == 'append')
    assert not app._approval_authority_records
    assert not getattr(app, '_relay_pending', {})
    assert not getattr(app, '_approval_waiters', {})
    assert not getattr(app, '_rpc_approval_spawners', {})


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['relay', 'rpc'])
@pytest.mark.parametrize('error', [OSError, ValueError])
async def test_creation_edit_failure_cleans_every_map(app, channel, error):
    def fail(*args):
        raise error('injected persistence failure')
    app._edit = fail
    task, ident = await launch(app, channel)
    assert ident is None
    result = await task
    assert result == 'audit-error' if channel == 'relay' else result is None
    assert not app._approval_authority_records
    assert not getattr(app, '_relay_pending', {})
    assert not getattr(app, '_approval_waiters', {})
    assert not getattr(app, '_rpc_approval_spawners', {})


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['relay', 'rpc'])
@pytest.mark.parametrize('error', [OSError, ValueError])
async def test_settle_edit_failure_revokes_then_creator_cleans(app, channel, error):
    task, ident = await launch(app, channel)
    def fail(*args):
        raise error('injected settlement failure')
    app._edit = fail
    take = approval_relay.take_answer if channel == 'relay' else tool_approval.take_spawner_answer
    answer = {'to': 'worker', 'from': 'leader', 'body': f'APPROVE {ident}'}
    assert not take(app, answer)
    assert not take(app, {**answer, 'body': f'DENY {ident}'})
    future = app._relay_pending[ident][0] if channel == 'relay' else app._approval_waiters[ident]
    assert not future.done()
    assert app._approval_authority_records[ident]['outcome'] == 'audit-error'
    assert app.conversation[0]['approval_delivery'][ident]['outcome'] == 'pending'
    assert app.conversation[0]['approval_delivery'][ident]['answerer_id'] is None
    result = await task
    assert result != 'approved' if channel == 'relay' else result is None
    assert not app._approval_authority_records
    assert not getattr(app, '_relay_pending', {})
    assert not getattr(app, '_approval_waiters', {})
    assert not take(app, answer)


@pytest.mark.asyncio
async def test_begin_wait_failure_still_cleans_created_authority(app):
    def fail(*args):
        raise ValueError('wait setup failure')
    app._begin_wait = fail
    task, _ = await launch(app, 'relay')
    with pytest.raises(ValueError, match='wait setup'):
        await task
    assert not app._relay_pending
    assert not app._approval_authority_records


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['relay', 'rpc'])
async def test_close_audit_failure_does_not_leak_pending(app, channel):
    task, ident = await launch(app, channel)
    def fail(*args):
        raise ValueError('close audit failure')
    app._edit = fail
    await task
    assert not app._approval_authority_records
    assert not getattr(app, '_relay_pending', {})
    assert not getattr(app, '_approval_waiters', {})
    assert not getattr(app, '_rpc_approval_spawners', {})
    assert app.conversation[0]['approval_delivery'][ident]['outcome'] == 'pending'
