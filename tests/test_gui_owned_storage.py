"""Actual GUI storage consumers with fixture-only owned and legacy stores."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import gui_rpc, paths
from litetui.agent_launch_context import acquire
from litetui.agent_store import StoreError
from litetui.agent_ownership import OwnershipError
from test_storage_catalog import AID, CID, agent


def persisted(transcript, text):
    transcript.write_text(json.dumps({'type': 'meta', 'id': CID, 'created': 1}) + '\n'
        + json.dumps({'type': 'msg', 'message': {'role': 'user', 'content': text}, 'ts': 2}) + '\n')


@pytest.fixture
def owned(tmp_path, monkeypatch):
    transcript = agent(tmp_path)
    persisted(transcript, 'owned bytes')
    archive = tmp_path / '.convos' / CID / 'convo.jsonl'
    archive.parent.mkdir(parents=True)
    persisted(archive, 'archive bytes')
    monkeypatch.setattr(paths, 'CONVO_DIR', archive.parent.parent)
    with acquire(tmp_path, 'QuietHelm') as session:
        app = SimpleNamespace(_agent_session=session, convo_id=CID, conversation=[],
            convo_dir=transcript.parent, _chat_running=lambda: False)
        yield app, transcript.parent.parent.parent, archive


def snapshot(root):
    # Windows mandatory byte lock denies reads of the already-held lease inode.
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and p.name != '.agent.lease'}


def test_gui_owned_list_read_and_memory_use_agent_not_archive(owned, monkeypatch):
    app, directory, archive = owned
    (directory / 'memory.md').write_text('agent memory')
    (directory / 'soul.md').write_text('agent soul')
    (directory / 'memories' / 'nested').mkdir(parents=True)
    (directory / 'memories' / 'nested' / 'topic.md').write_text('agent topic')
    (directory / 'internal.md').write_text('not memory')
    (archive.parent / 'memory.md').write_text('archive memory')
    (app.convo_dir / 'memory.md').write_text('per-conversation companion retained')
    before = snapshot(directory.parent.parent)
    rows = gui_rpc._conversations(app, 'list', {})
    assert len(rows) == 1 and rows[0]['agent_id'] == AID
    assert rows[0]['agent_name'] == 'QuietHelm'
    def forbidden(*args, **kwargs):
        pytest.fail('readonly owned browsing must never acquire legacy session lease')
    monkeypatch.setattr(gui_rpc, 'Lease', forbidden)
    read = gui_rpc._conversations(app, 'read', {'session_id': CID})
    assert read['messages'][0]['content'] == 'owned bytes'
    assert not read['read_only']
    names = {row['name'] for row in gui_rpc._memory(app, 'list', {})}
    assert names == {'memory.md', 'soul.md', 'memories/nested/topic.md'}
    assert gui_rpc._memory(app, 'read', {'name': 'memory.md'})['text'] == 'agent memory'
    assert snapshot(directory.parent.parent) == before


def test_gui_owned_write_preserves_archive_and_conversation_companions(owned):
    app, directory, archive = owned
    (archive.parent / 'memory.md').write_text('archive')
    (app.convo_dir / 'memory.md').write_text('companion')
    before = snapshot(archive.parent)
    result = gui_rpc._memory(app, 'write', {'name': 'memories/nested/topic.md', 'text': 'new agent topic'})
    assert result['text'] == 'new agent topic'
    assert (directory / 'memories' / 'nested' / 'topic.md').read_text() == 'new agent topic'
    assert (app.convo_dir / 'memory.md').read_text() == 'companion'
    assert snapshot(archive.parent) == before


@pytest.mark.parametrize('name', ['../memory.md', 'settings.json', 'internal.md',
    'conversations/' + CID + '/memory.md', 'memories/../memory.md', 'memories/topic:stream.md'])
def test_gui_owned_memory_refuses_non_memory_or_escape(owned, name):
    app, directory, _ = owned
    before = snapshot(directory)
    with pytest.raises(ValueError):
        gui_rpc._memory(app, 'write', {'name': name, 'text': 'must not write'})
    assert snapshot(directory) == before


def test_gui_owned_browsing_never_falls_back_archive(owned):
    app, directory, _ = owned
    missing = '44444444-4444-4444-8444-444444444444'
    legacy = directory.parent.parent / '.convos' / missing / 'convo.jsonl'
    legacy.parent.mkdir(parents=True)
    persisted(legacy, 'not owned')
    with pytest.raises(FileNotFoundError):
        gui_rpc._conversations(app, 'read', {'session_id': missing})
    assert not (directory / 'conversations' / missing).exists()


@pytest.mark.parametrize('action', ['read', 'list', 'write'])
def test_gui_memory_released_capability_refuses_all_actions(owned, action):
    app, directory, _ = owned
    before = snapshot(directory)
    app._agent_session.release()
    with pytest.raises(OwnershipError):
        gui_rpc._memory(app, action, {'name': 'memory.md', 'text': 'refused'})
    assert snapshot(directory) == before


@pytest.mark.parametrize('action', ['read', 'list', 'write'])
def test_gui_original_memory_link_rejected_before_resolving(owned, action, tmp_path):
    app, directory, _ = owned
    target = tmp_path / 'external.md'
    target.write_text('external unchanged')
    try:
        (directory / 'memory.md').symlink_to(target)
    except OSError:
        pytest.skip('host lacks fixture symlink privilege')
    with pytest.raises(StoreError, match='Linked'):
        gui_rpc._memory(app, action, {'name': 'memory.md', 'text': 'refused'})
    assert target.read_text() == 'external unchanged'


def test_gui_memory_revalidates_authority_after_temporary_write(owned, monkeypatch):
    app, directory, _ = owned
    original = Path.open
    def changed(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        if path.parent == directory and path.name.startswith('.memory.md.'):
            settings = json.loads((directory / 'settings.json').read_text())
            settings['execution']['model'] = 'changed'
            (directory / 'settings.json').write_text(json.dumps(settings))
        return handle
    monkeypatch.setattr(Path, 'open', changed)
    with pytest.raises(StoreError, match='authority changed'):
        gui_rpc._memory(app, 'write', {'name': 'memory.md', 'text': 'not published'})
    assert not (directory / 'memory.md').exists()


def test_gui_legacy_memory_still_reads_and_writes_legacy_folder(tmp_path, monkeypatch):
    legacy = tmp_path / '.convos' / CID
    legacy.mkdir(parents=True)
    monkeypatch.setattr(paths, 'CONVO_DIR', legacy.parent)
    calls = []
    app = SimpleNamespace(convo_id=CID, convo_dir=legacy, _chat_running=lambda: False,
        store=SimpleNamespace(acquire=lambda: calls.append('legacy lease')))
    gui_rpc._memory(app, 'write', {'name': 'memory.md', 'text': 'legacy continues'})
    assert calls == ['legacy lease']
    assert gui_rpc._memory(app, 'read', {'name': 'memory.md'})['text'] == 'legacy continues'
    assert not (tmp_path / '.agents').exists()
