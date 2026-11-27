"""T530: the bash tool runs a REAL bash on Windows when the box has one.

the user 2026-09-08, after the async test: the model chose the bash tool for
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
    """Reset the once-only resolver so each arm sees its own world (and not this box's
    CLAUDE_CODE_GIT_BASH_PATH, which T1397-B makes the first choice)."""
    monkeypatch.setattr(ct, "_BASH_RESOLVED", False)
    monkeypatch.setattr(ct, "_BASH_EXE", None)
    monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
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
    monkeypatch.setattr(ct, "_run_shell", lambda argv, *, shell, timeout, agent_id: seen.update(argv=argv, shell=shell) or "ok")
    ct.tool_bash({"command": "echo hi"})
    assert seen == {"argv": "echo hi", "shell": True}


def test_with_a_bash_the_spawn_is_argv_and_the_description_says_so(fresh_resolver, monkeypatch):
    monkeypatch.setattr(ct, "_BASH_RESOLVED", True)
    monkeypatch.setattr(ct, "_BASH_EXE", r"C:\fake\Git\bin\bash.exe")
    desc = ct.bash_spec()["function"]["description"]
    assert r"C:\fake\Git\bin\bash.exe" in desc and "sleep, grep, sed" in desc
    assert "BACKUP ONLY on Windows" in desc and "prefer `powershell`" in desc
    seen = {}
    monkeypatch.setattr(ct, "_run_shell", lambda argv, *, shell, timeout, agent_id: seen.update(argv=argv, shell=shell) or "ok")
    ct.tool_bash({"command": "sleep 1 && echo x > f"})
    assert seen == {"argv": [r"C:\fake\Git\bin\bash.exe", "-c", "sleep 1 && echo x > f"], "shell": False}


@pytest.mark.skipif(ct.bash_exe() is None, reason="no real bash on this box")
def test_a_real_bash_runs_sleep_pipes_and_redirection(tmp_path):
    out = tmp_path / "t530.txt"
    r = ct.tool_bash({"command": f"sleep 1 && echo t530-ok | tr a-z A-Z > '{out.as_posix()}' && cat '{out.as_posix()}'"})
    assert "T530-OK" in r, r
    assert out.read_text().strip() == "T530-OK"
    assert "[command exited with code 3]" in ct.tool_bash({"command": "exit 3"})


# ── T1397-B: never Git's leaking launcher (Ryan 2026-11-26 21:1x: "fix this for good") ─────────────


def _git_tree(root: Path) -> Path:
    """A Git for Windows layout: the launcher bin\\bash.exe, the real usr\\bin\\bash.exe, mingw64\\bin."""
    for rel in ("bin/bash.exe", "usr/bin/bash.exe"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"")
    (root / "mingw64" / "bin").mkdir(parents=True, exist_ok=True)
    return root


@pytest.mark.skipif(not WIN, reason="Git for Windows paths")
def test_resolution_order_env_var_then_usr_bin_then_path_and_a_launcher_is_never_chosen(
        fresh_resolver, monkeypatch, tmp_path):
    a, b = _git_tree(tmp_path / "GitA"), _git_tree(tmp_path / "GitB")
    real_a, real_b = str(a / "usr" / "bin" / "bash.exe"), str(b / "usr" / "bin" / "bash.exe")

    def resolve(env=None, well_known=(), which=None):
        monkeypatch.setattr(ct, "_BASH_RESOLVED", False)
        monkeypatch.setattr(ct, "_BASH_EXE", None)
        monkeypatch.setattr(ct, "_BASH_WELL_KNOWN", well_known)
        monkeypatch.setattr(ct.shutil, "which", lambda name: which)
        if env is None:
            monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
        else:
            monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", env)
        return ct.bash_exe()

    assert resolve(env=real_a, well_known=(real_b,)) == real_a, "the env var wins"
    assert resolve(env=f'"{real_a}"', well_known=(real_b,)) == real_a, "quoted the way a settings file writes it"
    assert resolve(env=str(a / "bin" / "bash.exe"), well_known=(real_b,)) == real_a, "a launcher -> its usr\\bin"
    assert resolve(env=str(tmp_path / "gone.exe"), well_known=(real_b,)) == real_b, "absent -> the next choice"
    assert resolve(well_known=(str(tmp_path / "none.exe"),), which=str(b / "bin" / "bash.exe")) == real_b, \
        "a launcher found on PATH -> its usr\\bin"
    assert resolve(env=r"C:\Windows\System32\bash.exe", which=None) is None, "the stubs stay excluded"


def test_the_shipped_well_known_list_names_no_launcher():
    assert ct._BASH_WELL_KNOWN == (r"C:\Program Files\Git\usr\bin\bash.exe",
                                   r"C:\Program Files (x86)\Git\usr\bin\bash.exe")


@pytest.mark.skipif(not WIN, reason="Git for Windows paths")
def test_bash_env_is_what_the_launcher_gave_derived_from_the_exe(tmp_path):
    root = _git_tree(tmp_path / "Git")
    exe = str(root / "usr" / "bin" / "bash.exe")
    env = ct.bash_env(exe, {"PATH": r"C:\Windows\System32", "MSYSTEM": "MSYS", "KEEP": "1"})
    assert env["PATH"] == os.pathsep.join([str(root / "mingw64" / "bin"), str(root / "usr" / "bin"),
                                           r"C:\Windows\System32"])
    assert env["MSYSTEM"] == "MINGW64" and env["KEEP"] == "1"
    base = {"PATH": r"C:\x"}
    assert ct.bash_env(r"C:\Program Files\PowerShell\7\pwsh.exe", base) is base, "not a Git bash: untouched"
    assert ct.bash_env(None, base) is base, "the cmd.exe string form: untouched"


def test_the_tool_and_the_monitor_hand_the_bash_env_to_the_child(fresh_resolver, monkeypatch):
    seen = {}
    monkeypatch.setattr(ct, "bash_env", lambda exe, env: seen.setdefault("exe", exe) and {**env, "T1397": "1"})

    class Proc:
        returncode = 0
        pid = 1

        def communicate(self, timeout=None):
            return "", ""
    monkeypatch.setattr(ct.ttyguard, "popen", lambda argv, **kw: seen.update(env=kw["env"]) or Proc())
    ct._run_shell([r"C:\G\usr\bin\bash.exe", "-c", "true"], shell=False, timeout=5)
    assert seen["exe"] == r"C:\G\usr\bin\bash.exe" and seen["env"]["T1397"] == "1"


def _git_root_here() -> Path | None:
    exe = ct.bash_exe()
    return ct._git_root(exe) if exe else None


@pytest.mark.skipif(not WIN or _git_root_here() is None, reason="needs Git for Windows on this box")
def test_ls_grep_sed_awk_git_curl_resolve_through_the_tool_as_under_the_launcher():
    """Under Git's launcher (MSYSTEM=MINGW64, mingw64\\bin then usr\\bin first on PATH) these are
    /usr/bin's coreutils and /mingw64/bin's git and curl. Same here, with the real bash run directly."""
    r = ct.tool_bash({"command": "echo MSYSTEM=$MSYSTEM; for c in ls grep sed awk git curl; do command -v $c; done"})
    lines = [line.strip() for line in r.splitlines() if line.strip()]
    assert "MSYSTEM=MINGW64" in lines, r
    for name, where in (("ls", "/usr/bin/"), ("grep", "/usr/bin/"), ("sed", "/usr/bin/"), ("awk", "/usr/bin/"),
                        ("git", "/mingw64/bin/"), ("curl", "/mingw64/bin/")):
        assert any(line.startswith(where) and line.rsplit("/", 1)[-1] in (name, f"{name}.exe") for line in lines), \
            f"{name} not resolved from {where}: {r}"


@pytest.mark.skipif(not WIN or _git_root_here() is None, reason="needs Git for Windows on this box")
def test_a_bash_call_starts_no_git_launcher_counted_with_psutil():
    """The spawn count: every process under this one while the tool runs git and curl. 0 launchers
    (Git\\bin\\bash.exe, Git\\cmd\\git.exe, Git\\bin\\git.exe); the real usr\\bin\\bash.exe must be SEEN,
    or the count proves nothing (the poll could be blind)."""
    import threading

    import psutil

    root = _git_root_here()
    launchers = {str(root / "bin" / "bash.exe").lower(), str(root / "cmd" / "git.exe").lower(),
                 str(root / "bin" / "git.exe").lower()}
    seen: set[str] = set()
    done = threading.Event()

    def poll():
        me = psutil.Process()
        while not done.is_set():
            for child in me.children(recursive=True):
                try:
                    seen.add(child.exe().lower())
                except (psutil.Error, OSError):
                    pass
    watcher = threading.Thread(target=poll, daemon=True)
    watcher.start()
    try:
        r = ct.tool_bash({"command": "git --version; curl --version | head -1; sleep 1; git --version"})
    finally:
        done.set()
        watcher.join(5)
    assert "git version" in r, r
    assert str(root / "usr" / "bin" / "bash.exe").lower() in seen, f"the poll saw no bash at all: {seen}"
    assert not (seen & launchers), f"a launcher started: {sorted(seen & launchers)}"


def test_background_stays_on_the_bash_schema():
    """The T517 gate reads the schema's properties; the templated description
    must not have moved or dropped them."""
    tasks_mod.backgroundable.cache_clear()
    assert tasks_mod.backgroundable("bash") is True
    assert "background" in ct.BASH_SPEC["function"]["parameters"]["properties"]
