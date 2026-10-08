"""T0075: engine launch choices are effective-only, not saved preferences."""
import argparse
from copy import deepcopy
import json
import os
import sys
from types import SimpleNamespace as NS

import pytest

from litetui import launch_options as launch
from litetui import settings as st


def options(*argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', default=None)
    launch.add_arguments(parser)
    return launch.from_args(parser.parse_args(argv))


@pytest.fixture(autouse=True)
def clean_engine_env(monkeypatch):
    monkeypatch.delenv('LITETUI_CODEX_ENGINE', raising=False)


@pytest.mark.parametrize('flag,env,saved,expected', [
    ('native', 'http', False, True),
    ('http', 'native', True, False),
    ('http', 'invalid', True, False),  # a flag replaces the environment default
    (None, 'native', False, True),
    (None, 'http', True, False),
    (None, None, True, True),
    (None, None, False, False),
    (None, '', True, True),
])
def test_cli_engine_precedence_reaches_real_app(tmp_path, monkeypatch, flag, env, saved, expected):
    from litetui import cli
    from litetui.app import LiteTUI
    from litetui.oauth_backend import OAuthBackend

    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'backend': 'codex', 'codex_native_engine': saved}))
    before = path.read_bytes()
    if env is not None:
        monkeypatch.setenv('LITETUI_CODEX_ENGINE', env)
    apps = []

    class NoRunApp(LiteTUI):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            apps.append(self)

        def run(self, **kwargs):
            pass  # no event loop, server, credentials, or model request

    monkeypatch.setattr('litetui.app.LiteTUI', NoRunApp)
    monkeypatch.setattr('litetui.image_viewer.init_image_backend', lambda: None)
    argv = ['litetui', '--backend', 'codex', '--model', 'fixture']
    if flag is not None:
        argv += ['--codex-engine', flag]
    monkeypatch.setattr(sys, 'argv', argv)
    cli.main()
    assert len(apps) == 1
    assert apps[0].settings.codex_native_engine is expected
    assert hasattr(OAuthBackend(apps[0].settings), 'app_server') is expected
    assert path.read_bytes() == before
    assert os.environ.get('LITETUI_CODEX_ENGINE') == env


@pytest.mark.parametrize('argv,env,message', [
    (['--codex-engine', 'invalid'], None, 'invalid choice'),
    (['--codex-engine', ''], None, 'invalid choice'),
    (['--codex-engine'], None, 'expected one argument'),
    ([], 'invalid', 'LITETUI_CODEX_ENGINE must be native or http'),
    ([], 'true', 'LITETUI_CODEX_ENGINE must be native or http'),
    ([], ' ', 'LITETUI_CODEX_ENGINE must be native or http'),
])
def test_invalid_input_refused_before_settings_or_app(monkeypatch, capsys, argv, env, message):
    from litetui import cli
    if env is not None:
        monkeypatch.setenv('LITETUI_CODEX_ENGINE', env)
    monkeypatch.setattr(st, 'load', lambda: pytest.fail('invalid input loaded settings'))
    monkeypatch.setattr(sys, 'argv', ['litetui', *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_help_documents_engine_and_precedence(monkeypatch, capsys):
    from litetui import cli
    monkeypatch.setattr(sys, 'argv', ['litetui', '--help'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert '--codex-engine {native,http}' in help_text
    assert 'LITETUI_CODEX_ENGINE' in help_text


@pytest.mark.parametrize('engine', ['native', 'http'])
def test_supervised_launch_round_trip(engine):
    original = launch.LaunchOptions(codex_engine=engine)
    assert options(*launch.to_argv(original)) == original


def test_programmatic_invalid_engine_is_rejected():
    with pytest.raises(ValueError, match='codex_engine must be native or http'):
        launch.LaunchOptions(codex_engine='invalid').overrides(st.Settings(), 'codex', None)


@pytest.mark.parametrize('source', ['flag', 'environment'])
@pytest.mark.parametrize('saved', [False, True])
def test_resume_and_unrelated_save_do_not_persist_engine(tmp_path, monkeypatch, source, saved):
    from litetui.app import LiteTUI
    from litetui import convo_settings
    from litetui.settings_runtime import capture_invocation, persist_settings
    from litetui.settings_service import SettingsService
    from litetui.agent_launch_context import create

    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'backend': 'codex', 'codex_native_engine': saved}))
    engine = 'http' if saved else 'native'
    if source == 'environment':
        monkeypatch.setenv('LITETUI_CODEX_ENGINE', engine)
        chosen = options()
    else:
        chosen = options('--codex-engine', engine)
    with create(tmp_path, 'EngineTest', agent_id='11111111-1111-4111-8111-111111111111',
                backend='codex', model='fixture', thinking_level='high') as session:
        cid = session.initial_conversation_id
        service = SettingsService(tmp_path, agent_session=session)
        service.create_conversation(cid)
        directory = session.conversation_directory(cid)
        original = directory.joinpath('settings.json').read_bytes()
        effective = st.load(tmp_path)
        overlay = chosen.overrides(effective, 'codex', None)
        app = NS(settings=effective, convo_dir=directory, _settings_service=service,
                 _agent_session=session, _launch_overrides=overlay,
                 _invocation_saved_values=capture_invocation(effective, overlay),
                 _backend=NS(name='codex'), _model_id='', available_models=[], seat=NS(),
                 chosen_tool_profile='autonomous',
                 _system=lambda text: None, _adopt_convo_backend=lambda cs: None)
        LiteTUI._adopt_convo_settings(app, born=False)
        assert app.settings.codex_native_engine is not saved
        candidate = deepcopy(app.settings)
        candidate.tts_timeout = 123
        result = persist_settings(app, candidate)
        assert all(p.saved for p in result.persistence)
        assert service.snapshot(cid).saved.codex_native_engine is saved
        assert directory.joinpath('settings.json').read_bytes() == original
        assert json.loads(path.read_text())['codex_native_engine'] is saved
        assert service.snapshot(cid).saved.tts_timeout == 123
        # A newly born conversation uses saved preferences, not the override.
        app.convo_dir = session.conversation_directory('33333333-3333-4333-8333-333333333333')
        app.convo_dir.mkdir()
        LiteTUI._adopt_convo_settings(app, born=True)
        assert convo_settings.load(app.convo_dir).execution['codex_native_engine'] is saved
        assert app.settings.codex_native_engine is not saved


@pytest.mark.parametrize('saved', [False, True])
def test_default_scope_save_keeps_override_effective_only(tmp_path, saved):
    from litetui.settings_runtime import capture_invocation, persist_settings
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'codex_native_engine': saved}))
    effective = st.load(tmp_path)
    overlay = options('--codex-engine', 'http' if saved else 'native').overrides(effective, 'codex', None)
    app = NS(settings=effective, convo_dir=None,
             _invocation_saved_values=capture_invocation(effective, overlay))
    candidate = deepcopy(effective)
    candidate.tts_timeout = 123
    result = persist_settings(app, candidate)
    assert result.persistence[0].saved
    assert json.loads(path.read_text())['codex_native_engine'] is saved
    assert effective.codex_native_engine is not saved
