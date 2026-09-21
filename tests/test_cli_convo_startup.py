"""Startup resume must precede connection and fail closed."""
import asyncio
from types import SimpleNamespace

import pytest
from litetui.app import LiteTUI
from litetui import paths


def host(tmp_path, monkeypatch, requested='saved'):
    monkeypatch.setattr(paths, 'CONVO_DIR', tmp_path)
    app = SimpleNamespace(_cli_convo_id=requested, convo_path=None, _said=[])
    app._system = app._said.append
    return app


def test_startup_resume_adopts_requested_store(tmp_path, monkeypatch):
    app = host(tmp_path, monkeypatch)
    target = tmp_path / 'saved' / 'convo.jsonl'
    target.parent.mkdir()
    target.write_text('{}\n')
    calls = []
    def resume(path, *, startup=False):
        calls.append((path, startup))
        app.convo_path = path
        return True
    app._resume = resume
    assert LiteTUI._resume_cli_conversation(app)
    assert calls == [(target, True)]


@pytest.mark.parametrize('requested', ['missing', '../outside', '/absolute', 'a/b', 'a\\b'])
def test_invalid_startup_never_resumes(tmp_path, monkeypatch, requested):
    app = host(tmp_path, monkeypatch, requested)
    app._resume = lambda *a, **k: pytest.fail('invalid path reached resume')
    assert not LiteTUI._resume_cli_conversation(app)
    assert app._cli_launch_error


def test_owned_or_unreadable_conversation_blocks(tmp_path, monkeypatch):
    app = host(tmp_path, monkeypatch)
    target = tmp_path / 'saved' / 'convo.jsonl'
    target.parent.mkdir()
    target.touch()
    app._resume = lambda *a, **k: False
    assert not LiteTUI._resume_cli_conversation(app)
    assert app._cli_launch_error


def test_existing_startup_failure_not_cleared_by_cli_worker():
    app = SimpleNamespace(_cli_launch_error='Conversation is owned',
        _cli_args_done=asyncio.Event(), _first_prompt='must not run')
    asyncio.run(LiteTUI._apply_cli_args.__wrapped__(app))
    assert app._cli_launch_error == 'Conversation is owned'
    assert app._cli_args_done.is_set()

@pytest.mark.asyncio
async def test_blocked_startup_cannot_send_later_rpc_prompt():
    from litetui.llm_backend import BackendError
    app = SimpleNamespace(_startup_resume_error='Conversation is owned')
    with pytest.raises(BackendError, match='owned'):
        await LiteTUI._ensure_chat_ready(app)


def test_actual_resume_restores_before_render_and_defers_native_history(tmp_path, monkeypatch):
    from litetui.app import ConversationRepository
    app = host(tmp_path, monkeypatch)
    target = tmp_path / 'saved' / 'convo.jsonl'
    target.parent.mkdir()
    target.touch()
    messages = [{'role': 'user', 'content': 'saved message'}]
    monkeypatch.setattr(ConversationRepository, 'read', lambda path: ({'id': 'saved'}, messages))
    calls = []
    app.store = SimpleNamespace(acquire=lambda directory: calls.append('acquire'))
    app._pending_input = []
    app._sync_seat_identity = lambda: None
    app._sync_fleet_identity = lambda: None
    app._refresh_ctx_label = lambda: None
    app._adopt_convo_settings = lambda **kw: calls.append('settings')
    app._render_resumed = lambda path: calls.append('render')
    app.backend = SimpleNamespace(app_server=object())
    app._refresh_native_history = lambda path: pytest.fail('native history before connection')
    assert LiteTUI._resume(app, target, startup=True)
    assert calls == ['acquire', 'settings', 'render']
    assert app.convo_id == 'saved'
    assert app.conversation is messages
    assert not app._convo_pending


def test_unavailable_saved_backend_cannot_fall_back_at_startup(monkeypatch):
    from litetui import llm_backend
    def unavailable(settings):
        raise llm_backend.BackendError('not installed')
    monkeypatch.setattr(llm_backend, 'make_backend', unavailable)
    app = SimpleNamespace(_startup_adopting=True, settings=SimpleNamespace(),
        _backend=SimpleNamespace(name='codex'))
    with pytest.raises(ValueError, match='unavailable'):
        LiteTUI._adopt_convo_backend(app, SimpleNamespace(backend='ninfer'))
    assert app._backend.name == 'codex'
