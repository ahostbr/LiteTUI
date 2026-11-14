from types import SimpleNamespace
from unittest.mock import Mock

from _owned_seat import conversation_id, owned_session

from litetui.plugins import settings_ui
from litetui.settings_service import SettingsService, SettingChange


def test_stale_dialog_cannot_save_or_apply_to_different_conversation(tmp_path, monkeypatch):
    session = owned_session(tmp_path)
    service = SettingsService(tmp_path, agent_session=session)
    A, B = conversation_id('a'), conversation_id('b')
    first = service.create_conversation(A)
    service.create_conversation(B)
    app = SimpleNamespace(convo_dir=session.conversation_directory(A),
                          settings=first.effective, _settings_service=service, _agent_session=session,
                          available_models=[], backend=SimpleNamespace(remote=True))
    present = Mock()
    monkeypatch.setattr(settings_ui, 'present_dialog', present)
    monkeypatch.setattr(settings_ui.model_residency, 'resident_models', lambda app: (set(), True))
    settings_ui._cmd_settings(app, '/settings', '')
    bindings = present.call_args.args[1].keywords
    before = (app.convo_dir / 'settings.json').read_bytes()
    app.convo_dir = session.conversation_directory(B)
    app.settings = service.snapshot(B).effective
    result = bindings['save_patch']((SettingChange('subagent_model', 'child', 'conversation'),), first.revisions)
    assert not result.fully_saved
    assert (session.conversation_directory(A) / 'settings.json').read_bytes() == before
    requested = service.snapshot(A).effective
    requested.subagent_model = 'child'
    from litetui.settings_apply import SettingsSaveResult, PersistenceDestinationResult
    success = SettingsSaveResult((PersistenceDestinationResult(A, 'conversation', True, fields=('subagent_model',)),))
    result = bindings['runtime_apply'](requested, success)
    assert result.has_runtime_failures
    assert app.settings.subagent_model is None
    assert service.snapshot(B).saved.subagent_model is None
