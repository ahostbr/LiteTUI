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
