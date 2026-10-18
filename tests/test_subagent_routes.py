"""T0347 global route ownership, migration, live precedence and explicit resets."""
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from litetui.agent_launch_context import ordinary
from litetui.plugins import subagent_plugin as plugin
from litetui.settings import Settings
from litetui.settings_service import SettingChange, SettingsService
from litetui.subagent_routing import resolve_route


@pytest.fixture
def host():
    with ExitStack() as sessions:
        def create(root, name='one', backend='codex'):
            session = sessions.enter_context(ordinary(root, Settings(seat_name=name, backend=backend, default_model='parent')))
            service = SettingsService(root, agent_session=session)
            cid = session.initial_conversation_id
            snapshot = service.create_conversation(cid)
            return SimpleNamespace(
                settings=snapshot.effective, backend=SimpleNamespace(name=backend, models={'parent': {}, 'child': {}}),
                model_id='parent', convo_id=cid, convo_dir=session.conversation_directory(cid),
                _settings_service=service, _agent_session=session, system_message=Mock(),
            )
        yield create


def set_global(app, route):
    service = app._settings_service
    snap = service.snapshot(app.convo_id)
    return service.save_patch(app.convo_id, [SettingChange('subagent_route', route, 'device')], snap.revisions)


def test_live_global_route_and_reset_across_instances(tmp_path, host):
    first, second = host(tmp_path), host(tmp_path, 'two', 'claude')
    assert set_global(first, {'backend': 'codex', 'model': 'child'}).fully_saved
    assert resolve_route(second) == {'backend': 'codex', 'model': 'child'}
    assert set_global(first, None).fully_saved
    assert resolve_route(second) == {'backend': 'claude', 'model': 'parent'}


def test_conversation_override_wins_and_inherit_restores_global(tmp_path, host):
    first, second = host(tmp_path), host(tmp_path, 'two')
    set_global(first, {'backend': 'free', 'model': 'free-model'})
    plugin._cmd_subagent_set(second, '/subagent-set', 'codex child')
    assert resolve_route(second) == {'backend': 'codex', 'model': 'child'}
    assert resolve_route(first) == {'backend': 'free', 'model': 'free-model'}
    plugin._cmd_subagent_set(second, '/subagent-set', 'default')
    assert resolve_route(second) == {'backend': 'codex', 'model': 'parent'}
    plugin._cmd_subagent_set(second, '/subagent-set', 'inherit')
    assert resolve_route(second) == {'backend': 'free', 'model': 'free-model'}
    assert second.model_id == 'parent'


@pytest.mark.parametrize('materialized', [True, False])
def test_global_route_supersedes_legacy_model_but_not_explicit_choices(tmp_path, host, materialized):
    app = host(tmp_path)
    svc = app._settings_service
    svc.save_patch(app.convo_id, [SettingChange('subagent_model', 'child', 'conversation')], svc.snapshot(app.convo_id).revisions)
    if not materialized:
        app.convo_dir = None
        app.settings.subagent_model = 'child'
    assert resolve_route(app) == {'backend': 'codex', 'model': 'child'}
    set_global(app, {'backend': 'claude', 'model': 'other'})
    assert resolve_route(app) == {'backend': 'claude', 'model': 'other'}
    assert resolve_route(app, 'explicit') == {'backend': 'codex', 'model': 'explicit'}
    set_global(app, None)
    assert resolve_route(app) == {'backend': 'codex', 'model': 'parent'}


def test_new_conversation_does_not_copy_legacy_subagent_model(tmp_path, host):
    tmp_path.joinpath('settings.json').write_text(json.dumps({'subagent_model': 'legacy'}))
    app = host(tmp_path)
    raw = json.loads((app.convo_dir / 'settings.json').read_text())
    assert 'subagent_model' not in raw['execution']
    assert resolve_route(app) == {'backend': 'codex', 'model': 'legacy'}


@pytest.mark.parametrize('old,new', [('child', {'backend': 'codex', 'model': 'child'}), (None, None)])
def test_migration_reads_once_then_drops_codex_key(tmp_path, host, old, new):
    tmp_path.joinpath('settings.json').write_text(json.dumps({'codex_subagent_model': old, 'theme_name': 'monokai'}))
    app = host(tmp_path)
    assert app._settings_service.global_value('subagent_route') == (True, new)
    raw = json.loads(tmp_path.joinpath('settings.json').read_text())
    assert 'codex_subagent_model' not in raw
    assert raw['subagent_route'] == new
    assert raw['theme_name'] == 'monokai'


def test_existing_route_wins_migration_conflict(tmp_path):
    route = {'backend': 'claude', 'model': 'sonnet'}
    tmp_path.joinpath('settings.json').write_text(json.dumps({'codex_subagent_model': 'child', 'subagent_route': route}))
    service = SettingsService(tmp_path)
    assert service.snapshot('one').saved.subagent_route == route
    assert 'codex_subagent_model' not in json.loads(tmp_path.joinpath('settings.json').read_text())


@pytest.mark.parametrize('route', [[], {'backend': 'wat', 'model': 'x'}, {'backend': 'codex'},
                                   {'backend': 'codex', 'model': ''}, {'backend': 'codex', 'model': 'x', 'secret': 'x'}])
def test_invalid_route_refused_before_persistence(tmp_path, host, route):
    app = host(tmp_path)
    with pytest.raises(ValueError, match='route'):
        set_global(app, route)
    assert not tmp_path.joinpath('settings.json').exists()


@pytest.mark.parametrize('old', [False, 0, [], {}, ''])
def test_migration_invalid_legacy_keeps_bytes_and_reports_failure(tmp_path, old):
    from litetui.subagent_routing import migrate_global_file

    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'codex_subagent_model': old}))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='legacy'):
        migrate_global_file(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('backend', [[], {}, None, False, 1])
def test_route_backend_types_raise_valueerror(backend):
    from litetui.subagent_routing import validate_route

    with pytest.raises(ValueError, match='route'):
        validate_route({'backend': backend, 'model': 'child'})


def test_expert_local_flag_default_off_and_global(tmp_path, host):
    app = host(tmp_path)
    assert not app.settings.allow_local_subagents
    result = app._settings_service.save_patch(app.convo_id, [SettingChange('allow_local_subagents', True, 'device')],
                                             app._settings_service.snapshot(app.convo_id).revisions)
    assert result.fully_saved
    assert host(tmp_path, 'two').settings.allow_local_subagents
