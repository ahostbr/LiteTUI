"""T1397-C: litetui.gitexe runs Git for Windows' real binary, not the leaking wrapper."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from litetui import gitexe


def _fake_git(tmp_path: Path, *, real: bool = True) -> Path:
    root = tmp_path / "Git"
    for rel in ("cmd", "bin", "usr/bin", "mingw64/bin"):
        (root / rel).mkdir(parents=True)
    (root / "cmd" / "git.exe").write_bytes(b"")
    (root / "bin" / "git.exe").write_bytes(b"")
    if real:
        (root / "mingw64" / "bin" / "git.exe").write_bytes(b"")
    return root


@pytest.fixture
def resolve_with(monkeypatch):
    def setup(which: str | None, *, os_name: str = "nt"):
        monkeypatch.setattr(gitexe.os, "name", os_name)
        monkeypatch.setattr(gitexe.shutil, "which", lambda name: which)
        gitexe._resolve.cache_clear()
    yield setup
    gitexe._resolve.cache_clear()


@pytest.mark.parametrize("found", ["cmd/git.exe", "bin/git.exe", "mingw64/bin/git.exe"])
def test_resolves_real_binary_from_any_git_on_path(tmp_path, resolve_with, found):
    root = _fake_git(tmp_path)
    resolve_with(str(root / found))
    assert gitexe.git_exe() == str(root / "mingw64" / "bin" / "git.exe")
    assert gitexe.git_cmd("status", "-s") == [str(root / "mingw64" / "bin" / "git.exe"), "status", "-s"]


def test_env_prepends_usr_bin_then_mingw64_bin(tmp_path, resolve_with):
    root = _fake_git(tmp_path)
    resolve_with(str(root / "cmd" / "git.exe"))
    env = gitexe.git_env({"Path": r"C:\Windows\system32", "MSYSTEM": "UCRT64"})
    assert env["Path"].split(os.pathsep) == [
        str(root / "usr" / "bin"), str(root / "mingw64" / "bin"), r"C:\Windows\system32"]
    assert env["MSYSTEM"] == "UCRT64"          # an existing value is kept
    assert gitexe.git_env({"PATH": ""})["MSYSTEM"] == "MINGW64"


def test_env_default_copies_this_process_env(tmp_path, resolve_with, monkeypatch):
    root = _fake_git(tmp_path)
    resolve_with(str(root / "cmd" / "git.exe"))
    monkeypatch.setenv("T1397C_MARK", "1")
    env = gitexe.git_env()
    assert env["T1397C_MARK"] == "1"
    path_key = next(k for k in env if k.upper() == "PATH")
    assert env[path_key].startswith(str(root / "usr" / "bin") + os.pathsep)
    assert "T1397C_MARK" in os.environ and env is not os.environ


@pytest.mark.parametrize("case", ["no_real_binary", "no_git", "unknown_layout", "not_windows"])
def test_falls_back_to_plain_git(tmp_path, resolve_with, case):
    root = _fake_git(tmp_path, real=case != "no_real_binary")
    which = {
        "no_real_binary": str(root / "cmd" / "git.exe"),
        "no_git": None,
        "unknown_layout": str(tmp_path / "git.exe"),
        "not_windows": "/usr/bin/git",
    }[case]
    resolve_with(which, os_name="posix" if case == "not_windows" else "nt")
    assert gitexe.git_exe() == "git"
    assert gitexe.git_env() is None
    passed = {"PATH": "x"}
    assert gitexe.git_env(passed) is passed


@pytest.mark.skipif(os.name != "nt", reason="Windows Git layout")
def test_this_machine_runs_a_working_real_git():
    import subprocess
    gitexe._resolve.cache_clear()
    out = subprocess.run(gitexe.git_cmd("--version"), env=gitexe.git_env(), capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0 and out.stdout.startswith("git version")
    if gitexe.git_exe() != "git":
        assert gitexe.git_exe().lower().endswith(r"\mingw64\bin\git.exe")
