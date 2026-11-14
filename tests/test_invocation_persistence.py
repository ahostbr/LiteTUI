from types import SimpleNamespace as NS

from _owned_seat import conversation_id, owned_session

from litetui.settings import Settings
from litetui import convo_settings
from litetui.app import LiteTUI


def test_new_conversation_is_born_on_the_invocation_backend(tmp_path):
    # T1027 (ruling 2b9aed25): the backend a flag launched is a fact about the
    # transcript, so the born file records it; the global default is untouched.
    settings = Settings(backend='codex')
    session = owned_session(tmp_path, backend='codex')
    directory = session.conversation_directory(conversation_id('born'))
    app = NS(settings=settings, _cli_initial_backend='codex',
             _invocation_saved_values={'backend': 'ninfer', 'backend_chosen': True},
             convo_dir=directory, seat=NS(), _agent_session=session)
    LiteTUI._adopt_convo_settings(app, born=True)
    stored = convo_settings.load(directory)
    assert stored.backend == 'codex'
    assert stored.execution['backend'] == 'codex'
    assert app.settings.backend == 'codex'
    assert app._invocation_saved_values['backend'] == 'ninfer', 'the saved global was rewritten'


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
    root.mkdir()
    session = owned_session(root)
    service = SettingsService(root, agent_session=session)
    cid = conversation_id('conversation')
    service.create_conversation(cid)
    # Use service's own configured path rather than assume storage layout.
    directory = service._paths(cid)['conversation'].parent
    stored_before = directory.joinpath('settings.json').read_bytes()
    effective = Settings(backend='codex')
    app = NS(settings=effective, convo_dir=directory, _settings_service=service, _agent_session=session,
             _invocation_saved_values={'backend': 'lmstudio', 'backend_chosen': False})
    outcome = persist_settings(app, replace(effective, tts_timeout=123))
    assert all(p.saved for p in outcome.persistence)
    assert directory.joinpath('settings.json').read_bytes() == stored_before
    assert service.snapshot(cid).saved.backend == 'lmstudio'
    assert service.snapshot(cid).saved.tts_timeout == 123


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


def test_resume_then_new_conversation_is_born_on_the_launch_flag_backend(tmp_path):
    session = owned_session(tmp_path, backend='codex')
    resumed = session.conversation_directory(conversation_id('resumed'))
    resumed.mkdir(parents=True)
    stored = convo_settings.born_from(Settings(backend='llamacpp'))
    convo_settings.save(resumed, stored, agent_session=session)
    app = NS(settings=Settings(backend='codex'), convo_dir=resumed,
             _cli_initial_backend='codex', _cli_tool_profile='autonomous',
             _invocation_saved_values={'backend': 'ninfer', 'backend_chosen': True},
             _backend=NS(name='codex'), _model_id='', available_models=[], seat=NS(),
             _system=lambda text: None, _adopt_convo_backend=lambda cs: None, _agent_session=session)
    before = (resumed / 'settings.json').read_bytes()
    LiteTUI._adopt_convo_settings(app, born=False)
    assert app.settings.backend == 'codex'
    assert (resumed / 'settings.json').read_bytes() == before
    # An owned agent's authority is its backend (a2f8e823): the resume adopts the seat's
    # codex over the file's llamacpp, so the remembered backend is codex. T1027's own point,
    # a new conversation born on the launch-flag backend, is asserted below and still holds.
    assert app._invocation_saved_values['backend'] == 'codex'
    new = session.conversation_directory(conversation_id('new'))
    new.mkdir()
    app.convo_dir = new
    LiteTUI._adopt_convo_settings(app, born=True)
    # T1027 (Marquee's ruling 2b9aed25): the engine a flag launched is a fact
    # about the transcript, so a new conversation is born on it. Recording the
    # saved backend instead made a resume run a different engine
    # (.convos/ec62c953: a claude seat's file said codex).
    assert convo_settings.load(new).backend == 'codex'


def test_conversation_explicit_edit_retires_only_after_success(tmp_path):
    from dataclasses import replace
    from litetui.settings_runtime import persist_settings
    from litetui.settings_service import SettingsService
    session = owned_session(tmp_path)
    service = SettingsService(tmp_path, agent_session=session)
    ONE = conversation_id('one')
    service.create_conversation(ONE)
    directory = service._paths(ONE)['conversation'].parent
    app = NS(settings=Settings(backend='codex'), convo_dir=directory,
             _settings_service=service, _cli_initial_backend='codex', _agent_session=session,
             _invocation_saved_values={'backend': 'lmstudio'})
    chosen = replace(app.settings, backend='llamacpp')
    before = service.snapshot(ONE)
    stale = dict(before.revisions)
    stale['conversation'] = 'invalid-revision'
    failed = persist_settings(app, chosen, expected_revisions=stale)
    assert any(not p.saved for p in failed.persistence)
    assert app._invocation_saved_values == {'backend': 'lmstudio'}
    assert app._cli_initial_backend == 'codex'
    assert service.snapshot(ONE).saved.backend == 'lmstudio'
    saved = persist_settings(app, chosen)
    assert all(p.saved for p in saved.persistence)
    assert 'backend' not in app._invocation_saved_values
    assert app._cli_initial_backend is None
    app.settings = chosen
    persist_settings(app, replace(chosen, tts_timeout=150))
    assert service.snapshot(ONE).saved.backend == 'llamacpp'


