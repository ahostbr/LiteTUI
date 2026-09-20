from types import SimpleNamespace as NS
from litetui.settings import Settings
from litetui import convo_settings
from litetui.app import LiteTUI


def test_new_conversation_does_not_inherit_invocation_backend(tmp_path):
    settings = Settings(backend='codex')
    app = NS(settings=settings, _cli_initial_backend='codex',
             _invocation_saved_values={'backend': 'ninfer', 'backend_chosen': True},
             convo_dir=tmp_path, seat=NS())
    LiteTUI._adopt_convo_settings(app, born=True)
    stored = convo_settings.load(tmp_path)
    assert stored.backend == 'ninfer'
    assert stored.execution['backend'] == 'ninfer'
    assert app.settings.backend == 'codex'


def test_unrelated_save_without_conversation_does_not_persist_invocation(tmp_path, monkeypatch):
    from litetui import settings as st
    from litetui.settings_runtime import persist_settings
    from dataclasses import replace
    monkeypatch.setattr(st, 'settings_path', lambda *args, **kwargs: tmp_path / 'settings.json')
    settings = Settings(backend='codex')
    app = NS(settings=settings, convo_dir=None,
             _invocation_saved_values={'backend': 'ninfer', 'backend_chosen': True})
    candidate = replace(settings, tts_timeout=123)
    result = persist_settings(app, candidate)
    import json
    saved = json.loads((tmp_path / 'settings.json').read_text())
    assert saved['backend'] == 'ninfer'
    assert saved['tts_timeout'] == 123
    assert candidate.backend == 'codex'
    assert result.persistence[0].saved


def test_conversation_unrelated_save_preserves_remembered_backend(tmp_path):
    from dataclasses import replace
    from litetui.settings_runtime import persist_settings
    from litetui.settings_service import SettingsService
    root = tmp_path / 'data'
    service = SettingsService(root)
    service.create_conversation('conversation')
    directory = root / '.convos' / 'conversation'
    # Use service's own configured path rather than assume storage layout.
    directory = service._paths('conversation')['conversation'].parent
    stored_before = directory.joinpath('settings.json').read_bytes()
    effective = Settings(backend='codex')
    app = NS(settings=effective, convo_dir=directory, _settings_service=service,
             _invocation_saved_values={'backend': 'lmstudio', 'backend_chosen': False})
    outcome = persist_settings(app, replace(effective, tts_timeout=123))
    assert all(p.saved for p in outcome.persistence)
    assert directory.joinpath('settings.json').read_bytes() == stored_before
    assert service.snapshot('conversation').saved.backend == 'lmstudio'
    assert service.snapshot('conversation').saved.tts_timeout == 123


def test_deliberate_backend_edit_is_not_stripped(tmp_path, monkeypatch):
    from dataclasses import replace
    from litetui import settings as st
    from litetui.settings_runtime import persist_settings
    import json
    monkeypatch.setattr(st, 'settings_path', lambda *args, **kwargs: tmp_path / 'settings.json')
    effective = Settings(backend='codex')
    app = NS(settings=effective, convo_dir=None,
             _invocation_saved_values={'backend': 'ninfer'})
    persist_settings(app, replace(effective, backend='llamacpp'))
    assert json.loads((tmp_path / 'settings.json').read_text())['backend'] == 'llamacpp'


def test_explicit_edit_retires_override_before_next_unrelated_save(tmp_path, monkeypatch):
    from dataclasses import replace
    from litetui import settings as st
    from litetui.settings_runtime import persist_settings
    import json
    monkeypatch.setattr(st, 'settings_path', lambda *args, **kwargs: tmp_path / 'settings.json')
    app = NS(settings=Settings(backend='codex'), convo_dir=None,
             _invocation_saved_values={'backend': 'ninfer'})
    chosen = replace(app.settings, backend='llamacpp')
    persist_settings(app, chosen)
    app.settings = chosen
    persist_settings(app, replace(chosen, tts_timeout=123))
    assert json.loads((tmp_path / 'settings.json').read_text())['backend'] == 'llamacpp'


def test_failed_explicit_save_keeps_invocation_override(tmp_path, monkeypatch):
    import pytest
    from dataclasses import replace
    from litetui import settings as st
    from litetui.settings_runtime import persist_settings
    app = NS(settings=Settings(backend='codex'), convo_dir=None,
             _cli_initial_backend='codex', _invocation_saved_values={'backend': 'ninfer'})
    def failed(candidate):
        raise OSError('disk fixture')
    monkeypatch.setattr(st, 'save', failed)
    with pytest.raises(OSError, match='disk fixture'):
        persist_settings(app, replace(app.settings, backend='llamacpp'))
    assert app._invocation_saved_values == {'backend': 'ninfer'}
    assert app._cli_initial_backend == 'codex'


def test_resume_then_new_conversation_uses_resumed_saved_backend(tmp_path):
    stored = convo_settings.born_from(Settings(backend='llamacpp'))
    convo_settings.save(tmp_path, stored)
    app = NS(settings=Settings(backend='codex'), convo_dir=tmp_path,
             _cli_initial_backend='codex', _cli_tool_profile='autonomous',
             _invocation_saved_values={'backend': 'ninfer', 'backend_chosen': True},
             _backend=NS(name='codex'), _model_id='', available_models=[], seat=NS(),
             _system=lambda text: None, _adopt_convo_backend=lambda cs: None)
    before = (tmp_path / 'settings.json').read_bytes()
    LiteTUI._adopt_convo_settings(app, born=False)
    assert app.settings.backend == 'codex'
    assert (tmp_path / 'settings.json').read_bytes() == before
    assert app._invocation_saved_values['backend'] == 'llamacpp'
    new = tmp_path / 'new'
    new.mkdir()
    app.convo_dir = new
    LiteTUI._adopt_convo_settings(app, born=True)
    assert convo_settings.load(new).backend == 'llamacpp'
