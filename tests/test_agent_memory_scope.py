"""T0308 separate agent memory authorization, fixture-only and opt-in."""
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import agent_ownership, agent_store, tool_policy as policy

AID = '11111111-1111-4111-8111-111111111111'
CID = '33333333-3333-4333-8333-333333333333'


@pytest.fixture
def session(tmp_path):
    directory = tmp_path / '.agents' / 'QuietHelm'
    directory.mkdir(parents=True)
    (directory / 'settings.json').write_text(json.dumps({
        'schema_version': 1, 'name': 'QuietHelm', 'agent_id': AID,
        'execution': {'backend': 'codex', 'model': 'fixture', 'thinking_level': 'high'},
    }), encoding='utf-8')
    with agent_ownership.AgentSession.acquire_existing(agent_store.AgentStore(tmp_path), agent_id=AID) as owned:
        yield owned


@pytest.mark.parametrize('relative,expected', [
    ('memory.md', True), ('soul.md', True), ('handoff.md', True),
    ('memories/topic.md', True), ('memories/deeper/topic.md', True),
    ('settings.json', False), ('.agent.lease', False),
    ('.settings.initializing.json', False), ('unknown.md', False),
    ('conversations/' + CID + '/memory.md', False),
    ('conversations/' + CID + '/convo.jsonl', False),
    ('../KindGrid/memory.md', False), ('../../names.json', False),
    ('../../.agents.catalog.lease', False),
])
def test_only_current_agent_memory_files_qualify(tmp_path, session, relative, expected):
    root = session.memory_root
    decision = policy.evaluate(policy.STRICT, policy.WRITE_POLICY,
                               {'path': str(root / relative)}, tmp_path,
                               active_agent_memory_root=root,
                               active_conversation=root / 'conversations' / CID)
    assert (policy.SELF_STORE in decision.capabilities) == expected
    assert decision.action == (policy.ALLOW if expected else policy.CONFIRM)


def test_tool_args_cannot_choose_agent_or_reenable_conversation_memory(tmp_path, session):
    sibling = tmp_path / '.agents' / 'KindGrid'
    for target in (sibling / 'memory.md', session.conversation_directory(CID) / 'memory.md'):
        args = {'path': str(target), 'active_agent_memory_root': str(sibling),
                'active_conversation': str(target.parent), 'agent_id': AID}
        decision = policy.evaluate(policy.STRICT, policy.WRITE_POLICY, args, tmp_path,
                                   active_agent_memory_root=session.memory_root,
                                   active_conversation=session.conversation_directory(CID))
        assert policy.SELF_STORE not in decision.capabilities
        assert decision.action == policy.CONFIRM
    decision = policy.evaluate(policy.STRICT, policy.WRITE_POLICY,
                               {'path': str(session.memory_root / 'memory.md'),
                                'active_agent_memory_root': str(session.memory_root)}, tmp_path)
    assert policy.SELF_STORE not in decision.capabilities


def test_legacy_memory_root_unchanged_until_agent_context_is_bound(tmp_path):
    own = tmp_path / '.convos' / CID
    assert policy.SELF_STORE in set(policy.classify_write(
        {'path': str(own / 'handoff.md')}, tmp_path, active_conversation=own))
    assert policy.SELF_STORE not in set(policy.classify_write(
        {'path': str(own / 'settings.json')}, tmp_path, active_conversation=own))


@pytest.mark.parametrize('linked', ['memories', 'memory.md', 'root'])
def test_symlink_memory_escapes_never_get_exception(tmp_path, session, linked):
    root = session.memory_root
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'memory.md').write_text('unchanged')
    try:
        if linked == 'root':
            alias = tmp_path / 'alias'
            alias.symlink_to(root, target_is_directory=True)
            supplied_root = alias
            target = alias / 'memory.md'
        elif linked == 'memories':
            (root / 'memories').symlink_to(outside, target_is_directory=True)
            supplied_root = root
            target = root / 'memories' / 'memory.md'
        else:
            (root / 'memory.md').symlink_to(outside / 'memory.md')
            supplied_root = root
            target = root / 'memory.md'
    except OSError:
        pytest.skip('host cannot create symlinks')
    decision = policy.evaluate(policy.STRICT, policy.WRITE_POLICY, {'path': str(target)}, tmp_path,
                               active_agent_memory_root=supplied_root)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM
    assert (outside / 'memory.md').read_text() == 'unchanged'


def test_reparse_memory_path_fails_closed(tmp_path, session, monkeypatch):
    root = session.memory_root
    (root / 'memories').mkdir()
    original = Path.lstat
    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == root / 'memories':
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, 'lstat', reparse)
    decision = policy.evaluate(policy.STRICT, policy.WRITE_POLICY,
                               {'path': str(root / 'memories' / 'topic.md')}, tmp_path,
                               active_agent_memory_root=root)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM


def test_host_authorization_root_comes_from_owned_session_not_args():
    # Source contract until separately gated launcher lifecycle integration.
    from litetui.app import LiteTUI
    source = inspect.getsource(LiteTUI._authorize_action)
    assert 'active_agent_memory_root=(self._agent_session.memory_root' in source
    assert "args.get('active_agent_memory_root'" not in source
    assert 'args.get("active_agent_memory_root"' not in source


def test_hardlinked_settings_in_memory_tree_never_gets_exception(tmp_path, session):
    import os
    root = session.memory_root
    (root / 'memories').mkdir()
    alias = root / 'memories' / 'settings-alias.md'
    before = (root / 'settings.json').read_bytes()
    os.link(root / 'settings.json', alias)
    decision = policy.evaluate(policy.STRICT, policy.WRITE_POLICY, {'path': str(alias)}, tmp_path,
                               active_agent_memory_root=root)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM
    assert (root / 'settings.json').read_bytes() == before
