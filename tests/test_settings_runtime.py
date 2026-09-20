"""Scoped app persistence adapter without starting a terminal or model."""
from types import SimpleNamespace
from litetui import settings as st
from litetui import convo_settings as cs


def test_runtime_adapter_never_persists_execution_to_global(tmp_path):
    from litetui.settings_runtime import persist_settings
    from litetui.settings_service import SettingsService
    service = SettingsService(tmp_path)
    service.create_conversation('a')
    app = SimpleNamespace(convo_dir=tmp_path / '.convos' / 'a',
                          settings=service.snapshot('a').effective,
                          _settings_service=service)
    app.settings.backend = 'codex'
    result = persist_settings(app, app.settings)
    assert result.fully_saved
    assert service.snapshot('a').saved.backend == 'codex'
    assert not st.settings_path(tmp_path).exists()
    assert cs.load(app.convo_dir).backend == 'codex'


def test_runtime_adapter_theme_save_does_not_leak_environment(tmp_path, monkeypatch):
    from litetui.settings_runtime import persist_settings
    from litetui.settings_service import SettingsService
    monkeypatch.setenv('LITETUI_BACKEND', 'ninfer')
    service = SettingsService(tmp_path)
    service.create_conversation('a')
    app = SimpleNamespace(convo_dir=tmp_path / '.convos' / 'a',
                          settings=service.snapshot('a').effective,
                          _settings_service=service)
    app.settings.theme_name = 'monokai'
    assert persist_settings(app, app.settings).fully_saved
    assert service.snapshot('a').saved.backend == 'lmstudio'
    assert service.snapshot('a').saved.theme_name == 'monokai'


def test_command_patch_does_not_revert_unrelated_device_change(tmp_path):
    from litetui.settings_runtime import persist_settings
    from litetui.settings_service import SettingsService, SettingChange
    svc = SettingsService(tmp_path)
    snap = svc.create_conversation('a')
    app = SimpleNamespace(convo_dir=tmp_path / '.convos' / 'a', settings=snap.effective,
                          _settings_service=svc)
    svc.save_patch('b', [SettingChange('theme_name', 'monokai', 'device')], svc.snapshot('b').revisions)
    app.settings.backend = 'codex'
    persist_settings(app, app.settings)
    assert svc.snapshot('a').saved.theme_name == 'monokai'


def test_real_settings_callback_persists_backend_only_in_conversation(tmp_path, monkeypatch):
    from litetui.app import LiteTUI
    from litetui.settings_service import SettingsService
    from copy import deepcopy
    svc = SettingsService(tmp_path)
    snap = svc.create_conversation('a')
    class App:
        _on_settings_saved = LiteTUI._on_settings_saved
        settings = snap.effective
        convo_dir = tmp_path / '.convos' / 'a'
        _settings_service = svc
        backend = SimpleNamespace(name='lmstudio')
        theme = settings.theme_name
        def _refresh_prompt_controls(self): pass
        def _next_follow_generation(self): pass
        def _scroll_down(self): pass
        def _bind_mic_hotkey(self): pass
        def _register_custom_themes(self): pass
        def _update_header(self): pass
        def _refresh_ctx_label(self): pass
        def apply_backend_change(self, choice): self.backend.name = choice
        def _system(self, text): pass
    app = App()
    new = deepcopy(app.settings)
    new.backend = 'codex'
    monkeypatch.setattr(st, 'save', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('global save bypass')))
    app._on_settings_saved(new)
    assert svc.snapshot('a').saved.backend == 'codex'


