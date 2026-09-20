import pytest

from litetui.agent_inbox import AgentInbox
from litetui.agent_receipts import ParentReceipts


def event():
    return {'completion_id': 'completion', 'result': {
        'child_id': 'child', 'conversation_id': 'child-convo',
        'status': 'completed', 'summary': 'done', 'cleanup': {}, 'evidence': []}}


def test_binding_survives_restart_and_filters_other_conversations(tmp_path):
    path = tmp_path / 'receipts.sqlite'
    receipts = ParentReceipts(path)
    assert receipts.accept_for_conversation('parent', 'original', event()) is True
    restored = ParentReceipts(path)
    assert restored.pending_for_conversation('parent', 'original') == [event()]
    assert restored.pending_for_conversation('parent', 'switched') == []
    assert restored.pending_for_conversation('other', 'original') == []
    assert restored.accept_for_conversation('parent', 'original', event()) is True
    with pytest.raises(ValueError, match='conversation'):
        restored.accept_for_conversation('parent', 'switched', event())


def test_unbound_legacy_receipt_requires_explicit_binding(tmp_path):
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    receipts.accept('parent', event())
    assert receipts.pending_for_conversation('parent', 'original') == []
    receipts.accept_for_conversation('parent', 'original', event())
    assert receipts.pending_for_conversation('parent', 'original') == [event()]
    assert receipts.mark_applied('parent', 'completion')
    assert receipts.accept_for_conversation('parent', 'original', event())
    assert receipts.pending_for_conversation('parent', 'original') == []


def test_binding_failure_does_not_ack_source(tmp_path):
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    ident = inbox.persist('parent', event()['result'])
    receipts.accept_for_conversation('parent', 'original', {
        **event(), 'completion_id': ident})
    with pytest.raises(ValueError, match='conversation'):
        inbox.replay('parent', accept=lambda e: receipts.accept_for_conversation('parent', 'wrong', e))
    assert len(inbox.pending('parent')) == 1


def test_delivery_never_calls_consumer_for_another_conversation(tmp_path):
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    receipts.accept_for_conversation('parent', 'original', event())
    seen = []
    assert receipts.deliver_for_conversation('parent', 'switched', commit=seen.append) == []
    assert seen == []
    assert receipts.deliver_for_conversation('parent', 'original', commit=seen.append) == []
    assert seen == [event()]
    assert receipts.pending_for_conversation('parent', 'original') == [event()]
    assert receipts.deliver_for_conversation('parent', 'original', commit=lambda e: True) == ['completion']
    assert receipts.pending_for_conversation('parent', 'original') == []


def test_existing_database_is_migrated_without_losing_receipts(tmp_path):
    import sqlite3
    path = tmp_path / 'receipts.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE receipts (parent TEXT NOT NULL, id TEXT NOT NULL, '
                   'payload TEXT NOT NULL, created REAL NOT NULL, applied INTEGER NOT NULL DEFAULT 0, '
                   'PRIMARY KEY(parent,id))')
    receipts = ParentReceipts(path)
    assert receipts.accept_for_conversation('parent', 'original', event())
    assert receipts.pending_for_conversation('parent', 'original') == [event()]


def test_replay_uses_launch_registry_not_current_chat(tmp_path):
    from litetui.agent_registry import AgentRegistry
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    ident = inbox.persist('parent', event()['result'])
    assert receipts.replay_from_inbox('parent', inbox=inbox, registry=registry) == []
    assert len(inbox.pending('parent')) == 1
    registry.claim('parent', 'child', limit=1, parent_conversation='original')
    assert receipts.replay_from_inbox('parent', inbox=inbox, registry=registry) == [ident]
    assert not inbox.pending('parent')
    assert receipts.pending_for_conversation('parent', 'original')[0]['completion_id'] == ident
    assert not receipts.pending_for_conversation('parent', 'switched')
