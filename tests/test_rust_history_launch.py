"""Early, side-effect-free admission for the native saved-history protocol."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import pytest

from litetui import launch_options as launch


def options(*argv: str) -> launch.LaunchOptions:
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend')
    parser.add_argument('--model')
    parser.add_argument('--prompt')
    launch.add_arguments(parser)
    return launch.from_args(parser.parse_args(argv))


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LITETUI_CODEX_ENGINE', raising=False)


def test_protocol_choice_roundtrips_without_granting_start_or_load() -> None:
    original = options('--history-projection-version', '1')
    assert original.history_projection_version == 1
    assert original.load_model is False and original.server_mode == 'auto'
    argv = launch.to_argv(original)
    assert argv.count('--history-projection-version') == 1
    assert '--load-model' not in argv and '--start-server' not in argv
    assert options(*argv) == original
    settings = SimpleNamespace()
    assert original.overrides(settings, 'custom', 'model') == {}
    assert vars(settings) == {}


def test_unset_preserves_existing_positional_launch_contract() -> None:
    old = launch.LaunchOptions(None, 'connect', False, None, None, None,
                              None, None, None, 5, None, False)
    assert old.history_projection_version is None
    assert '--history-projection-version' not in launch.to_argv(old)
    assert options('--backend', 'custom', *launch.to_argv(old)) == old


@pytest.mark.parametrize('value', [True, False, 0, 2, -1, '1', 1.0, []])
def test_invalid_programmatic_version_refuses_before_overlay_hardware_and_backend(
    monkeypatch: pytest.MonkeyPatch, value: Any,
) -> None:
    from litetui import gpu_gate

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail('Invalid history projection reached a side effect')

    monkeypatch.setattr(gpu_gate, 'is_rtx_5090', forbidden)
    original = launch.LaunchOptions(history_projection_version=value)
    before = asdict(original)
    with pytest.raises(ValueError, match='history.projection.version'):
        original.overrides(SimpleNamespace(), 'ninfer', 'model')
    with pytest.raises(ValueError, match='history.projection.version'):
        launch.to_argv(original)
    app = SimpleNamespace(_launch_options=original, backend=SimpleNamespace(name='ninfer', ensure_running=forbidden))
    with pytest.raises(ValueError, match='history.projection.version'):
        asyncio.run(launch.prepare(app))
    assert asdict(original) == before


BASE = ['--history-projection-version', '1', '--rpc', '--hold-restored-input']


@pytest.mark.parametrize('argv,message', [
    (BASE[:2], 'requires --rpc and --hold-restored-input'),
    ([*BASE[:2], '--rpc'], 'requires --rpc and --hold-restored-input'),
    ([*BASE[:2], '--hold-restored-input'], 'requires --rpc'),
    ([*BASE, '--export-conversation', 'never.jsonl'], 'cannot be combined with conversation export'),
    ([*BASE, '--export-output', 'never.md'], 'cannot be combined with conversation export'),
    ([*BASE, '--history-projection-version', '0'], 'invalid choice'),
    ([*BASE, '--history-projection-version', '2'], 'invalid choice'),
    ([*BASE, '--history-projection-version', 'true'], 'invalid int value'),
    ([*BASE, '--history-projection-version'], 'expected one argument'),
])
def test_cli_refusal_precedes_settings_cwd_and_agent_effects(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any, argv: list[str], message: str,
) -> None:
    from litetui import cli, gpu_gate, settings

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail('Refused history launch reached settings, cwd or hardware')

    monkeypatch.setattr(settings, 'load', forbidden)
    monkeypatch.setattr(gpu_gate, 'is_rtx_5090', forbidden)
    monkeypatch.setattr(os, 'chdir', forbidden)
    monkeypatch.setattr(sys, 'argv', ['litetui', *argv, '--cwd', str(tmp_path / 'absent')])
    before = set(tmp_path.rglob('*'))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    assert set(tmp_path.rglob('*')) == before


@pytest.mark.parametrize('extra', [[], ['--prompt', 'explicit draft'], ['--prompt', '']])
def test_valid_cli_enters_settings_after_protocol_admission(
    monkeypatch: pytest.MonkeyPatch, extra: list[str],
) -> None:
    from litetui import cli, settings

    class Validated(Exception):
        pass

    def settings_boundary() -> Any:
        raise Validated

    monkeypatch.setattr(settings, 'load', settings_boundary)
    monkeypatch.setattr(sys, 'argv', ['litetui', *BASE, *extra])
    with pytest.raises(Validated):
        cli.main()
