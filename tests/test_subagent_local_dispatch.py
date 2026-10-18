"""Actual dispatch boundary never invokes local readiness/load machinery."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from litetui import subagent_dispatch as dispatch
from litetui.settings import Settings
from litetui.settings_service import SettingChange, SettingsService


def host(root):
    service = SettingsService(root)
    snap = service.create_conversation('one')
    changes = [SettingChange('subagent_route', {'backend': 'llamacpp', 'model': 'child'}, 'device'),
               SettingChange('allow_local_subagents', True, 'device')]
    service.save_patch('one', changes, snap.revisions)
    return SimpleNamespace(backend=SimpleNamespace(name='codex'), model_id='parent', settings=Settings(),
                           convo_dir=root / '.convos' / 'one', _settings_service=service)


def test_same_loaded_reused_then_live_toggle_revocation_refuses(tmp_path, monkeypatch):
    app = host(tmp_path)
    calls = []
    backend = SimpleNamespace(name='llamacpp', subagent_model_states=lambda: {'child': 'loaded'},
                              load=Mock(), ensure_running=Mock(), ensure_chat_ready=Mock())
    async def execute(provider, payload):
        calls.append(payload['model'])
        return {'choices': [{'message': {'content': 'ok'}}]}
    monkeypatch.setattr(dispatch, 'make_backend', lambda settings: backend)
    monkeypatch.setattr(dispatch, '_execute', execute)
    assert dispatch.complete_child(app, {'messages': []})['model'] == 'child'
    app._settings_service.save_patch('one', [SettingChange('allow_local_subagents', False, 'device')],
                                    app._settings_service.snapshot('one').revisions)
    with pytest.raises(dispatch.ChildError, match='expert'):
        dispatch.complete_child(app, {'messages': []})
    assert calls == ['child']
    backend.load.assert_not_called()
    backend.ensure_running.assert_not_called()
    backend.ensure_chat_ready.assert_not_called()


def test_loading_refused_before_transport(tmp_path, monkeypatch):
    app = host(tmp_path)
    backend = SimpleNamespace(name='llamacpp', subagent_model_states=lambda: {'child': 'loaded', 'other': 'loading'})
    execute = Mock()
    monkeypatch.setattr(dispatch, 'make_backend', lambda settings: backend)
    monkeypatch.setattr(dispatch, '_execute', execute)
    with pytest.raises(dispatch.ChildError, match='loading'):
        dispatch.complete_child(app, {'messages': []})
    execute.assert_not_called()


def test_lmstudio_even_resident_refused_at_dispatch(tmp_path, monkeypatch):
    app = host(tmp_path)
    service = app._settings_service
    service.save_patch('one', [SettingChange('subagent_route', {'backend': 'lmstudio', 'model': 'child'}, 'device')],
                       service.snapshot('one').revisions)
    from litetui.llm_backend import LMStudioBackend
    backend = LMStudioBackend.__new__(LMStudioBackend)
    backend.subagent_model_states = lambda: {'child': 'loaded'}
    backend.host = lambda: 'http://127.0.0.1:1234'
    execute = Mock()
    monkeypatch.setattr(dispatch, 'make_backend', lambda settings: backend)
    monkeypatch.setattr(dispatch, '_execute', execute)
    with pytest.raises(dispatch.ChildError, match='usage/lease protocol'):
        dispatch.complete_child(app, {'messages': []})
    execute.assert_not_called()
