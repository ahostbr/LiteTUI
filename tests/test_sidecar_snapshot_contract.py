"""Contract: the installed sidecar accepts LiteTUI's real public_snapshot().

Resolve the installed binary independently of conftest's isolated data root.
--check-settings-snapshot validates the payload and exits without a window;
this is not proof that a visible sidecar opens or that a window race is fixed.
A missing installation FAILS with every searched path, never silently skips.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from litetui import paths
from litetui.plugins import sidecar_plugin
from litetui.settings_service import SettingsService
from litetui.sidecar_settings import public_snapshot


def _contract_executable() -> Path:
    override = os.environ.get("LITETUI_SIDECAR_EXE")
    name = "litetui-sidecar.exe" if sys.platform == "win32" else "litetui-sidecar"
    # The project venv lives in the real install, even when pytest runs from a
    # worktree and conftest replaces the data root. An explicit override wins
    # outright: a broken override must not silently validate a different build.
    candidates = [Path(override)] if override else [
        Path(sys.prefix).parent / "bin" / name,
        paths.ROOT / "bin" / name,
        sidecar_plugin._new_window(None).executable,
    ]
    searched = list(dict.fromkeys(candidates))
    for exe in searched:
        if exe.is_file():
            return exe
    pytest.fail(
        "Sidecar snapshot contract cannot run; searched: "
        + ", ".join(str(exe) for exe in searched)
        + ". Install/rebuild the sidecar or set LITETUI_SIDECAR_EXE; this guard must not skip.",
        pytrace=False,
    )


def test_shipped_sidecar_accepts_the_real_settings_snapshot(tmp_path):
    exe = _contract_executable()
    snapshot = public_snapshot(SettingsService(tmp_path).snapshot("c1"))
    result = subprocess.run([str(exe), "--check-settings-snapshot"], input=json.dumps(snapshot).encode(),
                            capture_output=True, timeout=20)
    assert result.returncode != 2, f"{exe} predates --check-settings-snapshot; rebuild and install the sidecar"
    assert result.returncode == 0, f"{exe} rejects LiteTUI's settings snapshot"


def test_contract_finds_real_install_outside_isolated_data(monkeypatch, tmp_path):
    install = tmp_path / "install"
    exe = install / "bin" / ("litetui-sidecar.exe" if sys.platform == "win32" else "litetui-sidecar")
    exe.parent.mkdir(parents=True)
    exe.touch()  # Lookup fixture only: never executed or claimed to be a real binary.
    monkeypatch.delenv("LITETUI_SIDECAR_EXE", raising=False)
    monkeypatch.setattr(sys, "prefix", str(install / ".venv"))
    monkeypatch.setattr(paths, "ROOT", tmp_path / "worktree")
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path / "isolated-data"))
    assert _contract_executable() == exe


def test_missing_contract_binary_fails_with_every_searched_path(monkeypatch, tmp_path):
    monkeypatch.delenv("LITETUI_SIDECAR_EXE", raising=False)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "install" / ".venv"))
    monkeypatch.setattr(paths, "ROOT", tmp_path / "worktree")
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path / "isolated-data"))
    with pytest.raises(pytest.fail.Exception) as error:
        _contract_executable()
    for directory in ("install", "worktree", "isolated-data"):
        assert str(tmp_path / directory / "bin") in str(error.value)
    assert "must not skip" in str(error.value)


def test_missing_explicit_binary_does_not_fall_back(monkeypatch, tmp_path):
    explicit = tmp_path / "missing-explicit-sidecar.exe"
    fallback = tmp_path / "install" / "bin" / ("litetui-sidecar.exe" if sys.platform == "win32" else "litetui-sidecar")
    fallback.parent.mkdir(parents=True)
    fallback.touch()  # A fallback exists, but a broken explicit override must fail.
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "install" / ".venv"))
    monkeypatch.setattr(paths, "ROOT", tmp_path / "install")
    monkeypatch.setenv("LITETUI_SIDECAR_EXE", str(explicit))
    with pytest.raises(pytest.fail.Exception) as error:
        _contract_executable()
    assert str(explicit) in str(error.value)
    assert "searched: " + str(explicit) + "." in str(error.value)
