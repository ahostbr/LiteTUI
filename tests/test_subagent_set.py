"""Codex's child default is shared; parent/local execution remains independent."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from litetui.plugins import subagent_plugin as plugin
from litetui.settings_service import SettingChange, SettingsService


def app(root, conversation='one'):
    service = SettingsService(root)
    snapshot = service.create_conversation(conversation)
    return SimpleNamespace(
        backend=SimpleNamespace(name='codex', remote=True, models={'parent': {}, 'child': {}}),
        settings=snapshot.effective, model_id='parent', convo_id=conversation,
        convo_dir=root / '.convos' / conversation, system_message=Mock(),
        _settings_service=service,
    )


def test_picker_lists_codex_models_and_global_follow_option(tmp_path, monkeypatch):
    host = app(tmp_path)
    pick = Mock()
    monkeypatch.setattr(plugin, 'pick', pick)
    plugin._cmd_subagent_set(host, '/subagent-set', '')
    args = pick.call_args.args
    assert [row[0] for row in args[2]] == ['__follow__', 'parent', 'child']
    assert 'global' in args[1].lower()
    assert host.model_id == 'parent'


def test_real_save_is_global_and_running_second_instance_adopts_and_clears(tmp_path):
    first, second = app(tmp_path), app(tmp_path, 'two')
    before = [host.convo_dir.joinpath('settings.json').read_bytes() for host in (first, second)]
    assert plugin._resolve_model(second, '') == 'parent'
    plugin._cmd_subagent_set(first, '/subagent-set', 'child')
    assert first.settings.codex_subagent_model == 'child'
    assert second._settings_service.snapshot('two').saved.codex_subagent_model == 'child'
    assert plugin._resolve_model(second, '') == 'child'  # no restart/reconnect
    assert app(tmp_path, 'three').settings.codex_subagent_model == 'child'
    assert first.model_id == second.model_id == 'parent'
    assert [host.convo_dir.joinpath('settings.json').read_bytes() for host in (first, second)] == before
    plugin._cmd_subagent_set(first, '/subagent-set', 'default')
    assert plugin._resolve_model(second, '') == 'parent'
    raw = json.loads(tmp_path.joinpath('settings.json').read_text())
    assert raw['codex_subagent_model'] is None
    assert first.settings.subagent_model is None


def test_global_null_overrides_legacy_but_unset_global_preserves_it(tmp_path):
    host = app(tmp_path)
    service = host._settings_service
    service.save_patch('one', [SettingChange('subagent_model', 'child', 'conversation')],
                       service.snapshot('one').revisions)
    host.settings = service.snapshot('one').effective
    assert plugin._resolve_model(host, '') == 'child'
    plugin._cmd_subagent_set(host, '/subagent-set', 'default')
    assert plugin._resolve_model(host, '') == 'parent'
    assert host.settings.subagent_model == 'child'  # legacy/local record untouched
    assert service.snapshot('one').saved.subagent_model == 'child'


def test_global_codex_choice_does_not_change_local_routing(tmp_path):
    first, local = app(tmp_path), app(tmp_path, 'two')
    local.backend = SimpleNamespace(name='lmstudio', remote=False,
                                    loaded_models=lambda: {'parent', 'child'})
    local.settings.subagent_model = 'parent'
    plugin._cmd_subagent_set(first, '/subagent-set', 'child')
    assert plugin._resolve_model(local, '') == 'parent'
    assert plugin._resolve_model(first, 'parent') == 'parent'  # explicit wins


def test_failed_save_does_not_change_default(tmp_path, monkeypatch):
    from litetui import settings_service
    host = app(tmp_path)
    monkeypatch.setattr(settings_service, '_write', Mock(side_effect=OSError('disk full')))
    plugin._cmd_subagent_set(host, '/subagent-set', 'child')
    assert host.settings.codex_subagent_model is None
    assert 'disk full' in host.system_message.call_args.args[0]
    assert plugin._resolve_model(host, '') == 'parent'


def test_stale_picker_does_not_save_into_new_conversation(tmp_path, monkeypatch):
    host = app(tmp_path)
    pick = Mock()
    monkeypatch.setattr(plugin, 'pick', pick)
    plugin._cmd_subagent_set(host, '/subagent-set', '')
    host.convo_id = 'two'
    pick.call_args.args[3]('child')
    assert not tmp_path.joinpath('settings.json').exists()


def test_non_codex_and_unknown_model_refuse(tmp_path):
    host = app(tmp_path)
    plugin._cmd_subagent_set(host, '/subagent-set', 'missing')
    host.backend.name = 'lmstudio'
    plugin._cmd_subagent_set(host, '/subagent-set', 'child')
    assert not tmp_path.joinpath('settings.json').exists()


def test_reset_when_already_following_still_publishes_global_null(tmp_path):
    host = app(tmp_path)
    plugin._cmd_subagent_set(host, '/subagent-set', 'default')
    assert json.loads(tmp_path.joinpath('settings.json').read_text())['codex_subagent_model'] is None


def test_global_default_can_be_saved_before_conversation_exists(tmp_path):
    host = app(tmp_path)
    host.convo_dir = None
    plugin._cmd_subagent_set(host, '/subagent-set', 'child')
    assert host._settings_service.snapshot('other').saved.codex_subagent_model == 'child'


def test_corrupt_global_settings_are_visible_tool_failure(tmp_path):
    host = app(tmp_path)
    tmp_path.joinpath('settings.json').write_text('{broken')
    result = plugin._make_runner(host)({'prompt': 'hello'})
    assert '[error]' in result
    assert 'JSONDecodeError' in result


def test_missing_global_model_falls_back_without_changing_parent(tmp_path):
    first, second = app(tmp_path), app(tmp_path, 'two')
    plugin._cmd_subagent_set(first, '/subagent-set', 'child')
    second.backend.models = {'parent': {}}
    assert plugin._resolve_model(second, '') == 'parent'
    assert second.model_id == 'parent'


def _running_instance(root, connection):
    try:
        host = app(root, 'second-process')
        connection.send(plugin._resolve_model(host, ''))
        while connection.recv() == 'resolve':
            connection.send(plugin._resolve_model(host, ''))
    finally:
        connection.close()


def test_already_running_os_process_adopts_global_change_without_restart(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_running_instance, args=(tmp_path, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15), 'second process did not become ready'
        assert parent.recv() == 'parent'
        host = app(tmp_path)
        for choice, expected in [('child', 'child'), ('default', 'parent')]:
            plugin._cmd_subagent_set(host, '/subagent-set', choice)
            parent.send('resolve')
            assert parent.poll(15), 'second process did not resolve the new default'
            assert parent.recv() == expected
        parent.send('stop')
        process.join(15)
        assert process.exitcode == 0
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_registry_keeps_codex_global_and_local_conversation():
    from litetui.settings_scope import SETTING_SPECS, SettingScope
    assert SETTING_SPECS['codex_subagent_model'].scope == SettingScope.DEVICE
    assert SETTING_SPECS['subagent_model'].scope == SettingScope.CONVERSATION


def test_global_lookup_validates_setting_name(tmp_path):
    with pytest.raises(ValueError):
        SettingsService(tmp_path).global_value('subagent_model')
