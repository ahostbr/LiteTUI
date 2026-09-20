import pytest
from litetui.agent_inbox import AgentInbox


def result():
    return {'child_id': 'child', 'conversation_id': 'convo', 'status': 'completed',
            'summary': 'done', 'evidence': [], 'cleanup': {'state': 'confirmed'}}


def test_acceptance_commits_receipt_before_ack_and_survives_restart(tmp_path):
    from litetui.agent_receipts import ParentReceipts
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    ident = inbox.persist('parent', result())
    assert inbox.replay('parent', accept=lambda event: receipts.accept('parent', event)) == [ident]
    assert not inbox.pending('parent')
    restored = ParentReceipts(tmp_path / 'receipts.sqlite')
    assert restored.pending('parent') == [{'completion_id': ident, 'result': result()}]
    assert not restored.pending('other')
    assert restored.accept('parent', {'completion_id': ident, 'result': result()}) is True
    assert len(restored.pending('parent')) == 1


def test_receipt_conflict_is_not_acknowledged(tmp_path):
    from litetui.agent_receipts import ParentReceipts
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    event = {'completion_id': 'completion', 'result': result()}
    receipts.accept('parent', event)
    with pytest.raises(ValueError, match='conflict'):
        receipts.accept('parent', {**event, 'result': {**result(), 'summary': 'changed'}})
    assert receipts.pending('parent') == [event]


def test_durable_receipt_is_not_consumed_by_notification(tmp_path):
    from litetui.agent_receipts import ParentReceipts
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    event = {'completion_id': 'completion', 'result': result()}
    receipts.accept('parent', event)
    seen = []
    receipts.deliver('parent', commit=seen.append)
    assert receipts.pending('parent') == [event]
    assert not receipts.mark_applied('other', 'completion')
    assert receipts.mark_applied('parent', 'completion')
    assert not receipts.pending('parent')
    assert receipts.accept('parent', event)
    assert not receipts.pending('parent')


def test_parent_commit_failure_keeps_receipt_for_replay(tmp_path):
    from litetui.agent_receipts import ParentReceipts
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    receipts.accept('parent', {'completion_id': 'completion', 'result': result()})
    def fail(event): raise OSError('transcript full')
    with pytest.raises(OSError): receipts.deliver('parent', commit=fail)
    assert len(receipts.pending('parent')) == 1
    assert receipts.deliver('parent', commit=lambda event: True) == ['completion']
    assert not receipts.pending('parent')


def test_disk_failure_never_acknowledges_inbox(tmp_path):
    import sqlite3
    from litetui.agent_receipts import ParentReceipts
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    inbox.persist('parent', result())
    with receipts._transaction() as db:
        db.execute("CREATE TRIGGER deny_receipt BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT, 'disk fault'); END")
    with pytest.raises(sqlite3.IntegrityError):
        inbox.replay('parent', accept=lambda event: receipts.accept('parent', event))
    assert len(inbox.pending('parent')) == 1
    assert not receipts.pending('parent')
