import pytest


def result(child='child-1'):
    return {'child_id': child, 'conversation_id': 'convo-1', 'status': 'completed',
            'summary': 'done', 'branch': 'agent/fixture', 'evidence': ['test.log'],
            'cleanup': {'state': 'confirmed'}}


def test_result_survives_restart_and_ack_is_parent_scoped(tmp_path):
    from litetui.agent_inbox import AgentInbox
    path = tmp_path / 'inbox.sqlite'
    inbox = AgentInbox(path)
    completion = inbox.persist('parent-a', result())
    restored = AgentInbox(path)
    assert restored.pending('parent-b') == []
    assert restored.pending('parent-a') == [{'completion_id': completion, 'result': result()}]
    assert not restored.acknowledge('parent-b', completion)
    assert restored.pending('parent-a')
    assert restored.acknowledge('parent-a', completion)
    assert restored.acknowledge('parent-a', completion)
    assert restored.pending('parent-a') == []
    assert restored.get('parent-a', completion) == result()
    assert restored.get('parent-b', completion) is None


def test_duplicate_completion_is_idempotent_but_conflict_is_rejected(tmp_path):
    from litetui.agent_inbox import AgentInbox
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    first = inbox.persist('parent', result())
    assert inbox.persist('parent', result()) == first
    assert len(inbox.pending('parent')) == 1
    changed = result()
    changed['status'] = 'failed'
    with pytest.raises(ValueError, match='conflict'):
        inbox.persist('parent', changed)
    assert inbox.get('parent', first) == result()


def test_notification_failure_keeps_committed_result_for_retry(tmp_path):
    from litetui.agent_inbox import AgentInbox
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    seen = []
    def disconnected(event):
        seen.append(event)
        assert inbox.get('parent', event['completion_id']) == result()
        raise ConnectionError('parent disconnected')
    with pytest.raises(ConnectionError):
        inbox.publish('parent', result(), notify=disconnected)
    assert len(inbox.pending('parent')) == 1
    assert inbox.publish('parent', result(), notify=seen.append) == seen[0]['completion_id']
    assert seen[0] == seen[1]


@pytest.mark.parametrize('payload', [{}, {'child_id': '../escape'}, {**result(), 'status': 'running'},
    {**result(), 'cleanup': None}, {**result(), 'evidence': 'not-list'}])
def test_invalid_completion_never_persists(tmp_path, payload):
    from litetui.agent_inbox import AgentInbox
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    with pytest.raises(ValueError):
        inbox.persist('parent', payload)
    assert inbox.pending('parent') == []


def test_concurrent_duplicate_writers_have_one_durable_completion(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from litetui.agent_inbox import AgentInbox
    path = tmp_path / 'inbox.sqlite'
    a, b = AgentInbox(path), AgentInbox(path)
    barrier = Barrier(2)
    def write(inbox):
        barrier.wait()
        return inbox.persist('parent', result())
    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(write, [a, b]))
    assert ids[0] == ids[1]
    assert len(a.pending('parent')) == 1


def test_disk_failure_never_notifies_or_exposes_partial_completion(tmp_path):
    import sqlite3
    from litetui.agent_inbox import AgentInbox
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    with inbox._transaction() as db:
        db.execute("CREATE TRIGGER deny_insert BEFORE INSERT ON completions BEGIN SELECT RAISE(ABORT, 'disk fault fixture'); END")
    notifications = []
    with pytest.raises(sqlite3.IntegrityError, match='disk fault fixture'):
        inbox.publish('parent', result(), notify=notifications.append)
    assert notifications == []
    assert inbox.pending('parent') == []
    with inbox._transaction() as db:
        db.execute('DROP TRIGGER deny_insert')
    inbox.publish('parent', result(), notify=notifications.append)
    assert len(notifications) == 1
    assert len(inbox.pending('parent')) == 1


def test_replay_ack_only_after_explicit_durable_parent_acceptance(tmp_path):
    from litetui.agent_inbox import AgentInbox
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    ident = inbox.persist('parent', result())
    assert inbox.replay('parent', accept=lambda event: None) == []
    assert len(inbox.pending('parent')) == 1
    seen = []
    def accepted(event):
        seen.append(event)
        return True
    assert inbox.replay('parent', accept=accepted) == [ident]
    assert not inbox.pending('parent')
    assert inbox.replay('parent', accept=accepted) == []
    assert len(seen) == 1


def test_replay_failure_keeps_pending_and_other_parent_cannot_consume(tmp_path):
    from litetui.agent_inbox import AgentInbox
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    inbox.persist('parent', result())
    def failed(event): raise OSError('parent persistence failed')
    assert inbox.replay('other', accept=failed) == []
    with pytest.raises(OSError):
        inbox.replay('parent', accept=failed)
    assert len(inbox.pending('parent')) == 1

def test_crash_between_acceptance_and_ack_redelivers_same_id(tmp_path):
    from litetui.agent_inbox import AgentInbox
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    completion = inbox.persist('parent', result())
    accepted = set()
    effects = []
    def accept(event):
        if event['completion_id'] not in accepted:
            accepted.add(event['completion_id'])
            effects.append(event['result'])
        return True
    def crashed(*args): raise OSError('ACK interrupted')
    inbox.acknowledge = crashed
    with pytest.raises(OSError):
        inbox.replay('parent', accept=accept)
    restored = AgentInbox(tmp_path / 'inbox.sqlite')
    assert restored.replay('parent', accept=accept) == [completion]
    assert effects == [result()]