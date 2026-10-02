"""Fresh child homes use real fixture files/leases; no provider or live host."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import agent_launch_context as launch, agent_ownership as ownership
from litetui.agent_store import AgentStore, AGENT_SEED_FILES, StoreError
from litetui.conversation import ConversationRepository

AID = '11111111-1111-4111-8111-111111111111'


def create(root):
    return launch.create(root, 'QuietHelm', agent_id=AID, backend='codex',
                         model='fixture', thinking_level='high')


def test_birth_is_actual_home_and_first_conversation(tmp_path):
    with create(tmp_path) as session:
        home = tmp_path / '.agents' / 'QuietHelm'
        assert session.memory_root == home
        assert session.authority.agent_id == AID
        cid = session.initial_conversation_id
        directory = home / 'conversations' / cid
        assert directory.is_dir()
        assert set(p.name for p in (home / 'conversations').iterdir()) == {cid}
        for name, seed in AGENT_SEED_FILES.items():
            assert (home / name).read_text(encoding='utf-8') == seed
        repository = ConversationRepository(agent_session=session)
        repository.stage(cid)
        assert repository.convo_dir == directory
        repository.pending = False
        repository.write_record({'type': 'meta', 'id': cid})
        repository.write_record({'type': 'msg', 'message': {'role': 'user', 'content': 'born here'}})
        repository.release()
        assert ConversationRepository.read(directory / 'convo.jsonl')[1][0]['content'] == 'born here'
        assert not (tmp_path / '.convos').exists()
        with pytest.raises(ownership.OwnershipError):
            launch.acquire(tmp_path, 'QuietHelm')
    with launch.acquire(tmp_path, 'QuietHelm', conversation_id=cid) as resumed:
        assert resumed.memory_root == home
        assert resumed.authority.model == 'fixture'


@pytest.mark.parametrize('failure', ['memory.md', 'soul.md', 'handoff.md', 'conversations'])
def test_seed_failure_stays_inactive_and_does_not_touch_foreign(tmp_path, monkeypatch, failure):
    foreign = tmp_path / '.convos' / AID
    foreign.mkdir(parents=True)
    original_bytes = b'foreign archive never copied or changed'
    (foreign / 'memory.md').write_bytes(original_bytes)
    original_open, original_mkdir = Path.open, Path.mkdir
    def opened(path, *args, **kwargs):
        if path.name == failure and '.agents' in path.parts:
            raise OSError('fixture interrupted seed')
        return original_open(path, *args, **kwargs)
    def mkdir(path, *args, **kwargs):
        if path.name == failure and '.agents' in path.parts:
            raise OSError('fixture interrupted seed')
        return original_mkdir(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', opened)
        patch.setattr(Path, 'mkdir', mkdir)
        with pytest.raises(OSError, match='interrupted seed'):
            create(tmp_path)
    home = tmp_path / '.agents' / 'QuietHelm'
    assert (home / '.agent.initializing').is_file()
    assert not (home / 'settings.json').exists()
    with pytest.raises(StoreError):
        launch.acquire(tmp_path, 'QuietHelm')
    with ownership._KernelLease(home / '.agent.lease'):
        pass
    assert (foreign / 'memory.md').read_bytes() == original_bytes


def test_cli_fresh_child_uses_home_before_app_and_releases(tmp_path, monkeypatch):
    import sys
    from litetui import cli, paths, settings, shared_state, image_viewer
    import litetui.app as app_module
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(settings, 'load', settings.Settings)
    monkeypatch.setattr(shared_state, 'check_data_version', lambda _: None)
    monkeypatch.setattr(image_viewer, 'init_image_backend', lambda: None)
    monkeypatch.setattr(app_module, 'wants_ansi_fallback', lambda: False)
    seen = []
    class App:
        def __init__(self, **kwargs):
            session = kwargs['agent_session']
            seen.append(session)
            assert session.memory_root == tmp_path / '.agents' / 'QuietHelm'
            assert (session.memory_root / 'memory.md').exists()
            assert session.initial_conversation_id
        def run(self):
            raise RuntimeError('fixture app stopped')
    monkeypatch.setattr(app_module, 'LiteTUI', App)
    monkeypatch.setattr(sys, 'argv', ['litetui', '--create-agent', 'QuietHelm', '--agent-id', AID,
                                     '--backend', 'codex', '--model', 'fixture', '--thinking-level', 'high'])
    with pytest.raises(RuntimeError, match='app stopped'):
        cli.main()
    assert len(seen) == 1
    with pytest.raises(ownership.OwnershipError):
        seen[0].authority
    with launch.acquire(tmp_path, 'QuietHelm'):
        pass
    assert not (tmp_path / '.convos').exists()
