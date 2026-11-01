"""Explicit Work startup negotiation is validated before runtime construction."""
from __future__ import annotations

import argparse
import sys

import pytest

from litetui import cli, launch_options
from litetui.launch_options import LaunchOptions


def arguments(extra=()):
    parser = argparse.ArgumentParser()
    for field in ("backend", "model", "prompt"):
        parser.add_argument("--" + field)
    parser.add_argument("--rpc", action="store_true")
    parser.add_argument("--hold-restored-input", action="store_true")
    launch_options.add_arguments(parser)
    return parser.parse_args(extra)


@pytest.mark.parametrize("version", [True, False, 0, 2, "1", 1.0])
def test_work_version_is_exact_integer_before_serialization(version):
    options = LaunchOptions(work_lifecycle_version=version)
    with pytest.raises(ValueError, match="work-lifecycle-version"):
        launch_options.to_argv(options)


def test_work_flag_roundtrips_without_changing_legacy_default(monkeypatch):
    monkeypatch.delenv("LITETUI_CODEX_ENGINE", raising=False)
    legacy = launch_options.from_args(arguments())
    assert legacy.work_lifecycle_version is None
    assert "--work-lifecycle-version" not in launch_options.to_argv(legacy)
    selected = launch_options.from_args(arguments(["--rpc", "--hold-restored-input", "--work-lifecycle-version", "1"]))
    assert selected.work_lifecycle_version == 1
    serialized = launch_options.to_argv(selected)
    assert serialized[serialized.index("--work-lifecycle-version") + 1] == "1"
    assert launch_options.from_args(arguments(["--rpc", "--hold-restored-input", *serialized])) == selected


@pytest.mark.parametrize("flags", [[], ["--rpc"], ["--hold-restored-input"],
    ["--rpc", "--hold-restored-input", "--prompt", "premature prompt"],
    ["--rpc", "--hold-restored-input", "--export-conversation", "saved"]])
def test_cli_rejects_incompatible_work_startup_before_ownership_or_runtime(monkeypatch, capsys, flags):
    monkeypatch.setattr(sys, "argv", ["litetui", "--work-lifecycle-version", "1", *flags])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err
