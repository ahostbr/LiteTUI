"""Scoped app persistence adapter without starting a terminal or model."""
from types import SimpleNamespace

import pytest

from litetui import settings as st
from litetui import convo_settings as cs

# Owned conversation folders are UUIDs (agent_store.valid_id), and every write goes
# through an owned agent session, so each test gets a real agent with a chosen model.
A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
VOICE = '33333333-3333-4333-8333-333333333333'


@pytest.fixture(autouse=True)
def _owned_sessions():
    opened = []
    yield opened
    for session in opened:
        session.release()


@pytest.fixture
def _service(tmp_path, _owned_sessions):
    from litetui.agent_launch_context import ordinary
    from litetui.settings_service import SettingsService

    def make():
        session = ordinary(tmp_path, st.Settings(), notice=lambda _text: None,
                           backend='lmstudio', model='fixture', thinking_level='medium')
        _owned_sessions.append(session)
        return SettingsService(tmp_path, agent_session=session)
    return make


def test_runtime_adapter_never_persists_execution_to_global(_service, tmp_path):
    from litetui.settings_runtime import persist_settings
    from litetui.settings_service import SettingsService
    service = _service()
    service.create_conversation(A)
    app = SimpleNamespace(convo_dir=service.conversation_root / A,
                          settings=service.snapshot(A).effective,
                          _settings_service=service, _agent_session=service.agent_session)
    app.settings.backend = 'codex'
    result = persist_settings(app, app.settings)
    assert result.fully_saved
    assert service.snapshot(A).saved.backend == 'codex'
    assert not st.settings_path(tmp_path).exists()
    assert cs.load(app.convo_dir).backend == 'codex'


def test_runtime_adapter_theme_save_does_not_leak_environment(_service, tmp_path, monkeypatch):
    from litetui.settings_runtime import persist_settings
    from litetui.settings_service import SettingsService
    monkeypatch.setenv('LITETUI_BACKEND', 'ninfer')
    service = _service()
    service.create_conversation(A)
    app = SimpleNamespace(convo_dir=service.conversation_root / A,
                          settings=service.snapshot(A).effective,
                          _settings_service=service, _agent_session=service.agent_session)
    app.settings.theme_name = 'monokai'
    assert persist_settings(app, app.settings).fully_saved
    assert service.snapshot(A).saved.backend == 'lmstudio'
    assert service.snapshot(A).saved.theme_name == 'monokai'


def test_command_patch_does_not_revert_unrelated_device_change(_service, tmp_path):
    from litetui.settings_runtime import persist_settings
    from litetui.settings_service import SettingsService, SettingChange
    svc = _service()
    snap = svc.create_conversation(A)
    app = SimpleNamespace(convo_dir=svc.conversation_root / A, settings=snap.effective,
                          _settings_service=svc, _agent_session=svc.agent_session)
    svc.save_patch(B, [SettingChange('theme_name', 'monokai', 'device')], svc.snapshot(B).revisions)
    app.settings.backend = 'codex'
    assert persist_settings(app, app.settings).fully_saved
    assert svc.snapshot(A).saved.theme_name == 'monokai'


def test_real_settings_callback_persists_backend_only_in_conversation(_service, tmp_path, monkeypatch):
    from litetui.app import LiteTUI
    from litetui.settings_service import SettingsService
    from copy import deepcopy
    svc = _service()
    snap = svc.create_conversation(A)
    class App:
        _on_settings_saved = LiteTUI._on_settings_saved
        settings = snap.effective
        convo_dir = svc.conversation_root / A
        _settings_service = svc
        _agent_session = svc.agent_session
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
    assert svc.snapshot(A).saved.backend == 'codex'


