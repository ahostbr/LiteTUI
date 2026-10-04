"""T0347 child sessions own their provider configuration, auth and cleanup."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from litetui import subagent_dispatch as dispatch
from litetui.settings import Settings
from litetui.settings_service import SettingChange, SettingsService


def app(tmp_path, parent='claude', child='codex'):
    service = SettingsService(tmp_path)
    snap = service.create_conversation('one')
    service.save_patch('one', [SettingChange('subagent_route', {'backend': child, 'model': 'child'}, 'device')], snap.revisions)
    return SimpleNamespace(backend=SimpleNamespace(name=parent), model_id='parent',
                           settings=Settings(backend=parent, custom_base_url='https://parent.invalid',
                                             custom_api_key_env='PARENT_SECRET'),
                           convo_dir=tmp_path / '.convos' / 'one', _settings_service=service)


@pytest.mark.parametrize('parent,child', [('claude','codex'), ('codex','claude'), ('claude','free'), ('codex','cline')])
def test_cross_backend_child_uses_fresh_settings_and_backend(tmp_path, monkeypatch, parent, child):
    host = app(tmp_path, parent, child)
    created = []
    async def ready(model):
        assert model == 'child'
    fake = SimpleNamespace(name=child, ensure_chat_ready=ready)
    async def execute(backend, payload):
        assert backend is fake
        assert payload['model'] == 'child'
        return {'choices': [{'message': {'content': 'answer'}}], 'usage': {'completion_tokens': 3}}
    def factory(settings):
        created.append(settings)
        return fake
    monkeypatch.setattr(dispatch, 'make_backend', factory)
    monkeypatch.setattr(dispatch, '_execute', execute)
    result = dispatch.complete_child(host, {'messages': [{'role': 'user', 'content': 'hello'}]})
    assert result['choices'][0]['message']['content'] == 'answer'
    assert created[0] is not host.settings
    assert created[0].backend == child
    assert created[0].custom_api_key_env != 'PARENT_SECRET'
    assert host.model_id == 'parent'
    assert host.backend.name == parent


def test_unavailable_auth_names_backend_and_fix(tmp_path, monkeypatch):
    host = app(tmp_path, child='cline')
    async def ready(model):
        raise dispatch.BackendError('run `cline auth`')
    monkeypatch.setattr(dispatch, 'make_backend', lambda settings: SimpleNamespace(name='cline', ensure_chat_ready=ready))
    monkeypatch.setattr(dispatch, '_execute', Mock())
    with pytest.raises(dispatch.ChildError, match='cline.*cline auth'):
        dispatch.complete_child(host, {'messages': []})


def test_child_cleanup_runs_on_success_and_failure(tmp_path, monkeypatch):
    host = app(tmp_path)
    calls = []
    async def ready(model):
        pass
    async def close():
        calls.append('closed')
    async def execute(backend, payload):
        raise dispatch.ChildError('child failed')
    monkeypatch.setattr(dispatch, 'make_backend', lambda settings: SimpleNamespace(name='codex', ensure_chat_ready=ready, close=close))
    monkeypatch.setattr(dispatch, '_execute', execute)
    with pytest.raises(dispatch.ChildError, match='codex.*child failed'):
        dispatch.complete_child(host, {'messages': []})
    assert calls == ['closed']


@pytest.mark.asyncio
async def test_sync_dispatch_refuses_ui_loop_before_creating_resources(tmp_path, monkeypatch):
    host = app(tmp_path)
    factory = Mock()
    monkeypatch.setattr(dispatch, 'make_backend', factory)
    with pytest.raises(dispatch.ChildError, match='worker thread'):
        dispatch.complete_child(host, {'messages': []})
    factory.assert_not_called()


def test_unknown_provider_error_does_not_echo_body_or_credentials(tmp_path, monkeypatch):
    host = app(tmp_path)
    async def ready(model):
        raise RuntimeError('secret-credential \x1b[31m' + 'response body ' * 1000)
    monkeypatch.setattr(dispatch, 'make_backend', lambda settings: SimpleNamespace(name='codex', ensure_chat_ready=ready))
    with pytest.raises(dispatch.ChildError) as caught:
        dispatch.complete_child(host, {'messages': []})
    text = str(caught.value)
    assert 'secret-credential' not in text
    assert 'response body' not in text
    assert '\x1b' not in text
    assert len(text) < 600
