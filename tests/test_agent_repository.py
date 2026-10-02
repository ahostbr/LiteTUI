"""T0308 ownership-bound repository, explicit root and durable agent memory."""
import json

import pytest

from litetui import agent_ownership, agent_store, paths, shared_state
from litetui.conversation import ConversationRepository

AID = '11111111-1111-4111-8111-111111111111'
CID = '33333333-3333-4333-8333-333333333333'
DID = '44444444-4444-4444-8444-444444444444'


@pytest.fixture
def owned(tmp_path):
    directory = tmp_path / '.agents' / 'QuietHelm'
    directory.mkdir(parents=True)
    (directory / 'settings.json').write_text(json.dumps({
        'schema_version': 1, 'name': directory.name, 'agent_id': AID,
        'execution': {'backend': 'codex', 'model': 'fixture', 'thinking_level': 'high'},
    }), encoding='utf-8')
    with agent_ownership.AgentSession.acquire_existing(agent_store.AgentStore(tmp_path), agent_id=AID) as session:
        yield session


def test_new_conversations_share_agent_memory_and_explicit_root(owned, monkeypatch):
    roots = []
    monkeypatch.setattr(shared_state, 'check_data_version', lambda root: roots.append(root))
    repository = ConversationRepository(agent_session=owned)
    try:
        repository.stage(CID)
        assert not repository.convo_dir.exists()
        repository.seed()
        assert roots == [owned.store.data_root]
        assert repository.convo_dir == owned.conversation_directory(CID)
        assert not (repository.convo_dir / 'memory.md').exists()
        (owned.memory_root / 'handoff.md').write_text('persistent-human-note')
        repository.stage(DID)
        assert owned.authority.agent_id == AID
        with pytest.raises(agent_ownership.OwnershipError):
            agent_ownership.AgentSession.acquire_existing(owned.store, agent_id=AID)
        repository.seed()
        assert roots == [owned.store.data_root, owned.store.data_root]
        assert (owned.memory_root / 'handoff.md').read_text() == 'persistent-human-note'
        assert not (repository.convo_dir / 'memories').exists()
    finally:
        repository.release()
    assert owned.authority.agent_id == AID  # convo lease release is not agent release


def test_compaction_record_stays_same_uuid_and_append_only(owned, monkeypatch):
    monkeypatch.setattr(shared_state, 'check_data_version', lambda root: None)
    repository = ConversationRepository(agent_session=owned)
    try:
        repository.stage(CID)
        repository.seed()
        repository.pending = False
        repository.write_record({'type': 'msg', 'message': {'role': 'user', 'content': 'original'}})
        before = repository.convo_path.read_bytes()
        repository.record_truncate(1, [{'role': 'assistant', 'content': 'summary'}], True)
        assert repository.convo_id == CID and repository.convo_dir == owned.conversation_directory(CID)
        assert repository.convo_path.read_bytes().startswith(before)
        assert json.loads(repository.convo_path.read_text().splitlines()[-1])['type'] == 'truncate'
    finally:
        repository.release()


def test_resume_stays_in_selected_agent_and_rejects_foreign_paths_before_writes(owned, tmp_path, monkeypatch):
    roots = []
    monkeypatch.setattr(shared_state, 'check_data_version', lambda root: roots.append(root))
    repository = ConversationRepository(agent_session=owned)
    try:
        repository.stage(CID)
        outside = tmp_path / '.agents' / 'Other' / 'conversations' / DID / 'convo.jsonl'
        with pytest.raises(agent_store.StoreError):
            repository.adopt(outside, DID)
        assert not outside.parent.exists() and roots == []
        own = owned.conversation_directory(DID)
        own.mkdir(parents=True)
        transcript = own / 'convo.jsonl'
        transcript.write_bytes(b'original archive\n')
        repository.adopt(transcript, DID)
        assert repository.convo_id == DID and not repository.pending
        assert transcript.read_bytes() == b'original archive\n'
        assert roots == [owned.store.data_root]
    finally:
        repository.release()


def test_released_agent_blocks_every_repository_mutation(owned, monkeypatch):
    monkeypatch.setattr(shared_state, 'check_data_version', lambda root: pytest.fail('must not write'))
    repository = ConversationRepository(agent_session=owned)
    repository.stage(CID)
    owned.release()
    for operation in (lambda: repository.stage(DID), repository.acquire):
        with pytest.raises(agent_ownership.OwnershipError):
            operation()
    repository.seed()  # existing repository error reporting contract catches OSError
    assert 'OwnershipError' in repository.persist_error
    assert not repository.convo_dir.exists()


def test_legacy_absent_context_keeps_original_paths_and_seeds(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'CONVO_DIR', tmp_path / '.convos')
    roots = []
    monkeypatch.setattr(shared_state, 'check_data_version', lambda root: roots.append(root))
    repository = ConversationRepository()
    try:
        repository.stage('legacy-id')
        repository.seed()
        assert repository.convo_dir == tmp_path / '.convos' / 'legacy-id'
        assert (repository.convo_dir / 'memory.md').exists()
        assert roots == [tmp_path]
    finally:
        repository.release()


@pytest.mark.parametrize('change', ['released', 'execution', 'transcript', 'directory', 'lease'])
def test_child_receipt_revalidates_borrowed_agent_and_exact_paths_before_writes(owned, monkeypatch, change):
    monkeypatch.setattr(shared_state, 'check_data_version', lambda root: None)
    repository = ConversationRepository(agent_session=owned)
    repository.stage(CID)
    repository.seed()
    repository.pending = False
    repository.write_record({'type': 'msg', 'message': {'role': 'user', 'content': 'original'}})
    original_path = repository.convo_path
    before = original_path.read_bytes()
    try:
        if change == 'released':
            owned.release()
        elif change == 'execution':
            path = owned.memory_root / 'settings.json'
            raw = json.loads(path.read_text())
            raw['execution']['model'] = 'changed'
            path.write_text(json.dumps(raw))
        elif change == 'transcript':
            repository.convo_path = original_path.with_name('foreign.jsonl')
        elif change == 'directory':
            repository.convo_dir = owned.conversation_directory(DID)
        else:
            repository._lease.path = repository.convo_dir / '.wrong.lease'
        # Must fail before even creating the coordinated lock companion.
        monkeypatch.setattr(shared_state, 'coordinated_write',
                            lambda *_: pytest.fail('must not create mutation lock'))
        with pytest.raises((agent_store.StoreError, agent_ownership.OwnershipError)):
            repository.commit_child_message('fixture-completion', {'role': 'user', 'content': 'receipt'})
        assert original_path.read_bytes() == before
        assert not original_path.with_name('foreign.jsonl').exists()
        assert not original_path.with_name(original_path.name + '.lock').exists()
    finally:
        repository.release()
