"""T530: the bash tool runs a REAL bash on Windows when the box has one.

Ryan 2026-09-08, after the async test: the model chose the bash tool for
`sleep 45 && echo PROMOTED > file` and cmd.exe answered "'sleep' is not
recognized". Git for Windows ships bash with coreutils; the tool now spawns
it in argv form and its description says so, or says cmd.exe when there is
no bash so the model is pointed at PowerShell as before.

Arms:
  * resolver: never the WSL launcher (System32) nor the Store alias
    (WindowsApps), even when those are the only `bash` on PATH
  * spawn: with a bash present, tool_bash runs it with -c and sleep/pipes/
    redirection work; the exit code survives
  * description: derived, names the shell that will run; the cmd.exe wording
    only when no bash is found
  * the background gate is untouched: `background` stays on the bash schema
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from litetui import tasks as tasks_mod
from litetui.plugins import core_tools as ct

WIN = os.name == "nt"


@pytest.fixture
def fresh_resolver(monkeypatch):
    """Reset the once-only resolver so each arm sees its own world."""
    monkeypatch.setattr(ct, "_BASH_RESOLVED", False)
    monkeypatch.setattr(ct, "_BASH_EXE", None)
    yield
    monkeypatch.setattr(ct, "_BASH_RESOLVED", False)
    monkeypatch.setattr(ct, "_BASH_EXE", None)


@pytest.mark.skipif(not WIN, reason="the look-alike bashes are a Windows shape")
def test_resolver_refuses_the_wsl_launcher_and_the_store_stub(fresh_resolver, monkeypatch):
    monkeypatch.setattr(ct, "_BASH_WELL_KNOWN", ())
    for stub in (r"C:\Windows\System32\bash.exe",
                 r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\bash.exe"):
        monkeypatch.setattr(ct, "_BASH_RESOLVED", False)
        monkeypatch.setattr(ct, "_BASH_EXE", None)
        monkeypatch.setattr(ct.shutil, "which", lambda name, _s=stub: _s)
        assert ct.bash_exe() is None, f"{stub} was accepted as a bash"


@pytest.mark.skipif(not WIN, reason="well-known Git paths are a Windows shape")
def test_resolver_prefers_a_well_known_git_bash(fresh_resolver, monkeypatch, tmp_path):
    fake = tmp_path / "bash.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr(ct, "_BASH_WELL_KNOWN", (str(fake),))
    monkeypatch.setattr(ct.shutil, "which", lambda name: r"C:\Windows\System32\bash.exe")
    assert ct.bash_exe() == str(fake)


def test_no_bash_means_cmd_wording_and_the_old_spawn(fresh_resolver, monkeypatch):
    monkeypatch.setattr(ct, "_BASH_WELL_KNOWN", ())
    monkeypatch.setattr(ct.shutil, "which", lambda name: None)
    assert ct.bash_exe() is None
    desc = ct.bash_spec()["function"]["description"].lower()
    assert "cmd.exe" in desc and "prefer" in desc and "powershell" in desc
    seen = {}
    monkeypatch.setattr(ct, "_run_shell", lambda argv, *, shell, timeout: seen.update(argv=argv, shell=shell) or "ok")
    ct.tool_bash({"command": "echo hi"})
    assert seen == {"argv": "echo hi", "shell": True}


def test_with_a_bash_the_spawn_is_argv_and_the_description_says_so(fresh_resolver, monkeypatch):
    monkeypatch.setattr(ct, "_BASH_RESOLVED", True)
    monkeypatch.setattr(ct, "_BASH_EXE", r"C:\fake\Git\bin\bash.exe")
    desc = ct.bash_spec()["function"]["description"]
    assert r"C:\fake\Git\bin\bash.exe" in desc and "sleep, grep, sed" in desc
    assert "PREFER THE `powershell` TOOL" not in desc
    seen = {}
    monkeypatch.setattr(ct, "_run_shell", lambda argv, *, shell, timeout: seen.update(argv=argv, shell=shell) or "ok")
    ct.tool_bash({"command": "sleep 1 && echo x > f"})
    assert seen == {"argv": [r"C:\fake\Git\bin\bash.exe", "-c", "sleep 1 && echo x > f"], "shell": False}


@pytest.mark.skipif(ct.bash_exe() is None, reason="no real bash on this box")
def test_a_real_bash_runs_sleep_pipes_and_redirection(tmp_path):
    out = tmp_path / "t530.txt"
    r = ct.tool_bash({"command": f"sleep 1 && echo t530-ok | tr a-z A-Z > '{out.as_posix()}' && cat '{out.as_posix()}'"})
    assert "T530-OK" in r, r
    assert out.read_text().strip() == "T530-OK"
    assert "[command exited with code 3]" in ct.tool_bash({"command": "exit 3"})


def test_background_stays_on_the_bash_schema():
    """The T517 gate reads the schema's properties; the templated description
    must not have moved or dropped them."""
    tasks_mod.backgroundable.cache_clear()
    assert tasks_mod.backgroundable("bash") is True
    assert "background" in ct.BASH_SPEC["function"]["parameters"]["properties"]