def test_environment_then_cli_backend_never_becomes_new_conversation_default(tmp_path, monkeypatch):
    import json
    from litetui import settings as st
    (tmp_path / 'settings.json').write_text(json.dumps({'backend': 'lmstudio'}))
    monkeypatch.setenv('LITETUI_BACKEND', 'ninfer')
    settings = st.load(tmp_path)
    assert settings.backend == 'ninfer'
    from litetui.settings_runtime import capture_invocation
    saved = capture_invocation(settings, {'backend': 'codex', 'backend_chosen': True})
    assert settings.backend == 'codex'
    session = owned_session(tmp_path)
    directory = session.conversation_directory(conversation_id('conversation'))
    directory.mkdir(parents=True)
    app = NS(settings=settings, _invocation_saved_values=saved, convo_dir=directory, seat=NS(),
             _agent_session=session)
    LiteTUI._adopt_convo_settings(app, born=True)
    assert convo_settings.load(directory).backend == 'lmstudio'
    assert json.loads((tmp_path / 'settings.json').read_text())['backend'] == 'lmstudio'


def test_environment_only_clone_save_does_not_persist_effective_backend(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    from litetui import settings as st
    from litetui.settings_runtime import persist_settings
    (tmp_path / 'settings.json').write_text(json.dumps({'backend': 'lmstudio'}))
    monkeypatch.setattr(st, 'settings_path', lambda *args, **kwargs: tmp_path / 'settings.json')
    monkeypatch.setenv('LITETUI_BACKEND', 'ninfer')
    effective = st.load(tmp_path)
    app = NS(settings=effective, convo_dir=None)
    persist_settings(app, replace(effective, tts_timeout=155))
    saved = json.loads((tmp_path / 'settings.json').read_text())
    assert saved['backend'] == 'lmstudio'
    assert saved['tts_timeout'] == 155
    assert effective.backend == 'ninfer'


def test_environment_explicit_edit_then_repeated_save_keeps_new_preference(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    from litetui import settings as st
    from litetui.settings_runtime import persist_settings
    (tmp_path / 'settings.json').write_text(json.dumps({'backend': 'lmstudio'}))
    monkeypatch.setattr(st, 'settings_path', lambda *args, **kwargs: tmp_path / 'settings.json')
    monkeypatch.setenv('LITETUI_BACKEND', 'ninfer')
    app = NS(settings=st.load(tmp_path), convo_dir=None)
    from copy import deepcopy
    chosen = deepcopy(app.settings)
    chosen.backend = 'codex'
    persist_settings(app, chosen)
    app.settings = chosen
    persist_settings(app, replace(chosen, tts_timeout=155))
    assert json.loads((tmp_path / 'settings.json').read_text())['backend'] == 'codex'


def test_conversation_explicit_env_edit_updates_saved_metadata_for_new_conversation(tmp_path, monkeypatch):
    from copy import deepcopy
    from litetui import settings as st
    from litetui.settings_runtime import persist_settings
    from litetui.settings_service import SettingsService
    monkeypatch.setenv('LITETUI_BACKEND', 'ninfer')
    session = owned_session(tmp_path)
    service = SettingsService(tmp_path, agent_session=session)
    ONE = conversation_id('one')
    service.create_conversation(ONE)
    effective = st.load(tmp_path)
    app = NS(settings=effective, convo_dir=service._paths(ONE)['conversation'].parent,
             _settings_service=service, seat=NS(), _agent_session=session)
    candidate = deepcopy(effective)
    candidate.backend = 'codex'
    outcome = persist_settings(app, candidate)
    assert all(p.saved for p in outcome.persistence)
    assert service.snapshot(ONE).saved.backend == 'codex'
    app.settings = candidate
    new = session.conversation_directory(conversation_id('next'))
    new.mkdir()
    app.convo_dir = new
    LiteTUI._adopt_convo_settings(app, born=True)
    assert convo_settings.load(new).backend == 'codex'
