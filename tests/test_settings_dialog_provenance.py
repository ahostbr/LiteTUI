from types import SimpleNamespace
from unittest.mock import Mock

from litetui.plugins import settings_ui
from litetui.settings_service import SettingsService, SettingChange


def test_stale_dialog_cannot_save_or_apply_to_different_conversation(tmp_path, monkeypatch):
    service = SettingsService(tmp_path)
    first = service.create_conversation('a')
    service.create_conversation('b')
    app = SimpleNamespace(convo_dir=tmp_path / '.convos' / 'a',
                          settings=first.effective, _settings_service=service,
                          available_models=[], backend=SimpleNamespace(remote=True))
    present = Mock()
    monkeypatch.setattr(settings_ui, 'present_dialog', present)
    monkeypatch.setattr(settings_ui.model_residency, 'resident_models', lambda app: (set(), True))
    settings_ui._cmd_settings(app, '/settings', '')
    bindings = present.call_args.args[1].keywords
    before = (app.convo_dir / 'settings.json').read_bytes()
    app.convo_dir = tmp_path / '.convos' / 'b'
    app.settings = service.snapshot('b').effective
    result = bindings['save_patch']((SettingChange('subagent_model', 'child', 'conversation'),), first.revisions)
    assert not result.fully_saved
    assert (tmp_path / '.convos' / 'a' / 'settings.json').read_bytes() == before
    requested = service.snapshot('a').effective
    requested.subagent_model = 'child'
    from litetui.settings_apply import SettingsSaveResult, PersistenceDestinationResult
    success = SettingsSaveResult((PersistenceDestinationResult('a', 'conversation', True, fields=('subagent_model',)),))
    result = bindings['runtime_apply'](requested, success)
    assert result.has_runtime_failures
    assert app.settings.subagent_model is None
    assert service.snapshot('b').saved.subagent_model is None
