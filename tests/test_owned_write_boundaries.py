"""No-session mutable fallbacks must refuse before touching retained archives."""
from pathlib import Path

import pytest

from litetui import paths, tasks
from litetui.agent_store import StoreError
from litetui.conversation import ConversationRepository

CID = '33333333-3333-4333-8333-333333333333'


def test_unowned_stage_refuses_without_legacy_state(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'CONVO_DIR', tmp_path / '.convos')
    with pytest.raises(StoreError, match='owned'):
        ConversationRepository().stage(CID)
    assert list(tmp_path.iterdir()) == []


def test_unowned_adopt_is_read_only_without_lease(tmp_path):
    archive = tmp_path / '.convos' / CID
    archive.mkdir(parents=True)
    transcript = archive / 'convo.jsonl'
    transcript.write_bytes(b'{"type":"meta"}\n')
    store = ConversationRepository()
    store.adopt(transcript, CID)
    assert not store.owned
    with pytest.raises(StoreError, match='owned'):
        store.acquire()
    with pytest.raises(StoreError, match='owned'):
        store.write_record({'type': 'msg'})
    assert set(p.name for p in archive.iterdir()) == {'convo.jsonl'}
    assert transcript.read_bytes() == b'{"type":"meta"}\n'


def test_unowned_task_destination_refuses_existing_archive(tmp_path, monkeypatch):
    archive = tmp_path / '.convos' / CID
    archive.mkdir(parents=True)
    monkeypatch.setattr(paths, 'CONVO_DIR', archive.parent)
    with pytest.raises(tasks.StoreNotBorn, match='owned'):
        tasks.convo_store_dir(CID)
    assert list(archive.iterdir()) == []
