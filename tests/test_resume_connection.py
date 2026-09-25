"""/resume restores and connects the provider, model and thinking selection."""
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import convo_settings, llm_backend, paths
from litetui.conversation import ConversationRepository
from litetui.settings import Settings


class Backend:
    remote = True
    attached = True

    def __init__(self, settings, calls, fail=False):
        self.name = self.label = settings.backend
        self.settings = settings
        self.calls = calls
        self.fail = fail

    def shutdown(self):
        pass

    def set_settings(self, settings):
        self.settings = settings

    def base_url(self):
        return 'http://127.0.0.1:1/v1'

    host = base_url

    async def ensure_running(self):
        self.calls.append(('connect', self.name))
        if self.fail:
            raise llm_backend.BackendError('provider unavailable')
        return 'ok'

    async def list_models(self):
        self.calls.append(('models', self.name))
        return [SimpleNamespace(key=m, loaded=True) for m in ('saved-model', 'global-model')]


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', llm_backend.BACKEND_NAMES)
@pytest.mark.parametrize('same_provider', [False, True])
async def test_resume_connects_saved_selection(tmp_path, monkeypatch, provider, same_provider):
    monkeypatch.setattr(paths, 'CONVO_DIR', tmp_path)
    calls = []
    monkeypatch.setattr(llm_backend, 'make_backend', lambda settings: Backend(settings, calls))
    app = app_mod.LiteTUI()
    app.settings = Settings(backend='custom', default_model='global-model', pin_default_model=True)
    app._backend = Backend(app.settings, calls)
    app._connect = lambda: None  # Only suppress mount's initial connection.
    app._fetch_ctx_window = lambda: None
    app._probe_thinking = lambda: None
    async with app.run_test() as pilot:
        await pilot.pause()
        app._materialise_convo()
        app._append({'role': 'user', 'content': 'saved conversation'})
        path = app.convo_path
        convo_settings.save(path.parent, convo_settings.ConvoSettings(
            backend=provider, model='saved-model', thinking_level='high',
            reasoning_effort='high', execution={'pin_default_model': True}))
        before = convo_settings.path_for(path.parent).read_bytes()
        app._backend = Backend(Settings(backend=provider if same_provider else 'other'), calls)
        app.available_models = ['stale-model']
        app.model_rows = {'stale-model': object()}
        app._model_id = 'stale-model'
        monkeypatch.delenv('LITETUI_BACKEND', raising=False)
        assert app._resume(path)
        await pilot.pause()
        assert ('connect', provider) in calls
        assert ('models', provider) in calls
        assert app.backend.name == app.settings.backend == provider
        assert app.model_id == 'saved-model'
        assert app.thinking_level == 'high'
        assert app.available_models == ['saved-model', 'global-model']
        assert not getattr(app, '_resume_backend_error', None)
        assert not getattr(app, '_resume_connection_error', None)
        assert app.backend.settings is app.settings
        assert convo_settings.path_for(path.parent).read_bytes() == before
        assert ConversationRepository.read(path)[1]

@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['connection', 'model'])
async def test_resume_failure_blocks_send_and_reconnect_recovers(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(paths, 'CONVO_DIR', tmp_path)
    calls = []
    backend = Backend(Settings(backend='custom'), calls)
    monkeypatch.setattr(llm_backend, 'make_backend', lambda settings: backend)
    app = app_mod.LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    async with app.run_test() as pilot:
        await pilot.pause()
        app._materialise_convo()
        app._append({'role': 'user', 'content': 'saved'})
        path = app.convo_path
        convo_settings.save(path.parent, convo_settings.ConvoSettings(
            backend='custom', model='missing' if failure == 'model' else 'saved-model',
            thinking_level='high'))
        monkeypatch.delenv('LITETUI_BACKEND', raising=False)
        backend.fail = failure == 'connection'
        assert app._resume(path)
        await pilot.pause()
        assert app._resume_connection_error
        assert app.model_id == ('missing' if failure == 'model' else 'saved-model')
        with pytest.raises(llm_backend.BackendError):
            await app._ensure_chat_ready()
        backend.fail = False
        if failure == 'model':
            app.model_id = 'saved-model'
        app.connect()
        await pilot.pause()
        assert not app._resume_connection_error
        assert app.model_id == 'saved-model'
        assert app.thinking_level == 'high'

@pytest.mark.asyncio
async def test_native_history_waits_for_connection(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(paths, 'CONVO_DIR', tmp_path)
    calls = []
    backend = Backend(Settings(backend='codex'), calls)
    backend.app_server = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(llm_backend, 'make_backend', lambda settings: backend)
    app = app_mod.LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app._refresh_native_history = lambda path: calls.append(('history', path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._materialise_convo()
        app._append({'role': 'user', 'content': 'saved'})
        path = app.convo_path
        convo_settings.save(path.parent, convo_settings.ConvoSettings(
            backend='codex', model='saved-model', reasoning_effort='high'))
        monkeypatch.delenv('LITETUI_BACKEND', raising=False)
        assert app._resume(path)
        assert ('history', path) not in calls
        await pilot.pause()
        assert calls.index(('connect', 'codex')) < calls.index(('models', 'codex')) < calls.index(('history', path))
        assert app.thinking_level == 'high'
