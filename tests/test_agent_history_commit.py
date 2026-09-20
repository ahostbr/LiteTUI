import json
import pytest
from litetui.conversation import ConversationRepository


@pytest.fixture
def store(tmp_path):
    repository = ConversationRepository()
    repository.convo_id = 'parent'
    repository.convo_dir = tmp_path / '.convos' / 'parent'
    repository.convo_path = repository.convo_dir / 'convo.jsonl'
    repository.convo_dir.mkdir(parents=True)
    repository.convo_path.write_text('{"type":"meta","id":"parent"}\n')
    repository.acquire()
    yield repository
    repository.release()


def test_receipt_message_commits_once_and_replays_as_normal_message(store):
    message = {'role': 'user', 'content': '[child completion] done'}
    assert store.commit_child_message('completion', message) is True
    assert store.commit_child_message('completion', message) is True
    assert ConversationRepository.read(store.convo_path)[1] == [message]
    records = [json.loads(line) for line in store.convo_path.read_text().splitlines()]
    assert records[-1]['completion_id'] == 'completion'
    assert 'completion_id' not in records[-1]['message']


def test_conflicting_receipt_cannot_overwrite_committed_history(store):
    store.commit_child_message('completion', {'role': 'user', 'content': 'first'})
    with pytest.raises(ValueError, match='conflict'):
        store.commit_child_message('completion', {'role': 'user', 'content': 'changed'})
    assert ConversationRepository.read(store.convo_path)[1][0]['content'] == 'first'


def test_commit_requires_owned_materialized_store(store):
    store.release()
    with pytest.raises(ValueError, match='owned'):
        store.commit_child_message('completion', {'role': 'user', 'content': 'done'})


def test_fsync_failure_is_not_silent_and_retry_syncs_existing_record(store, monkeypatch):
    import os
    actual = os.fsync
    calls = []
    def failed(fd):
        calls.append(fd)
        raise OSError('fsync failed')
    monkeypatch.setattr(os, 'fsync', failed)
    message = {'role': 'user', 'content': 'done'}
    with pytest.raises(OSError, match='fsync failed'):
        store.commit_child_message('completion', message)
    monkeypatch.setattr(os, 'fsync', actual)
    assert store.commit_child_message('completion', message)
    assert ConversationRepository.read(store.convo_path)[1] == [message]
    assert calls


def test_torn_tail_preserved_and_new_record_separated(store):
    with store.convo_path.open('ab') as stream: stream.write(b'{"torn":')
    assert store.commit_child_message('completion', {'role': 'user', 'content': 'done'})
    assert b'{"torn":\n' in store.convo_path.read_bytes()
    assert len(ConversationRepository.read(store.convo_path)[1]) == 1