def test_runtime_result_distinguishes_saved_from_effective(tmp_path):
    from litetui.settings_runtime import apply_saved_result
    from litetui.settings_service import SettingsService, SettingChange
    svc = SettingsService(tmp_path)
    snap = svc.create_conversation('a')
    app = SimpleNamespace(settings=snap.effective, convo_dir=tmp_path / '.convos' / 'a',
                          _settings_service=svc)
    result = svc.save_patch('a', [SettingChange('ninfer_max_context', 16384, 'conversation')], snap.revisions)
    target = svc.snapshot('a').saved
    result = apply_saved_result(app, target, result)
    assert result.fully_saved
    assert result.runtime[0].field == 'ninfer_max_context'
    assert result.runtime[0].status == 'pending'
    assert result.runtime[0].action == 'restart'
    assert result.runtime[0].requested == 16384
    assert result.runtime[0].effective == 32768


def test_saved_environment_field_stays_effective_override(tmp_path, monkeypatch):
    from litetui.settings_runtime import apply_saved_result
    from litetui.settings_service import SettingsService, SettingChange
    monkeypatch.setenv('LITETUI_THINKING', 'high')
    svc = SettingsService(tmp_path)
    snap = svc.create_conversation('a')
    app = SimpleNamespace(settings=snap.effective, convo_dir=tmp_path / '.convos' / 'a', _settings_service=svc)
    result = svc.save_patch('a', [SettingChange('thinking_level', 'low', 'conversation')], snap.revisions)
    result = apply_saved_result(app, svc.snapshot('a').saved, result)
    assert app.settings.thinking_level == 'high'
    assert result.runtime[0].requested == 'low'
    assert result.runtime[0].effective == 'high'
    assert result.runtime[0].status == 'pending'


def test_settings_plugin_supplies_scoped_bindings(tmp_path, monkeypatch):
    from litetui.plugins import settings_ui
    from litetui.settings_service import SettingsService
    svc = SettingsService(tmp_path)
    snap = svc.create_conversation('a')
    captured = {}
    app = SimpleNamespace(settings=snap.effective, convo_dir=tmp_path / '.convos' / 'a',
                          _settings_service=svc, available_models=[],
                          _on_settings_saved=lambda value: None)
    monkeypatch.setattr(settings_ui, 'mcp_server_names', lambda a: [])
    monkeypatch.setattr(settings_ui.model_residency, 'resident_models', lambda a: ([], False))
    monkeypatch.setattr(settings_ui, 'present_dialog', lambda a, body, screen, callback: captured.update(body=body, screen=screen))
    settings_ui._cmd_settings(app, 'settings', '')
    assert callable(captured['body'].keywords['snapshot_provider'])
    assert callable(captured['screen'].keywords['save_patch'])
    assert callable(captured['screen'].keywords['runtime_apply'])


def test_real_ui_adapter_can_save_twice_with_service_revisions(tmp_path):
    from copy import deepcopy
    from litetui.settings_ui_adapter import SettingsUiAdapter
    from litetui.settings_service import SettingsService
    svc = SettingsService(tmp_path)
    snap = svc.create_conversation('a')
    adapter = SettingsUiAdapter(snap.effective, snapshot_provider=lambda: svc.snapshot('a'),
                                save_patch=lambda changes, rev: svc.save_patch('a', changes, rev))
    target = deepcopy(snap.effective)
    target.temperature = 0.2
    assert adapter.save(target).fully_saved
    target.temperature = 0.4
    assert adapter.save(target).fully_saved


def test_runtime_failure_preserves_saved_evidence_and_old_effective(tmp_path):
    from litetui.settings_runtime import apply_saved_result
    from litetui.settings_service import SettingsService, SettingChange
    svc = SettingsService(tmp_path)
    snap = svc.create_conversation('a')
    class App:
        settings = snap.effective
        convo_dir = tmp_path / '.convos' / 'a'
        @property
        def theme(self): return 'textual-dark'
        @theme.setter
        def theme(self, value): raise RuntimeError('injected runtime failure')
    app = App()
    result = svc.save_patch('a', [SettingChange('theme_name', 'monokai', 'device')], snap.revisions)
    result = apply_saved_result(app, svc.snapshot('a').saved, result)
    assert result.fully_saved
    assert result.runtime[0].status == 'failed'
    assert app.settings.theme_name == 'textual-dark'
    assert svc.snapshot('a').saved.theme_name == 'monokai'