def test_runtime_result_distinguishes_saved_from_effective(_service, tmp_path):
    from litetui.settings_runtime import apply_saved_result
    from litetui.settings_service import SettingsService, SettingChange
    svc = _service()
    snap = svc.create_conversation(A)
    app = SimpleNamespace(settings=snap.effective, convo_dir=svc.conversation_root / A,
                          _settings_service=svc, _agent_session=svc.agent_session)
    result = svc.save_patch(A, [SettingChange('ninfer_max_context', 16384, 'device')], snap.revisions)
    target = svc.snapshot(A).saved
    result = apply_saved_result(app, target, result)
    assert result.fully_saved
    assert result.runtime[0].field == 'ninfer_max_context'
    assert result.runtime[0].status == 'pending'
    assert result.runtime[0].action == 'restart'
    assert result.runtime[0].requested == 16384
    assert result.runtime[0].effective == 32768


def test_saved_environment_field_stays_effective_override(_service, tmp_path, monkeypatch):
    from litetui.settings_runtime import apply_saved_result
    from litetui.settings_service import SettingsService, SettingChange
    monkeypatch.setenv('LITETUI_THINKING', 'high')
    svc = _service()
    snap = svc.create_conversation(A)
    app = SimpleNamespace(settings=snap.effective, convo_dir=svc.conversation_root / A, _settings_service=svc, _agent_session=svc.agent_session)
    result = svc.save_patch(A, [SettingChange('thinking_level', 'low', 'conversation')], snap.revisions)
    result = apply_saved_result(app, svc.snapshot(A).saved, result)
    assert app.settings.thinking_level == 'high'
    assert result.runtime[0].requested == 'low'
    assert result.runtime[0].effective == 'high'
    assert result.runtime[0].status == 'pending'


def test_settings_plugin_supplies_scoped_bindings(_service, tmp_path, monkeypatch):
    from litetui.plugins import settings_ui
    from litetui.settings_service import SettingsService
    svc = _service()
    snap = svc.create_conversation(A)
    captured = {}
    app = SimpleNamespace(settings=snap.effective, convo_dir=svc.conversation_root / A,
                          _settings_service=svc, _agent_session=svc.agent_session, available_models=[],
                          _on_settings_saved=lambda value: None)
    monkeypatch.setattr(settings_ui, 'mcp_server_names', lambda a: [])
    monkeypatch.setattr(settings_ui.model_residency, 'resident_models', lambda a: ([], False))
    monkeypatch.setattr(settings_ui, 'present_dialog', lambda a, body, screen, callback: captured.update(body=body, screen=screen))
    settings_ui._cmd_settings(app, 'settings', '')
    assert callable(captured['body'].keywords['snapshot_provider'])
    assert callable(captured['screen'].keywords['save_patch'])
    assert callable(captured['screen'].keywords['runtime_apply'])


def test_real_ui_adapter_can_save_twice_with_service_revisions(_service, tmp_path):
    from copy import deepcopy
    from litetui.settings_ui_adapter import SettingsUiAdapter
    from litetui.settings_service import SettingsService
    svc = _service()
    snap = svc.create_conversation(A)
    adapter = SettingsUiAdapter(snap.effective, snapshot_provider=lambda: svc.snapshot(A),
                                save_patch=lambda changes, rev: svc.save_patch(A, changes, rev))
    target = deepcopy(snap.effective)
    target.temperature = 0.2
    assert adapter.save(target).fully_saved
    target.temperature = 0.4
    assert adapter.save(target).fully_saved


def test_runtime_failure_preserves_saved_evidence_and_old_effective(_service, tmp_path):
    from litetui.settings_runtime import apply_saved_result
    from litetui.settings_service import SettingsService, SettingChange
    svc = _service()
    snap = svc.create_conversation(A)
    class App:
        settings = snap.effective
        convo_dir = svc.conversation_root / A
        @property
        def theme(self): return 'textual-dark'
        @theme.setter
        def theme(self, value): raise RuntimeError('injected runtime failure')
    app = App()
    result = svc.save_patch(A, [SettingChange('theme_name', 'monokai', 'device')], snap.revisions)
    result = apply_saved_result(app, svc.snapshot(A).saved, result)
    assert result.fully_saved
    assert result.runtime[0].status == 'failed'
    assert app.settings.theme_name == 'textual-dark'
    assert svc.snapshot(A).saved.theme_name == 'monokai'


