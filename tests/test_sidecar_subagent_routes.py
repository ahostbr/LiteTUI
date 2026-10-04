"""T0347 sidecar wire supports structured routes and conversation ownership."""
from types import SimpleNamespace

from litetui.settings_service import SettingsService
from litetui.sidecar_patch import apply_patch
from litetui.sidecar_settings import public_snapshot


def host(root):
    service = SettingsService(root)
    snapshot = service.create_conversation('one')
    return SimpleNamespace(settings=snapshot.effective, backend=SimpleNamespace(name='codex'),
                           convo_dir=root / '.convos' / 'one', _settings_service=service)


def test_sidecar_exposes_route_override_and_expert_flag_on_any_backend(tmp_path):
    service = SettingsService(tmp_path)
    for backend in ['codex', 'claude', 'lmstudio']:
        result = public_snapshot(service.snapshot('one'), backend=SimpleNamespace(name=backend, reasoning_levels=lambda model: []))
        for key, scope in [('subagent_route', 'device'), ('subagent_route_override', 'conversation'),
                           ('allow_local_subagents', 'device')]:
            assert result['fields'][key]['scope'] == scope
            assert result['fields'][key]['settings_control']
            assert result['fields'][key]['control']['editable']


def test_structured_route_patch_is_shared_and_override_stays_scoped(tmp_path):
    app = host(tmp_path)
    route = {'backend': 'claude', 'model': 'sonnet'}
    service = app._settings_service
    result = apply_patch(app, {'conversation_id': 'one', 'expected_revisions': service.snapshot('one').revisions,
                              'changes': [{'key': 'subagent_route', 'value': route, 'scope': 'device'}]})
    assert result['saved']
    assert service.snapshot('two').saved.subagent_route == route
    result = apply_patch(app, {'conversation_id': 'one', 'expected_revisions': service.snapshot('one').revisions,
                              'changes': [{'key': 'subagent_route_override', 'value': {}, 'scope': 'conversation'}]})
    assert result['saved']
    assert service.snapshot('one').saved.subagent_route_override == {}
    assert service.snapshot('two').saved.subagent_route_override is None


def test_same_revision_other_conversation_cannot_receive_old_draft(tmp_path):
    app = host(tmp_path)
    result = apply_patch(app, {'conversation_id': 'other', 'expected_revisions': app._settings_service.snapshot('one').revisions,
                              'changes': [{'key': 'subagent_route_override', 'value': {}, 'scope': 'conversation'}]})
    assert not result['saved']
    assert result['conflict']
    assert app._settings_service.snapshot('one').saved.subagent_route_override is None
