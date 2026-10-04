"""T0308 owned official registration adapter, no real registry/CLI launch."""
import json
import os
from types import SimpleNamespace

import pytest

from litetui import agent_ownership, agent_store, harness

AID = '11111111-1111-4111-8111-111111111111'


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.delenv(harness.NO_HARNESS_ENV, raising=False)
    directory = tmp_path / '.agents' / 'Quiet Helm, Test'
    directory.mkdir(parents=True)
    (directory / 'settings.json').write_text(json.dumps({
        'schema_version': 1, 'name': directory.name, 'agent_id': AID,
        'execution': {'backend': 'codex', 'model': 'fixture', 'thinking_level': 'high'},
    }), encoding='utf-8')
    with agent_ownership.AgentSession.acquire_existing(agent_store.AgentStore(tmp_path), agent_id=AID) as session:
        yield session


def receipt(session):
    authority = session.authority
    return {'agent_id': authority.agent_id, 'name': authority.name,
            'backend': authority.backend, 'model': authority.model,
            'thinking_level': authority.thinking_level, 'session_pid': os.getpid()}


def seat(session):
    return harness.Seat(AID, session.authority.name, 'poison', agent_session=session)


def test_exact_structured_receipt_preserves_delimiter_name_and_execution(owned, monkeypatch):
    calls = []
    def official(argv, **kwargs):
        calls.append(argv)
        assert '--strict-identity' in argv and '--takeover' not in argv
        for flag, value in (('--agent-id', AID), ('--name', owned.authority.name),
                            ('--backend', 'codex'), ('--model', 'fixture'),
                            ('--thinking-level', 'high'), ('--session-pid', str(os.getpid()))):
            assert argv[argv.index(flag) + 1] == value
        with pytest.raises(agent_ownership.OwnershipError):
            agent_ownership.AgentSession.acquire_existing(owned.store, agent_id=AID)
        return SimpleNamespace(returncode=0, stdout='Registered name=WRONG\nFolder-owned identity: '
                               + json.dumps(receipt(owned)), stderr='')
    monkeypatch.setattr(harness, '_cli', official)
    instance = seat(owned)
    assert instance.register()
    instance.model = 'registry-poison'
    instance.backend = 'claude'
    assert instance.heartbeat()
    assert instance.name == 'Quiet Helm, Test'
    assert (instance.model, instance.backend, instance.thinking_level) == ('fixture', 'codex', 'high')
    assert not instance.claim_name('RenameFromConversation')
    assert not instance.refresh_name()
    assert len(calls) == 2


@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'malformed', 'wrong_name',
                                  'wrong_id', 'wrong_backend', 'wrong_pid', 'pid_string', 'nonzero'])
def test_missing_or_mismatched_receipt_never_registers(owned, monkeypatch, kind):
    row = receipt(owned)
    changes = {'wrong_name': ('name', 'Other'), 'wrong_id': ('agent_id', str(os.getpid())),
               'wrong_backend': ('backend', 'claude'), 'wrong_pid': ('session_pid', os.getpid()+1),
               'pid_string': ('session_pid', str(os.getpid()))}
    if kind in changes:
        key, value = changes[kind]
        row[key] = value
    output = 'Folder-owned identity: ' + json.dumps(row)
    if kind == 'missing':
        output = 'Registered name=Quiet Helm, Test'
    elif kind == 'duplicate':
        output += '\n' + output
    elif kind == 'malformed':
        output = 'Folder-owned identity: {bad'
    calls = []
    def official(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1 if kind == 'nonzero' else 0, stdout=output, stderr='')
    monkeypatch.setattr(harness, '_cli', official)
    instance = seat(owned)
    instance.registered = True  # stale prior success must not survive strict failure
    assert not instance.register()
    assert not instance.registered
    assert instance.name == owned.authority.name
    assert len(calls) == 1  # no suffix attempts


def test_owned_heartbeat_preserves_adopted_parent_and_strict_receipt(owned, monkeypatch):
    calls = []
    current_parent = {'id': 'launch-parent'}
    valid_receipt = {'ok': True}
    def official(argv, **kwargs):
        calls.append(argv)
        assert '--strict-identity' in argv and '--takeover' not in argv
        if '--spawned-by' in argv:
            current_parent['id'] = argv[argv.index('--spawned-by') + 1]
        row = receipt(owned)
        if not valid_receipt['ok']:
            row['agent_id'] = 'wrong-folder-identity'
        return SimpleNamespace(returncode=0, stdout='Folder-owned identity: '
                               + json.dumps(row), stderr='')
    monkeypatch.setattr(harness, '_cli', official)
    instance = seat(owned)
    instance.spawned_by = 'launch-parent'
    assert instance.register()
    assert calls[-1][calls[-1].index('--spawned-by') + 1] == 'launch-parent'
    current_parent['id'] = 'adopted-parent'
    assert instance.heartbeat()
    assert '--spawned-by' not in calls[-1]
    assert current_parent['id'] == 'adopted-parent'
    assert instance.name == owned.authority.name
    # Explicit registration/rebind retains the helper's original default.
    result, name = instance._register_as(owned.authority.name)
    assert result.returncode == 0 and name == owned.authority.name
    assert calls[-1][calls[-1].index('--spawned-by') + 1] == 'launch-parent'
    current_parent['id'] = 'adopted-parent'
    valid_receipt['ok'] = False
    assert not instance.heartbeat()
    assert not instance.registered
    assert '--spawned-by' not in calls[-1]
    assert current_parent['id'] == 'adopted-parent'


def test_released_child_capability_cannot_register(owned, monkeypatch):
    instance = seat(owned)
    owned.release()
    monkeypatch.setattr(harness, '_cli', lambda *a, **k: pytest.fail('must not call official CLI'))
    assert not instance.register()
    assert not instance.registered


def test_disabled_owned_heartbeat_clears_stale_registration(owned, monkeypatch):
    instance = seat(owned)
    instance.registered = True
    monkeypatch.setenv(harness.NO_HARNESS_ENV, '1')
    monkeypatch.setattr(harness, '_cli', lambda *a, **k: pytest.fail('disabled must not invoke CLI'))
    assert not instance.heartbeat()
    assert not instance.registered
