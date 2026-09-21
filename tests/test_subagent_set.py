from types import SimpleNamespace
from unittest.mock import Mock

from litetui.plugins import subagent_plugin as plugin
from litetui.settings import Settings


def app():
    return SimpleNamespace(backend=SimpleNamespace(name="codex", models={"parent": {}, "child": {}}),
                           settings=Settings(), model_id="parent", convo_id="one",
                           convo_dir="one", system_message=Mock())


def test_picker_lists_codex_models_and_follow_option(monkeypatch):
    host = app()
    pick = Mock()
    monkeypatch.setattr(plugin, "pick", pick)
    plugin._cmd_subagent_set(host, "/subagent-set", "")
    args = pick.call_args.args
    assert [row[0] for row in args[2]] == ["__follow__", "parent", "child"]
    assert host.model_id == "parent"


def test_selection_persists_only_child_default(monkeypatch):
    host = app()
    save = Mock()
    monkeypatch.setattr(plugin.settings_runtime, "persist_or_raise", save)
    monkeypatch.setattr(plugin.settings_runtime, "apply_saved_result",
                        lambda app, requested, result: setattr(app, "settings", requested))
    plugin._cmd_subagent_set(host, "/subagent-set", "child")
    assert save.call_args.args[1].subagent_model == "child"
    assert host.settings.subagent_model == "child"
    assert host.model_id == "parent"


def test_failed_save_does_not_change_default(monkeypatch):
    host = app()
    monkeypatch.setattr(plugin.settings_runtime, "persist_or_raise", Mock(side_effect=OSError("disk full")))
    plugin._cmd_subagent_set(host, "/subagent-set", "child")
    assert host.settings.subagent_model is None
    assert "disk full" in host.system_message.call_args.args[0]


def test_stale_picker_does_not_save_into_new_conversation(monkeypatch):
    host = app()
    pick, save = Mock(), Mock()
    monkeypatch.setattr(plugin, "pick", pick)
    monkeypatch.setattr(plugin.settings_runtime, "persist_or_raise", save)
    plugin._cmd_subagent_set(host, "/subagent-set", "")
    host.convo_id = "two"
    pick.call_args.args[3]("child")
    save.assert_not_called()


def test_non_codex_and_unknown_model_refuse(monkeypatch):
    host = app()
    save = Mock()
    monkeypatch.setattr(plugin.settings_runtime, "persist_or_raise", save)
    plugin._cmd_subagent_set(host, "/subagent-set", "missing")
    host.backend.name = "lmstudio"
    plugin._cmd_subagent_set(host, "/subagent-set", "child")
    save.assert_not_called()


def test_real_save_is_conversation_scoped_and_clearable(tmp_path):
    from litetui.settings_service import SettingsService
    service = SettingsService(tmp_path)
    first = service.create_conversation('one')
    service.create_conversation('two')
    host = app()
    host.convo_dir = tmp_path / '.convos' / 'one'
    host.settings = first.effective
    host._settings_service = service
    plugin._cmd_subagent_set(host, '/subagent-set', 'child')
    assert service.snapshot('one').saved.subagent_model == 'child'
    assert host.settings.subagent_model == 'child'
    assert service.snapshot('two').saved.subagent_model is None
    assert host.model_id == 'parent'
    plugin._cmd_subagent_set(host, '/subagent-set', 'default')
    assert service.snapshot('one').saved.subagent_model is None
    assert host.settings.subagent_model is None
