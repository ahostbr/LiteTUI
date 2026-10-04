"""Route commands distinguish global default, override, follow and inherit."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

from litetui.plugins import subagent_plugin as plugin
from litetui.settings_service import SettingsService
from litetui.subagent_routing import resolve_route


def app(root, conversation='one', backend='codex'):
    service = SettingsService(root)
    snapshot = service.create_conversation(conversation)
    return SimpleNamespace(
        backend=SimpleNamespace(name=backend, remote=True, models={'parent': {}, 'child': {}}),
        settings=snapshot.effective, model_id='parent', convo_id=conversation,
        convo_dir=root / '.convos' / conversation, system_message=Mock(), _settings_service=service,
    )


def test_picker_describes_scope_and_inherit(tmp_path, monkeypatch):
    host = app(tmp_path)
    pick = Mock()
    monkeypatch.setattr(plugin, 'pick', pick)
    plugin._cmd_subagent_set(host, '/subagent-set', '')
    args = pick.call_args.args
    assert [row[0] for row in args[2]] == ['inherit', 'default', 'codex parent', 'codex child']
    assert 'conversation' in args[1]


def test_global_command_does_not_write_conversation(tmp_path):
    first, second = app(tmp_path), app(tmp_path, 'two', 'claude')
    before = first.convo_dir.joinpath('settings.json').read_bytes()
    plugin._cmd_subagent_global(first, '/subagent-global', 'codex child')
    assert resolve_route(second) == {'backend': 'codex', 'model': 'child'}
    assert first.convo_dir.joinpath('settings.json').read_bytes() == before
    plugin._cmd_subagent_global(first, '/subagent-global', 'default')
    assert resolve_route(second) == {'backend': 'claude', 'model': 'parent'}
    assert json.loads(tmp_path.joinpath('settings.json').read_text())['subagent_route'] is None


def test_failed_save_keeps_route_and_reports_error(tmp_path, monkeypatch):
    from litetui import settings_service
    host = app(tmp_path)
    monkeypatch.setattr(settings_service, '_write', Mock(side_effect=OSError('disk full')))
    plugin._cmd_subagent_global(host, '/subagent-global', 'codex child')
    assert host.settings.subagent_route is None
    assert 'disk full' in host.system_message.call_args.args[0]


def test_stale_picker_refuses(tmp_path, monkeypatch):
    host = app(tmp_path)
    pick = Mock()
    monkeypatch.setattr(plugin, 'pick', pick)
    plugin._cmd_subagent_set(host, '/subagent-set', '')
    host.convo_dir = tmp_path / '.convos' / 'other'
    pick.call_args.args[3]('codex child')
    assert 'changed' in host.system_message.call_args.args[0]


def _running_instance(root, connection):
    try:
        host = app(root, 'second-process', 'claude')
        connection.send(resolve_route(host))
        while connection.recv() == 'resolve':
            connection.send(resolve_route(host))
    finally:
        connection.close()


def test_running_os_process_adopts_global_without_restart(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_running_instance, args=(tmp_path, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15)
        assert parent.recv() == {'backend': 'claude', 'model': 'parent'}
        host = app(tmp_path)
        for choice, expected in [('codex child', {'backend': 'codex', 'model': 'child'}),
                                 ('default', {'backend': 'claude', 'model': 'parent'})]:
            plugin._cmd_subagent_global(host, '/subagent-global', choice)
            parent.send('resolve')
            assert parent.poll(15)
            assert parent.recv() == expected
        parent.send('stop')
        process.join(15)
        assert process.exitcode == 0
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(5)
