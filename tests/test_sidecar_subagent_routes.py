"""T0347 sidecar wire supports structured routes and conversation ownership."""
from types import SimpleNamespace

from _owned_seat import conversation_id, owned_session
from litetui.settings_service import SettingsService
from litetui.sidecar_patch import apply_patch
from litetui.sidecar_settings import public_snapshot

# Owned conversation folders are UUIDs (agent_store.valid_id).
ONE, TWO, OTHER = (conversation_id(name) for name in ('one', 'two', 'other'))


def host(root):
    """A real owned seat: a conversation write is refused without one (T0332, owned_storage.py)."""
    session = owned_session(root)
    service = SettingsService(root, agent_session=session)
    snapshot = service.create_conversation(ONE)
    return SimpleNamespace(settings=snapshot.effective, backend=SimpleNamespace(name='codex'),
                           convo_dir=session.conversation_directory(ONE), _agent_session=session,
                           _settings_service=service)


def test_sidecar_exposes_route_override_and_expert_flag_on_any_backend(tmp_path):
    service = SettingsService(tmp_path)
    for backend in ['codex', 'claude', 'lmstudio']:
        result = public_snapshot(service.snapshot('one'), backend=SimpleNamespace(name=backend, reasoning_levels=lambda model: []))
        for key, scope in [('subagent_route', 'device'), ('subagent_route_override', 'conversation'),
                           ('allow_local_subagents', 'device')]:
            assert result['fields'][key]['scope'] == scope
            assert result['fields'][key]['settings_control']
            assert result['fields'][key]['control']['editable']
            assert result['ui']['fields'][key]['description']
            assert any(key in section['fields'] for section in result['ui']['sections'])


def test_structured_route_patch_is_shared_and_override_stays_scoped(tmp_path):
    app = host(tmp_path)
    route = {'backend': 'claude', 'model': 'sonnet'}
    service = app._settings_service
    result = apply_patch(app, {'conversation_id': ONE, 'expected_revisions': service.snapshot(ONE).revisions,
                              'changes': [{'key': 'subagent_route', 'value': route, 'scope': 'device'}]})
    assert result['saved']
    assert service.snapshot(TWO).saved.subagent_route == route
    result = apply_patch(app, {'conversation_id': ONE, 'expected_revisions': service.snapshot(ONE).revisions,
                              'changes': [{'key': 'subagent_route_override', 'value': {}, 'scope': 'conversation'}]})
    assert result['saved']
    assert service.snapshot(ONE).saved.subagent_route_override == {}
    assert service.snapshot(TWO).saved.subagent_route_override is None


def test_same_revision_other_conversation_cannot_receive_old_draft(tmp_path):
    app = host(tmp_path)
    result = apply_patch(app, {'conversation_id': OTHER, 'expected_revisions': app._settings_service.snapshot(ONE).revisions,
                              'changes': [{'key': 'subagent_route_override', 'value': {}, 'scope': 'conversation'}]})
    assert not result['saved']
    assert result['conflict']
    assert app._settings_service.snapshot(ONE).saved.subagent_route_override is None