def test_pending_runtime_is_retained_when_user_saves_again(_service, tmp_path):
    from copy import deepcopy
    from litetui.settings_runtime import apply_saved_result
    from litetui.settings_ui_adapter import SettingsUiAdapter
    from litetui.settings_service import SettingsService
    svc = _service()
    snap = svc.create_conversation(A)
    app = SimpleNamespace(settings=snap.effective, convo_dir=svc.conversation_root / A, _settings_service=svc, _agent_session=svc.agent_session)
    adapter = SettingsUiAdapter(snap.effective, snapshot_provider=lambda: svc.snapshot(A),
             save_patch=lambda changes, rev: svc.save_patch(A, changes, rev),
             runtime_apply=lambda target, result: apply_saved_result(app, target, result))
    target = deepcopy(snap.effective)
    target.ninfer_max_context = 16384
    first = adapter.save(target)
    assert first.has_pending_runtime
    second = adapter.save(target)
    assert second.has_pending_runtime


def test_reconnect_prepares_saved_endpoint_without_reverting_pending_restart(_service, tmp_path):
    from litetui.settings_runtime import prepare_reconnect
    from litetui.settings_service import SettingsService, SettingChange
    svc = _service()
    snap = svc.create_conversation(A)
    app = SimpleNamespace(settings=snap.effective, convo_dir=svc.conversation_root / A, _settings_service=svc, _agent_session=svc.agent_session)
    svc.save_patch(A, [SettingChange('lm_host', 'http://localhost:9999', 'conversation'),
                         SettingChange('ninfer_max_context', 16384, 'device')], snap.revisions)
    prepare_reconnect(app)
    assert app.settings.lm_host == 'http://localhost:9999'
    assert app.settings.ninfer_max_context == 32768


def test_reconnect_factory_failure_preserves_effective_backend(_service, tmp_path, monkeypatch):
    from litetui.settings_runtime import prepare_reconnect
    from litetui.settings_service import SettingsService, SettingChange
    from litetui import llm_backend
    import pytest
    svc = _service()
    snap = svc.create_conversation(A)
    old_backend = SimpleNamespace(name='lmstudio')
    app = SimpleNamespace(settings=snap.effective, convo_dir=svc.conversation_root / A,
                          _settings_service=svc, _agent_session=svc.agent_session, backend=old_backend)
    svc.save_patch(A, [SettingChange('backend', 'codex', 'conversation')], snap.revisions)
    monkeypatch.delenv('LITETUI_BACKEND', raising=False)
    def fail(settings): raise llm_backend.BackendError('injected unavailable provider')
    monkeypatch.setattr(llm_backend, 'make_backend', fail)
    with pytest.raises(llm_backend.BackendError):
        prepare_reconnect(app)
    assert app.backend is old_backend
    assert app.settings.backend == 'lmstudio'
    assert svc.snapshot(A).saved.backend == 'codex'


def test_saved_voice_off_stops_current_speech_and_refreshes_controls(_service, tmp_path, monkeypatch):
    from litetui.settings_runtime import apply_saved_result
    from litetui.settings_service import SettingsService, SettingChange
    from litetui import voice_backend
    from copy import deepcopy
    svc = _service()
    snap = svc.create_conversation(VOICE)
    called = []
    app = SimpleNamespace(settings=deepcopy(snap.effective), _refresh_prompt_controls=lambda: called.append('refresh'))
    app.settings.tts_enabled = True
    requested = deepcopy(app.settings)
    requested.tts_enabled = False
    result = svc.save_patch(VOICE, [SettingChange('tts_enabled', False, 'device')], snap.revisions)
    monkeypatch.setattr(voice_backend, 'stop', lambda: called.append('stop'))
    apply_saved_result(app, requested, result)
    assert called == ['stop', 'refresh']
    assert not app.settings.tts_enabled
