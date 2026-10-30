"""Pure engine-management launch gates; no app, hardware or server startup."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from litetui import launch_options as launch


def options(*argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend')
    parser.add_argument('--model')
    parser.add_argument('--prompt')
    launch.add_arguments(parser)
    return launch.from_args(parser.parse_args(argv))


@pytest.fixture(autouse=True)
def no_engine_environment(monkeypatch):
    monkeypatch.delenv('LITETUI_CODEX_ENGINE', raising=False)


@pytest.mark.parametrize('backend', ['ninfer', 'strata'])
def test_management_roundtrip_is_connect_only_and_grants_no_start_or_load(backend):
    original = options('--engine-management', '--backend', backend, '--model', 'selected 雪',
                       '--server-mode', 'connect')
    assert original.engine_management is True
    assert original.server_mode == 'connect' and original.load_model is False
    argv = launch.to_argv(original)
    assert argv.count('--engine-management') == 1
    assert '--start-server' not in argv and '--load-model' not in argv and '--prompt' not in argv
    assert options('--backend', backend, '--model', 'selected 雪', *argv) == original


def test_ordinary_launch_and_positional_options_keep_the_old_default():
    original = launch.LaunchOptions(None, 'connect', False, None, None, None,
                                    None, None, None, 5, None)
    assert original.engine_management is False
    assert '--engine-management' not in launch.to_argv(original)
    assert options('--backend', 'custom', *launch.to_argv(original)) == original


@pytest.mark.parametrize('overrides,backend,model', [
    ({'engine_management': 1}, 'strata', 'model'),
    ({'engine_management': 'true'}, 'strata', 'model'),
    ({}, 'custom', 'model'),
    ({}, None, 'model'),
    ({}, 'ninfer', None),
    ({}, 'ninfer', ''),
    ({}, 'ninfer', '   '),
    ({}, 'ninfer', 'bad\x00model'),
    ({}, 'ninfer', '雪' * 342),
    ({'server_mode': 'auto'}, 'ninfer', 'model'),
    ({'server_mode': 'start'}, 'ninfer', 'model'),
    ({'load_model': True}, 'ninfer', 'model'),
    ({'load_model': 1}, 'ninfer', 'model'),
])
def test_invalid_programmatic_management_refuses_before_overlay_or_gpu(monkeypatch, overrides, backend, model):
    from litetui import gpu_gate

    def forbidden(*_args, **_kwargs):
        pytest.fail('Invalid management launch reached hardware')

    monkeypatch.setattr(gpu_gate, 'is_rtx_5090', forbidden)
    value = launch.LaunchOptions(**{'engine_management': True, 'server_mode': 'connect', **overrides})
    before = asdict(value)
    with pytest.raises(ValueError):
        value.overrides(SimpleNamespace(), backend, model)
    assert asdict(value) == before


@pytest.mark.parametrize('backend', ['ninfer', 'strata'])
def test_management_flag_is_invocation_state_and_creates_no_settings_overlay(monkeypatch, backend):
    from litetui import gpu_gate
    monkeypatch.setattr(gpu_gate, 'is_rtx_5090', lambda: True)
    value = options('--engine-management', '--backend', backend, '--model', 'selected',
                    '--server-mode', 'connect')
    settings = SimpleNamespace()
    assert value.overrides(settings, backend, 'selected') == {}
    assert vars(settings) == {}


BASE = ['--engine-management', '--rpc', '--hold-restored-input',
        '--backend', 'strata', '--model', 'selected', '--server-mode', 'connect']


@pytest.mark.parametrize('argv,message', [
    ([v for v in BASE if v != '--rpc'], 'requires --rpc'),
    ([v for v in BASE if v != '--hold-restored-input'], 'requires --rpc and --hold-restored-input'),
    ([*BASE, '--prompt', ''], 'cannot carry --prompt'),
    ([*BASE, '--prompt', 'never send'], 'cannot carry --prompt'),
    ([*BASE, '--load-model'], 'without automatic start/load'),
    ([*BASE[:-2], '--start-server'], 'without automatic start/load'),
    (BASE[:-2], 'without automatic start/load'),
    ([*BASE[:-2], '--server-mode', 'auto'], 'without automatic start/load'),
    ([*BASE, '--backend', 'custom'], 'requires an explicit --backend'),
    ([*BASE, '--model', ''], 'requires an explicit --model'),
    ([*BASE, '--model', '雪' * 342], 'requires an explicit --model'),
    ([*BASE, '--export-conversation', 'ignored.jsonl'], 'cannot be combined with conversation export'),
    ([*BASE, '--export-output', 'ignored.md'], 'cannot be combined with conversation export'),
])
def test_cli_refusal_precedes_settings_cwd_agent_creation_and_hardware(tmp_path, monkeypatch, capsys, argv, message):
    from litetui import cli, gpu_gate, settings

    def forbidden(*_args, **_kwargs):
        pytest.fail('Refused CLI reached settings, cwd or hardware')

    monkeypatch.setattr(settings, 'load', forbidden)
    monkeypatch.setattr(gpu_gate, 'is_rtx_5090', forbidden)
    monkeypatch.setattr(os, 'chdir', forbidden)
    monkeypatch.setattr(sys, 'argv', ['litetui', *argv, '--cwd', str(tmp_path / 'never-entered')])
    before = set(tmp_path.rglob('*'))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    assert set(tmp_path.rglob('*')) == before


@pytest.mark.parametrize('backend', ['ninfer', 'strata'])
def test_valid_cli_reaches_settings_only_after_management_flags_validate(monkeypatch, backend):
    from litetui import cli, settings

    class Validated(Exception):
        pass

    def reached_settings():
        raise Validated

    monkeypatch.setattr(settings, 'load', reached_settings)
    monkeypatch.setattr(sys, 'argv', ['litetui', *BASE, '--backend', backend])
    with pytest.raises(Validated):
        cli.main()


@pytest.mark.parametrize('mode,load', [('start', False), ('auto', False), ('connect', True)])
def test_preparation_rechecks_management_before_granting_launch_consent(mode, load):
    async def forbidden(*_args, **_kwargs):
        pytest.fail('Invalid management preparation reached a backend')

    app = SimpleNamespace(
        backend=SimpleNamespace(name='ninfer', ensure_running=forbidden, start_engine=forbidden),
        _cli_initial_model='selected', _explicit_launch_load=False,
        _launch_options=launch.LaunchOptions(engine_management=True, server_mode=mode, load_model=load))
    with pytest.raises(ValueError, match='without automatic start/load'):
        asyncio.run(launch.prepare(app))
    assert app._explicit_launch_load is False
