"""Frozen lineage authority, exact elapsed boundaries, audit and channel parity."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from litetui import approval_authority as authority
from litetui import approval_relay, harness, tool_approval

IDENT = 'appr-a9f2e196419c'
REQUESTER, APPROVER, PARENT, GRANDPARENT = 'worker', 'leader', 'parent', 'grandparent'


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'AGENTS_DIR', tmp_path)
    clock = [100.0]
    monkeypatch.setattr(authority.time, 'monotonic', lambda: clock[0])
    for ident, parent in [(APPROVER, PARENT), (PARENT, GRANDPARENT), (GRANDPARENT, None)]:
        (tmp_path / f'{ident}.json').write_text(json.dumps({'agent_id': ident, 'spawned_by': parent}))
    app = SimpleNamespace(seat=SimpleNamespace(agent_id=REQUESTER), conversation=[{'role': 'user', 'content': 'x'}],
                          _edit=lambda *args: None)
    return app, clock, tmp_path


def msg(sender, verb='APPROVE', **extra):
    return {'from': sender, 'to': REQUESTER, 'body': f'{verb} {IDENT}', **extra}


def create(app, route='spawner', timeout=600):
    return authority.create(app, IDENT, approver=APPROVER, route=route, timeout=timeout)


@pytest.mark.parametrize('elapsed,sender,expected', [
    (0, APPROVER, True), (59.999, PARENT, False), (60, PARENT, True),
    (119.999, GRANDPARENT, False), (120, GRANDPARENT, True),
    (120, 'unrelated', False), (600, APPROVER, False), (600, PARENT, False),
])
def test_exact_boundaries(context, elapsed, sender, expected):
    app, clock, _ = context
    record = create(app)
    assert record.ancestors == (PARENT, GRANDPARENT)
    clock[0] += elapsed
    assert authority.can_answer(app, IDENT, msg(sender)) is expected


def test_lineage_changes_cannot_expand_authority(context):
    app, clock, root = context
    create(app)
    (root / f'{APPROVER}.json').write_text(json.dumps({'agent_id': APPROVER, 'spawned_by': 'new-leader'}))
    clock[0] += 60
    assert authority.can_answer(app, IDENT, msg(PARENT))
    assert not authority.can_answer(app, IDENT, msg('new-leader'))


@pytest.mark.parametrize('route', ['own', 'hand', 'refuse', None, 'unknown'])
def test_human_only_and_unknown_routes_never_delegate(context, route):
    app, clock, _ = context
    create(app, route)
    clock[0] += 120
    assert not any(authority.can_answer(app, IDENT, msg(s)) for s in (APPROVER, PARENT, GRANDPARENT))


@pytest.mark.parametrize('fault', ['missing', 'cycle', 'duplicate-key', 'invalid', 'wrong-id', 'grandcycle'])
def test_corrupt_lineage_grants_no_ancestor_authority(context, fault):
    app, clock, root = context
    path = root / f'{PARENT}.json'
    if fault == 'missing':
        path.unlink()
    elif fault == 'duplicate-key':
        path.write_text('{"agent_id":"parent","agent_id":"parent","spawned_by":"grandparent"}')
    elif fault == 'grandcycle':
        (root / f'{GRANDPARENT}.json').write_text(json.dumps({'agent_id': GRANDPARENT, 'spawned_by': APPROVER}))
    else:
        path.write_text(json.dumps({'agent_id': 'wrong' if fault == 'wrong-id' else PARENT,
                                   'spawned_by': APPROVER if fault == 'cycle' else '../bad'}))
    record = create(app)
    assert record.ancestors == ()
    clock[0] += 120
    assert not authority.can_answer(app, IDENT, msg(PARENT))


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['relay', 'rpc'])
async def test_one_atomic_answer_and_exact_sender_record(context, channel):
    app, clock, _ = context
    create(app)
    future = asyncio.get_running_loop().create_future()
    if channel == 'relay':
        app._relay_pending = {IDENT: (future, APPROVER)}
        take = approval_relay.take_answer
    else:
        app._approval_waiters = {IDENT: future}
        take = tool_approval.take_spawner_answer
    clock[0] += 60
    assert take(app, msg(PARENT, 'DENY'))
    assert not take(app, msg(APPROVER))
    audit = app.conversation[0]['approval_delivery'][IDENT]
    assert audit['answerer_id'] == PARENT and audit['outcome'] == 'denied'
    assert audit['frozen_ancestor_ids'] == [PARENT, GRANDPARENT]
    assert future.result() in (False, tool_approval.DENIED)


def test_address_ttl_name_and_restart_fail_closed(context):
    app, clock, _ = context
    create(app)
    clock[0] += 120
    assert not authority.can_answer(app, IDENT, msg(PARENT, to='foreign'))
    assert not authority.can_answer(app, IDENT, msg('named-parent', sender_name=PARENT))
    assert not authority.can_answer(app, IDENT, msg(PARENT, ttl_minutes=1, timestamp='2000-01-01T00:00:00+00:00'))
    authority.close(app, IDENT, 'cancelled')
    assert not authority.can_answer(app, IDENT, msg(PARENT))
    # The audit survives but a restarted pending-authority map is absent.
    app._approval_authority_records = {}
    assert not authority.can_answer(app, IDENT, msg(APPROVER))


@pytest.mark.parametrize('channel', ['relay', 'rpc'])
@pytest.mark.parametrize('elapsed', [59.999, 119.999, 600])
@pytest.mark.asyncio
async def test_channel_rejects_early_and_expired_answers(context, channel, elapsed):
    app, clock, _ = context
    create(app)
    future = asyncio.get_running_loop().create_future()
    if channel == 'relay':
        app._relay_pending = {IDENT: (future, APPROVER)}
        take = approval_relay.take_answer
    else:
        app._approval_waiters = {IDENT: future}
        take = tool_approval.take_spawner_answer
    clock[0] += elapsed
    sender = PARENT if elapsed == 59.999 else GRANDPARENT
    assert not take(app, msg(sender))
    assert not future.done()
    assert app.conversation[0]['approval_delivery'][IDENT]['outcome'] == 'pending'


def test_max_two_edges_and_immutable_snapshot(context):
    from dataclasses import FrozenInstanceError
    app, clock, root = context
    (root / f'{GRANDPARENT}.json').write_text(json.dumps({'agent_id': GRANDPARENT, 'spawned_by': 'greatgrand'}))
    (root / 'greatgrand.json').write_text(json.dumps({'agent_id': 'greatgrand'}))
    record = create(app)
    assert record.ancestors == (PARENT, GRANDPARENT)
    with pytest.raises(FrozenInstanceError):
        record.ancestors = ('greatgrand',)
    clock[0] += 180
    assert not authority.can_answer(app, IDENT, msg('greatgrand'))


@pytest.mark.parametrize('fault', ['missing', 'corrupt', 'duplicate-key', 'self'])
def test_invalid_original_approver_is_nondelegable(context, fault):
    app, _, root = context
    path = root / f'{APPROVER}.json'
    if fault == 'missing':
        path.unlink()
    elif fault == 'corrupt':
        path.write_text('{')
    elif fault == 'duplicate-key':
        path.write_text('{"agent_id":"leader","agent_id":"leader"}')
    else:
        app.seat.agent_id = APPROVER
    assert create(app).human_only
    assert not authority.can_answer(app, IDENT, msg(APPROVER))


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['own', 'hand', 'unknown', 'spawner', 'host'])
async def test_rpc_creation_uses_actual_route_and_frozen_ids(context, monkeypatch, route):
    from litetui import seat_authority
    app, clock, root = context
    monkeypatch.setattr(seat_authority, 'confirm_route', lambda app: route)
    monkeypatch.setattr(approval_relay, 'current_spawner', lambda app: APPROVER)
    events = []
    app._rpc_emit = events.append
    task = asyncio.create_task(tool_approval.approve_over_rpc(app, 'shell', {}, SimpleNamespace(reason='test'), timeout=300))
    await asyncio.sleep(0)
    ident = events[0]['id']
    snapshot = app._approval_authority_records[ident]['authority']
    assert snapshot.human_only is (route not in ('spawner', 'host'))
    (root / f'{APPROVER}.json').write_text(json.dumps({'agent_id': APPROVER, 'spawned_by': 'replacement'}))
    clock[0] += 120
    answer = {'to': REQUESTER, 'from': GRANDPARENT, 'body': f'DENY {ident}'}
    if snapshot.human_only:
        assert not tool_approval.take_spawner_answer(app, answer)
        assert tool_approval.resolve_over_rpc(app, ident, False)
    else:
        assert tool_approval.take_spawner_answer(app, answer)
    assert await task is tool_approval.DENIED
    assert not app._approval_authority_records
    audit = app.conversation[0]['approval_delivery'][ident]
    assert audit['answerer_id'] == (None if snapshot.human_only else GRANDPARENT)
    assert audit['outcome'] == 'denied'


@pytest.mark.asyncio
@pytest.mark.parametrize('finish', ['timeout', 'cancelled'])
async def test_rpc_completion_records_reason_and_cannot_reopen(tmp_path, monkeypatch, finish):
    from litetui import seat_authority
    monkeypatch.setattr(harness, 'AGENTS_DIR', tmp_path)
    (tmp_path / f'{APPROVER}.json').write_text(json.dumps({'agent_id': APPROVER}))
    monkeypatch.setattr(seat_authority, 'confirm_route', lambda app: 'spawner')
    monkeypatch.setattr(approval_relay, 'current_spawner', lambda app: APPROVER)
    events = []
    app = SimpleNamespace(seat=SimpleNamespace(agent_id=REQUESTER), _rpc_emit=events.append,
                          conversation=[{'role': 'user'}], _edit=lambda *args: None)
    task = asyncio.create_task(tool_approval.approve_over_rpc(app, 'shell', {}, SimpleNamespace(reason='x'), timeout=0.02))
    await asyncio.sleep(0)
    ident = events[0]['id']
    if finish == 'cancelled':
        task.cancel()
    assert await task is None
    assert app.conversation[0]['approval_delivery'][ident]['outcome'] == finish
    assert not tool_approval.take_spawner_answer(app, {'to': REQUESTER, 'from': APPROVER, 'body': f'APPROVE {ident}'})
    assert not tool_approval.resolve_over_rpc(app, ident, True)


def test_rpc_human_answer_after_deadline_is_refused(context):
    app, clock, _ = context
    create(app)
    future = SimpleNamespace(done=lambda: False, set_result=lambda value: pytest.fail('late answer'))
    app._approval_waiters = {IDENT: future}
    clock[0] += 600
    assert not tool_approval.resolve_over_rpc(app, IDENT, True)


def test_audit_stays_on_creation_conversation(context):
    from litetui import approval_delivery
    app, _, _ = context
    app._system = lambda *args: None
    create(app)
    app.conversation.append({'role': 'user', 'content': 'new input'})
    authority.settle(app, IDENT, outcome='approved', answerer_id=APPROVER)
    approval_delivery.visible_state(app, IDENT, 'responded', 'test')
    assert app.conversation[0]['approval_delivery'][IDENT]['answerer_id'] == APPROVER
    assert app.conversation[0]['approval_delivery'][IDENT]['state'] == 'responded'
    assert 'approval_delivery' not in app.conversation[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['own', 'hand', 'unknown'])
async def test_relay_creation_cannot_override_human_route(context, monkeypatch, route):
    from litetui import seat_authority
    app, clock, _ = context
    app.seat.name = 'worker'
    app.seat.registered = True
    monkeypatch.setattr(approval_relay, 'current_spawner', lambda app: APPROVER)
    monkeypatch.setattr(seat_authority, 'confirm_route', lambda app: route)
    app.settings = SimpleNamespace(relay_approval_timeout_s=300)
    app._system = lambda *args: None
    app._begin_wait = lambda *args: None
    app._end_wait = lambda *args: None
    answer_attempts = []
    def send(to, body, **metadata):
        ident = metadata['approval_request'][0]
        assert app._approval_authority_records[ident]['authority'].human_only
        answer_attempts.append(approval_relay.take_answer(app,
            {'to': REQUESTER, 'from': GRANDPARENT, 'body': f'APPROVE {ident}'}))
        return False
    app.seat.send = send
    decision = SimpleNamespace(danger='test', capabilities=set(), reason='x')
    assert await approval_relay.ask_spawner(app, 'shell', {}, decision, 'harness') == 'absent'
    assert answer_attempts == [False]
    assert not app._approval_authority_records
