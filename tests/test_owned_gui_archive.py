"""GUI archive read must not manufacture a conversation lease or write target."""
import json
from types import SimpleNamespace

import pytest

from litetui import gui_rpc, paths

CID = '33333333-3333-4333-8333-333333333333'


def test_archive_rpc_read_no_missing_lease_created(tmp_path, monkeypatch):
    archive = tmp_path / '.convos' / CID
    archive.mkdir(parents=True)
    transcript = archive / 'convo.jsonl'
    transcript.write_text(json.dumps({'type': 'meta', 'id': CID}) + '\n')
    before = transcript.read_bytes()
    monkeypatch.setattr(paths, 'CONVO_DIR', archive.parent)
    app = SimpleNamespace(convo_id='different', _agent_session=None)
    result = gui_rpc._conversations(app, 'read', {'session_id': CID})
    assert result['read_only'] is True
    assert 'archive' in result['ownership_reason']
    assert transcript.read_bytes() == before
    assert {p.name for p in archive.iterdir()} == {'convo.jsonl'}


@pytest.mark.parametrize('action', ['create', 'open', 'rename', 'edit', 'retry'])
def test_unowned_mutable_rpc_refuses_before_app_or_filesystem_side_effect(tmp_path, monkeypatch, action):
    monkeypatch.setattr(paths, 'CONVO_DIR', tmp_path / '.convos')
    app = SimpleNamespace(convo_id=CID, _agent_session=None)
    with pytest.raises(ValueError, match='owned agent session'):
        gui_rpc._conversations(app, action, {'session_id': CID})
    assert not list(tmp_path.iterdir())



def test_owned_rpc_path_never_falls_back_to_archive_or_other_agent(tmp_path, monkeypatch):
    from litetui.agent_launch_context import create
    from litetui.agent_store import StoreError
    archive = tmp_path / '.convos' / CID
    archive.mkdir(parents=True)
    (archive / 'convo.jsonl').write_text('ARCHIVE')
    monkeypatch.setattr(paths, 'CONVO_DIR', archive.parent)
    with create(tmp_path, 'QuietHelm', agent_id='11111111-1111-4111-8111-111111111111',
                backend='codex', model='fixture', thinking_level='high') as session:
        app = SimpleNamespace(_agent_session=session)
        expected = session.conversation_directory(CID) / 'convo.jsonl'
        assert gui_rpc._session_path(CID, app) == expected
        assert not expected.exists()
        for invalid in ('../escape', str(archive), 'not-a-uuid'):
            with pytest.raises(StoreError):
                gui_rpc._session_path(invalid, app)
    assert {p.name for p in archive.iterdir()} == {'convo.jsonl'}
