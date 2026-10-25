"""/agent-new NAME and /new NAME: create an agent home and continue this window as that agent."""
import sys
from types import SimpleNamespace

import pytest

from litetui import paths, settings
from litetui.agent_launch_context import ordinary
from litetui.plugins import convo


def _app(session, **extra):
    said = []
    app = SimpleNamespace(
        _agent_session=session, _rpc=False, _spawned_marker=False, _chat_running=lambda: False,
        system_message=said.append, exit=lambda: said.append('EXIT'), said=said, **extra)
    return app


@pytest.fixture
def seat(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    cfg = settings.Settings(default_model='home-model')
    with ordinary(tmp_path, cfg) as session:
        yield session


def test_a_new_name_relaunches_this_window_as_a_created_agent(seat):
    app = _app(seat)
    convo._cmd_agent_new(app, '/agent-new', '  Scout  ')
    argv = app._relaunch_argv
    assert argv[:5] == [sys.executable, '-m', 'litetui.cli', '--create-agent', 'Scout']
    auth = seat.authority
    flags = dict(zip(argv[5::2], argv[6::2]))
    assert flags == {'--agent-id': flags['--agent-id'], '--backend': auth.backend, '--model': 'home-model',
                     '--thinking-level': auth.thinking_level}
    assert flags['--agent-id'] != auth.agent_id
    assert app.said[-1] == 'EXIT'


def test_slash_new_with_a_name_is_the_same_command_and_plain_new_is_not(seat):
    named = _app(seat)
    convo._cmd_new(named, '/new', 'Scout')
    assert named._relaunch_argv[4] == 'Scout'
    for typed in ('/clear', '/reset'):      # only /new takes a name
        other = _app(seat, conversation=SimpleNamespace(clear=lambda: (_ for _ in ()).throw(RuntimeError('plain path'))))
        with pytest.raises(RuntimeError, match='plain path'):
            convo._cmd_new(other, typed, 'Scout')
        assert not hasattr(other, '_relaunch_argv')
    plain = _app(seat, conversation=SimpleNamespace(clear=lambda: (_ for _ in ()).throw(RuntimeError('plain path'))))
    with pytest.raises(RuntimeError, match='plain path'):
        convo._cmd_new(plain, '/new', '   ')


@pytest.mark.parametrize('arg,fragment', [('', 'Usage'), ('a/b', 'cannot be an agent name'),
                                          ('CON', 'cannot be an agent name')])
def test_bad_names_change_nothing(seat, arg, fragment):
    app = _app(seat)
    convo._cmd_agent_new(app, '/agent-new', arg)
    assert fragment in app.said[0] and not hasattr(app, '_relaunch_argv') and 'EXIT' not in app.said


def test_an_existing_agent_is_not_recreated(seat):
    app = _app(seat)
    convo._cmd_agent_new(app, '/agent-new', seat.authority.name.upper())    # case-insensitive, like the catalog
    assert 'already exists' in app.said[0] and not hasattr(app, '_relaunch_argv')


@pytest.mark.parametrize('what,fragment', [
    ({'_chat_running': lambda: True}, 'current turn'),
    ({'_rpc': True}, 'hand-launched'),
    ({'_spawned_marker': True}, 'hand-launched'),
])
def test_guards(seat, what, fragment):
    app = _app(seat)
    for key, value in what.items():
        setattr(app, key, value)
    convo._cmd_agent_new(app, '/agent-new', 'Scout')
    assert fragment in app.said[0] and not hasattr(app, '_relaunch_argv')


def test_a_seat_without_a_model_is_asked_to_choose_one_first(seat):
    auth = seat.authority
    seat.update_execution(backend=auth.backend, model=None, thinking_level=auth.thinking_level)
    app = _app(seat)
    convo._cmd_agent_new(app, '/agent-new', 'Scout')
    assert 'Choose a model first' in app.said[0] and not hasattr(app, '_relaunch_argv')


def test_the_cli_relaunches_only_after_this_window_has_released_its_home(tmp_path, monkeypatch):
    import subprocess

    from litetui import app as app_module
    from litetui import cli, harness, image_viewer
    from litetui.agent_launch_context import acquire
    from litetui.seat_authority import AGENT_SHELL_MARKERS

    for name in (*AGENT_SHELL_MARKERS, harness.SPAWN_IDENTITY_MARKER, "LITESUITE_CANVAS_AGENT",
                 "LITEHARNESS_TIER", "LITEHARNESS_AGENT_NAME", "LITEHARNESS_SPAWNED_BY"):
        monkeypatch.delenv(name, raising=False)
    cfg = settings.Settings()
    cfg.default_model = "fixture-model"
    cfg.user_name_asked = True
    cfg.user_name = "Fixture"
    monkeypatch.setattr(settings, "load", lambda: cfg)
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    monkeypatch.setattr(image_viewer, "init_image_backend", lambda: None)
    monkeypatch.setattr(cli.sys, "argv", ["litetui"])
    monkeypatch.setattr(harness, "harness_disabled", lambda: True)
    seen = []

    class OfflineApp(app_module.LiteTUI):
        def run(self, **kwargs):
            self._relaunch_argv = ["child", "--create-agent", "Scout"]

    def call(argv):
        # By now the parent's home must be free: the child will want its own lease, and this one proves it.
        with acquire(tmp_path, "LiteTUI") as reopened:
            seen.append((argv, reopened.authority.name))
        return 7

    monkeypatch.setattr(app_module, "LiteTUI", OfflineApp)
    monkeypatch.setattr(subprocess, "call", call)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 7
    assert seen == [(["child", "--create-agent", "Scout"], "LiteTUI")]


def test_the_relaunch_arguments_are_accepted_by_the_cli_and_create_the_agent(seat, tmp_path, monkeypatch):
    """The command line /agent-new builds must be exactly what `litetui --create-agent` takes."""
    from litetui import app as app_module
    from litetui import cli, harness, image_viewer
    from litetui.seat_authority import AGENT_SHELL_MARKERS

    app = _app(seat)
    convo._cmd_agent_new(app, '/agent-new', 'Scout')
    argv = app._relaunch_argv
    for name in (*AGENT_SHELL_MARKERS, harness.SPAWN_IDENTITY_MARKER, "LITESUITE_CANVAS_AGENT",
                 "LITEHARNESS_TIER", "LITEHARNESS_AGENT_NAME", "LITEHARNESS_SPAWNED_BY"):
        monkeypatch.delenv(name, raising=False)
    cfg = settings.Settings()
    cfg.user_name_asked = True
    cfg.user_name = "Fixture"
    monkeypatch.setattr(settings, "load", lambda: cfg)
    monkeypatch.setattr(image_viewer, "init_image_backend", lambda: None)
    monkeypatch.setattr(harness, "harness_disabled", lambda: True)
    monkeypatch.setattr(cli.sys, "argv", ["litetui", *argv[3:]])    # what `python -m litetui <rest>` parses
    created = []

    class OfflineApp(app_module.LiteTUI):
        def run(self, **kwargs):
            auth = self._agent_session.authority
            created.append((auth.name, auth.backend, auth.model, auth.thinking_level))

    monkeypatch.setattr(app_module, "LiteTUI", OfflineApp)
    cli.main()
    auth = seat.authority
    assert created == [('Scout', auth.backend, 'home-model', auth.thinking_level)]
    assert (tmp_path / '.agents' / 'Scout' / 'settings.json').is_file()


# ── T0703: both commands dropped the window straight back to the shell ─────────────────────────
# Root cause: the relaunch ran `python -m litetui ...`, and the package has no __main__, so the child
# exited at once ("No module named litetui.__main__"). Every test above stubbed the child away.

@pytest.mark.parametrize('typed', ['/new', '/agent-new'])
def test_the_relaunch_command_line_starts_a_real_litetui_process(seat, typed):
    """Run the exact interpreter and module the command builds; only the tail is swapped for --version."""
    import subprocess

    app = _app(seat)
    convo._cmd_new(app, typed, 'Scout') if typed == '/new' else convo._cmd_agent_new(app, typed, 'Scout')
    argv = app._relaunch_argv
    done = subprocess.run([*argv[:3], '--version'], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith('litetui '), (done.stdout, done.stderr)


@pytest.mark.parametrize('typed', ['/new Scout', '/agent-new Scout'])
def test_a_failure_inside_the_command_is_said_in_the_app_and_the_app_stays_up(seat, monkeypatch, typed):
    from litetui import agent_store

    def broken(self):
        raise RuntimeError('catalog exploded')

    monkeypatch.setattr(agent_store.AgentStore, 'list_agents', broken)
    app = _app(seat)
    name, _, arg = typed.partition(' ')
    (convo._cmd_new if name == '/new' else convo._cmd_agent_new)(app, name, arg)
    assert 'RuntimeError: catalog exploded' in app.said[0] and 'nothing was changed' in app.said[0]
    assert app._relaunch_argv is None and 'EXIT' not in app.said


@pytest.mark.parametrize('outcome,fragment,code', [
    (OSError('cannot start'), 'could not continue this window', 1),
    (3, 'ended with exit code 3', 3),
])
def test_a_relaunch_that_fails_is_reported_on_the_terminal(tmp_path, monkeypatch, capsys, outcome, fragment, code):
    import subprocess

    from litetui import app as app_module
    from litetui import cli, harness, image_viewer
    from litetui.seat_authority import AGENT_SHELL_MARKERS

    for name in (*AGENT_SHELL_MARKERS, harness.SPAWN_IDENTITY_MARKER, "LITESUITE_CANVAS_AGENT",
                 "LITEHARNESS_TIER", "LITEHARNESS_AGENT_NAME", "LITEHARNESS_SPAWNED_BY"):
        monkeypatch.delenv(name, raising=False)
    cfg = settings.Settings()
    cfg.default_model = "fixture-model"
    cfg.user_name_asked = True
    cfg.user_name = "Fixture"
    monkeypatch.setattr(settings, "load", lambda: cfg)
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    monkeypatch.setattr(image_viewer, "init_image_backend", lambda: None)
    monkeypatch.setattr(cli.sys, "argv", ["litetui"])
    monkeypatch.setattr(harness, "harness_disabled", lambda: True)

    class OfflineApp(app_module.LiteTUI):
        def run(self, **kwargs):
            self._relaunch_argv = ["child"]

    def call(argv):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(app_module, "LiteTUI", OfflineApp)
    monkeypatch.setattr(subprocess, "call", call)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == code
    assert fragment in capsys.readouterr().err
