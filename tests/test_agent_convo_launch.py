"""UUID-only CLI resume resolves owned home; no archive guess or live host."""
import json
import sys

import pytest

from litetui import cli, paths, settings, shared_state, image_viewer
from litetui.agent_launch_context import create, acquire
from litetui.agent_store import StoreError

AID = '11111111-1111-4111-8111-111111111111'
BID = '22222222-2222-4222-8222-222222222222'
CID = '33333333-3333-4333-8333-333333333333'


def seed(root, name='QuietHelm', identity=AID):
    with create(root, name, agent_id=identity, backend='codex', model='fixture', thinking_level='high') as session:
        transcript = session.conversation_directory(CID) / 'convo.jsonl'
        transcript.parent.mkdir()
        transcript.write_text('{"type":"msg","message":{"role":"user","content":"fixture"}}\n')
    return root / '.agents' / name


@pytest.fixture
def launch_world(tmp_path, monkeypatch):
    import litetui.app as app_module
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(settings, 'load', settings.Settings)
    monkeypatch.setattr(shared_state, 'check_data_version', lambda _: None)
    monkeypatch.setattr(image_viewer, 'init_image_backend', lambda: None)
    monkeypatch.setattr(app_module, 'wants_ansi_fallback', lambda: False)
    seen = []
    class App:
        def __init__(self, **kw):
            seen.append(kw)
        def run(self):
            pass
    monkeypatch.setattr(app_module, 'LiteTUI', App)
    monkeypatch.setattr(sys, 'argv', ['litetui', '--convo', CID])
    return tmp_path, seen


def test_actual_cli_uuid_only_acquires_exact_owner_and_releases(launch_world):
    root, seen = launch_world
    home = seed(root)
    before = (home / 'settings.json').read_bytes()
    cli.main()
    assert len(seen) == 1
    session = seen[0]['agent_session']
    assert session._agent.directory == home
    assert seen[0]['initial_model'] == 'fixture'
    assert seen[0]['convo_id'] == CID
    with acquire(root, 'QuietHelm', conversation_id=CID):
        pass
    assert (home / 'settings.json').read_bytes() == before
    assert not (root / '.convos').exists()


@pytest.mark.parametrize('failure', ['absent', 'ambiguous', 'inactive', 'corrupt'])
def test_actual_cli_uuid_only_never_falls_back_to_archive(launch_world, failure, capsys):
    root, seen = launch_world
    archive = root / '.convos' / CID
    archive.mkdir(parents=True)
    (archive / 'convo.jsonl').write_bytes(b'legacy archive evidence')
    if failure != 'absent':
        home = seed(root)
        if failure == 'ambiguous':
            seed(root, 'OtherAgent', BID)
        elif failure == 'inactive':
            (home / '.agent.initializing').write_text('incomplete')
        else:
            (home / 'settings.json').write_text('{bad')
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert 'pass --agent <Name>' in capsys.readouterr().err
    assert seen == []
    assert (archive / 'convo.jsonl').read_bytes() == b'legacy archive evidence'


def test_unnamed_managed_child_fresh_cli_unchanged(launch_world, monkeypatch):
    root, seen = launch_world
    # Actual managed transport uses no --convo; its validated operation-owned
    # root/depth does not become a blanket legacy resume exception.
    monkeypatch.setattr(sys, 'argv', ['litetui', '--backend', 'codex', '--model', 'fixture'])
    monkeypatch.setenv('LITETUI_AGENT_DEPTH', '1')
    cli.main()
    assert len(seen) == 1 and seen[0]['agent_session'] is None
    assert not (root / '.agents').exists()
