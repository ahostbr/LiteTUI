from types import SimpleNamespace
from litetui.app import LiteTUI
from litetui import llm_backend
import pytest


def test_interactive_resume_blocks_send_if_saved_backend_unavailable(monkeypatch):
    def unavailable(settings):
        raise llm_backend.BackendError('not installed')
    monkeypatch.setattr(llm_backend, 'make_backend', unavailable)
    app = SimpleNamespace(settings=SimpleNamespace(), _backend=SimpleNamespace(name='codex'),
                          _system=lambda text: None)
    LiteTUI._adopt_convo_backend(app, SimpleNamespace(backend='ninfer'))
    assert app._resume_backend_error


@pytest.mark.asyncio
async def test_interactive_backend_refusal_reaches_shared_send_gate(monkeypatch):
    def unavailable(settings):
        raise llm_backend.BackendError('not installed')
    monkeypatch.setattr(llm_backend, 'make_backend', unavailable)
    app = SimpleNamespace(settings=SimpleNamespace(), _backend=SimpleNamespace(name='codex'),
                          _system=lambda text: None)
    LiteTUI._adopt_convo_backend(app, SimpleNamespace(backend='ninfer'))
    with pytest.raises(llm_backend.BackendError, match='unavailable'):
        await LiteTUI._ensure_chat_ready(app)


def test_explicit_backend_assignment_clears_resume_refusal():
    app = SimpleNamespace(_resume_backend_error='blocked', _remember_for_this_convo=lambda *args: None)
    LiteTUI.backend.fset(app, SimpleNamespace(name='codex'))
    assert app._resume_backend_error is None


def test_explicit_same_backend_choice_can_recover_from_refused_resume():
    from unittest.mock import Mock
    from litetui.plugins.model_switch import _switch_backend
    app = SimpleNamespace(backend=SimpleNamespace(name='codex'), _resume_backend_error='blocked',
                          system_message=Mock(), apply_backend_change=Mock())
    _switch_backend(app, 'codex')
    app.apply_backend_change.assert_called_once_with('codex')
