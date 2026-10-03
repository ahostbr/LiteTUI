"""Frozen outbound targets do not confer remote scheduling priority."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from litetui import approval_authority as authority
from litetui import approval_delivery as delivery
from litetui import harness

IDENT = 'appr-a9f2e196419c'


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'AGENTS_DIR', tmp_path)
    for ident, parent in [('leader', 'parent'), ('parent', 'grandparent'), ('grandparent', None)]:
        (tmp_path / f'{ident}.json').write_text(json.dumps({'agent_id': ident, 'spawned_by': parent}))
    return tmp_path


def envelope(target, ancestors):
    return {'type': 'QUESTION', 'from': 'worker', 'to': target,
            'thread_id': json.dumps({'kind': delivery.REQUEST_TYPE, 'id': IDENT,
                'requester': 'worker', 'approver': 'leader', 'frozen_ancestors': ancestors})}


def test_remote_snapshot_cannot_expand_priority(registry):
    assert delivery.request_id(envelope('leader', ['parent', 'grandparent'])) == IDENT
    assert delivery.request_id(envelope('parent', ['parent', 'grandparent'])) == IDENT
    assert delivery.request_id(envelope('grandparent', ['parent', 'grandparent'])) is None
    assert delivery.request_id(envelope('arbitrary', ['arbitrary'])) is None
    assert delivery.request_id(envelope('grandparent', ['parent', 'grandparent']),
                               outbound_ancestors=('parent', 'grandparent')) == IDENT
    assert delivery.request_id(envelope('grandparent', ['grandparent']),
                               outbound_ancestors=('parent', 'grandparent')) is None


@pytest.mark.parametrize('ancestors', [['parent', 'parent'], ['worker'], ['leader'], ['../bad'],
                                       ['parent', 'grandparent', 'greatgrand']])
def test_bad_remote_snapshot_is_not_priority(registry, ancestors):
    assert delivery.request_id(envelope('leader', ancestors)) is None


def test_expired_request_has_no_priority(registry):
    request = envelope('leader', [])
    request.update(ttl_minutes=1, timestamp='2000-01-01T00:00:00+00:00')
    assert delivery.request_id(request) is None


@pytest.mark.asyncio
async def test_frozen_outbound_two_notifications_despite_reparenting_and_failure(registry, monkeypatch):
    monkeypatch.setattr(delivery, 'ESCALATE_AFTER_S', 0.6)
    future = asyncio.get_running_loop().create_future()
    sent = []
    app = SimpleNamespace(seat=SimpleNamespace(agent_id='worker', send=lambda to, body, **kw: sent.append((to, kw)) or False),
                          conversation=[{'role': 'user'}], _edit=lambda *args: None, _system=lambda *args: None)
    frozen = authority.create(app, IDENT, approver='leader', route='spawner', timeout=1.5)
    (registry / 'leader.json').write_text(json.dumps({'agent_id': 'leader', 'spawned_by': 'replacement'}))
    with pytest.raises(TimeoutError):
        await delivery.wait_for_answer(app, future, approver='leader', ident=IDENT,
                                       message='test', timeout=1.5, created_at=frozen.created_at)
    assert [target for target, _ in sent] == ['parent', 'grandparent']
    assert all(options['approval_ancestors'] == ('parent', 'grandparent') for _, options in sent)
    assert app.conversation[0]['approval_delivery'][IDENT]['frozen_ancestor_ids'] == ['parent', 'grandparent']
    # Notification failure did not modify the authority clock or original expiry.
    assert app._approval_authority_records[IDENT]['authority'] is frozen
    assert not authority.can_answer(app, IDENT, {'to': 'worker', 'from': 'parent'})