def test_pending_runtime_is_retained_when_user_saves_again(tmp_path):
    from copy import deepcopy
    from litetui.settings_runtime import apply_saved_result
    from litetui.settings_ui_adapter import SettingsUiAdapter
    from litetui.settings_service import SettingsService
    svc = SettingsService(tmp_path)
    snap = svc.create_conversation('a')
    app = SimpleNamespace(settings=snap.effective, convo_dir=tmp_path / '.convos' / 'a', _settings_service=svc)
    adapter = SettingsUiAdapter(snap.effective, snapshot_provider=lambda: svc.snapshot('a'),
             save_patch=lambda changes, rev: svc.save_patch('a', changes, rev),
             runtime_apply=lambda target, result: apply_saved_result(app, target, result))
    target = deepcopy(snap.effective)
    target.ninfer_max_context = 16384
    first = adapter.save(target)
    assert first.has_pending_runtime
    second = adapter.save(target)
    assert second.has_pending_runtime


def test_reconnect_prepares_saved_endpoint_without_reverting_pending_restart(tmp_path):
    from litetui.settings_runtime import prepare_reconnect
    from litetui.settings_service import SettingsService, SettingChange
    svc = SettingsService(tmp_path)
    snap = svc.create_conversation('a')
    app = SimpleNamespace(settings=snap.effective, convo_dir=tmp_path / '.convos' / 'a', _settings_service=svc)
    svc.save_patch('a', [SettingChange('lm_host', 'http://localhost:9999', 'conversation'),
                         SettingChange('ninfer_max_context', 16384, 'conversation')], snap.revisions)
    prepare_reconnect(app)
    assert app.settings.lm_host == 'http://localhost:9999'
    assert app.settings.ninfer_max_context == 32768


def test_reconnect_factory_failure_preserves_effective_backend(tmp_path, monkeypatch):
    from litetui.settings_runtime import prepare_reconnect
    from litetui.settings_service import SettingsService, SettingChange
    from litetui import llm_backend
    import pytest
    svc = SettingsService(tmp_path)
    snap = svc.create_conversation('a')
    old_backend = SimpleNamespace(name='lmstudio')
    app = SimpleNamespace(settings=snap.effective, convo_dir=tmp_path / '.convos' / 'a',
                          _settings_service=svc, backend=old_backend)
    svc.save_patch('a', [SettingChange('backend', 'codex', 'conversation')], snap.revisions)
    monkeypatch.delenv('LITETUI_BACKEND', raising=False)
    def fail(settings): raise llm_backend.BackendError('injected unavailable provider')
    monkeypatch.setattr(llm_backend, 'make_backend', fail)
    with pytest.raises(llm_backend.BackendError):
        prepare_reconnect(app)
    assert app.backend is old_backend
    assert app.settings.backend == 'lmstudio'
    assert svc.snapshot('a').saved.backend == 'codex'


def test_saved_voice_off_stops_current_speech_and_refreshes_controls(tmp_path, monkeypatch):
    from litetui.settings_runtime import apply_saved_result
    from litetui.settings_service import SettingsService, SettingChange
    from litetui import voice_backend
    from copy import deepcopy
    svc = SettingsService(tmp_path)
    snap = svc.create_conversation('voice')
    called = []
    app = SimpleNamespace(settings=deepcopy(snap.effective), _refresh_prompt_controls=lambda: called.append('refresh'))
    app.settings.tts_enabled = True
    requested = deepcopy(app.settings)
    requested.tts_enabled = False
    result = svc.save_patch('voice', [SettingChange('tts_enabled', False, 'device')], snap.revisions)
    monkeypatch.setattr(voice_backend, 'stop', lambda: called.append('stop'))
    apply_saved_result(app, requested, result)
    assert called == ['stop', 'refresh']
    assert not app.settings.tts_enabled
