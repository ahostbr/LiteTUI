"""The deny floor (T1026): deletes no profile, rule or turn source can run.

THE USER, 2026-09-26 (card T1026): "something just wiped out ~claude bun and
others .... u have to figure out wtf just happened" / "what can we do about
litetui letting that happend ?" / "it was set to auto though not interactive".

At 16:45 an AUTONOMOUS seat, on an INBOX turn, ran the command pinned below as
INCIDENT. The danger table classified it destructive_irreversible; autonomous
has no confirm step, so the verdict was ignored and the profile folder went.
The floor (deny_floor.py, a byte-identical copy of liteharness's) runs inside
tool_policy.evaluate BEFORE the profile, so every profile and every turn source
meets it, and it adds no prompt: it only refuses.

No arm here runs a dangerous command to see whether it is blocked. The only
real deletes target folders the test itself created, by literal path.
"""
from __future__ import annotations

import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from litetui import deny_floor, hook_host, tool_policy as tp
from litetui import app as app_mod
from litetui.plugins import core_tools as ct
from litetui.settings import Settings
from litetui.tool_approval import ONCE

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_deny_floor  # noqa: E402

INCIDENT = (r"$home='C:\Projects\LiteTUI\temp-working-dir\t1018-main-red-home'; "
            r"Remove-Item $home -Recurse -Force -ErrorAction SilentlyContinue")


@pytest.mark.parametrize("command, refused", [
    ("lst.exe run tasks action=list", False),
    ("python x.py 'please do run it'", False),
    ("if exist package.json bun run dev", False),
    ("Invoke-Headless lst.exe @('run','tasks','action=list')", False),
    ("$p.ArgumentList.Add('run')", False),
    ("run.bat", True),
    ("{owner}", True),
    ("'{owner}'", True),
    ("cmd /c '{owner}'", True),
    ("Start-Process -FilePath:{owner}", True),
    ("Start-Process -FilePath '{owner}'", True),
    ("Invoke-Item '{owner}'", True),
    ("cd {dir} && run", True),
    ("cmd /c run", True),
    ("run", True),
    ("call run", True),
    ("cmd /c @run", True),
    ("cmd /c ^run", True),
    ("cmd /c 2>nul run", True),
    ("if 1==1 run", True),
    ("if exist src run", True),
    ("if defined X run", True),
    ("for %i in (1) do run", True),
    ("if 1==2 (echo a) else run", True),
    ("for /f %i in ('echo') do run", True),
    ("Get-Content '{owner}'", False),
    ("python x.py '{owner}'", False),
    ("{other}", False),
    ("{missing}", False),
])
def test_owner_launcher_requires_executable_position_and_resolved_identity(
        tmp_path, monkeypatch, command, refused):
    """T0206: judge strings only, never execute the inert owner launcher."""
    owner = tmp_path / "owner" / "run.bat"
    (owner.parent / "src" / "litetui").mkdir(parents=True)
    owner.write_text("@echo off\n", encoding="utf-8")
    other = tmp_path / "other" / "run.bat"
    (other.parent / "src" / "litetui").mkdir(parents=True)
    other.write_text("@echo off\n", encoding="utf-8")
    missing = tmp_path / "missing" / "run.bat"
    (missing.parent / "src" / "litetui").mkdir(parents=True)
    monkeypatch.setattr(deny_floor, "_OWNER_LAUNCHER", owner)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    reason = deny_floor.refusal(command.format(owner=owner, other=other, missing=missing, dir=owner.parent),
                                owner.parent)
    assert bool(reason) is refused, reason
    if refused:
        assert "[owner-launcher]" in reason
        assert str(owner.resolve()) in reason

@pytest.mark.parametrize("command, refused", [
    ("@'\nrun = vi.fn();\n'@ | Add-Content tests.ts", False),
    ("@'\nrun\n'@", False),
    ("@'\nrun\n'@ | Set-Content 'test cases.ts'", False),
    ("@'\nrun\n'@ | Out-File tests.ts", False),
    ("@'\nrun\n'@ | Add-Content tests.ts; Write-Output ok", False),
    ("@'\r\nrun\r\n'@ | Add-Content tests.ts", False),
    ("@'\nrun\n'@ | powershell -", True),
    ("@'\nrun\n'@ | pwsh -c -", True),
    ("@'\nrun\n'@ | cmd", True),
    ("@'\nrun\n'@ | iex", True),
    ("@'\nrun\n'@ | Invoke-Expression", True),
    ("iex @'\nrun\n'@", True),
    ("Invoke-Expression @'\nrun\n'@", True),
    ("& ([scriptblock]::Create(@'\nrun\n'@))", True),
    ("python -c @'\nrun\n'@", True),
    ("node -e @'\nrun\n'@", True),
    ("@'\nrun\n'@ | unknown-sink", True),
    ("@'\nrun\n'@ | Add-Content tests.ts | iex", True),
    ("@'\nrun\n'@\n | iex", True),
    ('@"\n$(run)\n"@ | Add-Content tests.ts', True),
    ("@'\nrun\n", True),
    ("run; @'\ntext\n'@ | Add-Content tests.ts", True),
    ("@'\ntext\n'@ | Add-Content tests.ts; run", True),
    ("@'\ntext\n'@\nrun", True),
    ("@'\nrun\n'@\n# continuation comment\n | iex", True),
    ("@'\ncd missing-folder\n'@ | Add-Content tests.ts; run", True),
    ("@'\ncd missing-folder\n'@ | Add-Content tests.ts\nrun", True),
    ("& '{owner}'", True),
    ("cmd /c '{owner}'", True),
    ("Start-Process -FilePath '{owner}'", True),
])
def test_owner_launcher_literal_here_string_data_only(tmp_path, monkeypatch, command, refused):
    """T0291: classify strings only; every launcher file is inert test data."""
    owner = tmp_path / "run.bat"
    owner.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(deny_floor, "_OWNER_LAUNCHER", owner)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    reason = deny_floor.refusal(command.format(owner=owner), owner.parent, shell="powershell")
    assert bool(reason) is refused, reason
    if refused:
        assert "[owner-launcher]" in reason


def test_original_t0291_command_is_literal_test_data(tmp_path, monkeypatch):
    owner = tmp_path / "run.bat"
    owner.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(deny_floor, "_OWNER_LAUNCHER", owner)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    command = (Path(__file__).parent / "fixtures" / "T0291-here-string.txt").read_text(encoding="utf-8")
    assert deny_floor.refusal(command, owner.parent, shell="powershell") is None



# ── the gate: every profile, every standing rule ────────────────────────────

@pytest.mark.parametrize("profile", [*tp.PROFILE_NAMES, "no-such-profile"])
def test_the_incident_is_denied_under_every_profile(tmp_path, profile):
    decision = tp.evaluate(profile, tp.SHELL_POLICY, {"command": INCIDENT}, tmp_path,
                           tool_name="powershell")
    assert decision.action == tp.DENY
    assert "DENY FLOOR [home-variable-delete]" in decision.reason


def test_an_always_allow_rule_cannot_lift_the_floor(tmp_path):
    caps = tp.SHELL_POLICY.classify({"command": INCIDENT}, tmp_path)
    rule = tp.rule_key("powershell", caps)
    decision = tp.evaluate(tp.INTERACTIVE, tp.SHELL_POLICY, {"command": INCIDENT}, tmp_path,
                           tool_name="powershell", always_allow=frozenset({rule}))
    assert decision.action == tp.DENY and "DENY FLOOR" in decision.reason


@pytest.mark.parametrize("command", [
    "rm -rf ~", 'rm -rf "$HOME"', "rm -rf $HOME/*", "Remove-Item $env:USERPROFILE -Recurse",
    "rd /s /q %USERPROFILE%", "Get-ChildItem $home | Remove-Item -Recurse -Force",
    'bash -c "rm -rf ~"', "rm -rf /", "rm -rf ..",
])
def test_both_shells_meet_the_floor_under_autonomous(tmp_path, command):
    for argv in ({"command": command}, {"command": ["bash", "-c", command]}):
        decision = tp.evaluate(tp.AUTONOMOUS, tp.SHELL_POLICY, argv, tmp_path, tool_name="bash")
        assert decision.action == tp.DENY and "DENY FLOOR" in decision.reason, argv


def test_any_tool_carrying_a_command_meets_the_floor(tmp_path):
    """Review 2198d4ab F2: an MCP shell (litesuite-tools `shell`) arrives under
    MCP_UNKNOWN_POLICY, not SHELL_POLICY, with the same {command, cwd}."""
    decision = tp.evaluate(tp.AUTONOMOUS, tp.MCP_UNKNOWN_POLICY, {"command": INCIDENT},
                           tmp_path, tool_name="mcp__litesuite-tools__shell")
    assert decision.action == tp.DENY and "DENY FLOOR" in decision.reason
    # A call with no command is not judged: the floor stays out of a write.
    decision = tp.evaluate(tp.AUTONOMOUS, tp.WRITE_POLICY, {"path": str(tmp_path / "x")},
                           tmp_path, tool_name="write")
    assert decision.action == tp.ALLOW


def _profile_shaped(root, monkeypatch):
    """A fake profile the process believes is home: USERPROFILE/HOME point at it."""
    home = root / "Users" / "someone"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    return home


def test_a_relative_delete_is_judged_in_the_folder_the_tool_runs_in(tmp_path, monkeypatch):
    """Review F1: the caller judged against paths.ROOT while the shell tools run
    in Path.cwd(). `rm -rf .claude` from inside the profile must be refused."""
    home = _profile_shaped(tmp_path, monkeypatch)
    monkeypatch.chdir(home)
    elsewhere = tmp_path / "install-dir"
    elsewhere.mkdir()
    decision = tp.evaluate(tp.AUTONOMOUS, tp.SHELL_POLICY, {"command": "rm -rf .claude"},
                           elsewhere, tool_name="bash")
    assert decision.action == tp.DENY and "~/.claude" in decision.reason
    # ...and an MCP shell naming the profile as its own cwd, from anywhere.
    monkeypatch.chdir(elsewhere)
    decision = tp.evaluate(tp.AUTONOMOUS, tp.MCP_UNKNOWN_POLICY,
                           {"command": "rm -rf .claude", "cwd": str(home)},
                           elsewhere, tool_name="mcp__litesuite-tools__shell")
    assert decision.action == tp.DENY and "~/.claude" in decision.reason


@pytest.mark.parametrize("command", [
    "cd ~; Remove-Item * -Recurse -Force",
    r"Set-Location $HOME; Remove-Item .\* -Recurse",
])
def test_a_cd_home_then_a_relative_delete_is_refused(tmp_path, monkeypatch, command):
    """Review F3, both strings verbatim: the natural rewrite of a refused $home.
    The workspace is a SIBLING of the fake profile: were it the profile's
    parent, `*` would be refused as a folder containing the profile even with
    the cd ignored, and the arm could not tell (measured: it stayed green)."""
    _profile_shaped(tmp_path, monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)   # Path.cwd() is judged too (F1); keep it a sibling
    decision = tp.evaluate(tp.AUTONOMOUS, tp.SHELL_POLICY, {"command": command}, workspace,
                           tool_name="powershell")
    assert decision.action == tp.DENY and "[protected-root-delete]" in decision.reason


def test_git_rm_cached_in_a_repo_root_is_not_refused(tmp_path, monkeypatch):
    """Review F4: `git rm -r --cached .` touches the index, not the tree."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "repo")
    decision = tp.evaluate(tp.AUTONOMOUS, tp.SHELL_POLICY,
                           {"command": "git rm -r --cached ."}, tmp_path / "repo",
                           tool_name="bash")
    assert decision.action == tp.ALLOW, decision.reason


# ── through the seat: the turn sources the incident took ────────────────────

class _Policies:
    def policy_for(self, _name):
        return tp.SHELL_POLICY


def _seat(run, *, launched, saved):
    """A seat double on the real `_execute_tool` path. `launched` is the
    in-memory --tool-profile; `saved` is what the convo file holds."""
    seen = []

    async def confirm(screen):
        seen.append(screen)
        return ONCE  # a modal, if one opened, would approve: none may open

    return _Seat(
        tools_enabled=True, _rpc_emit=lambda data: None, _rpc=False,
        _active_tool_profile=launched, _dispatch_for=lambda _name: run,
        plugins=_Policies(), push_screen_wait=confirm,
        settings=Settings(tool_policy_profile=saved),
        _loop_refusal=lambda name, args: None, _loop_record=lambda name, args, result: None,
        _loop_warn=lambda name, args, result: result,
        _maybe_stage_shot=lambda name, args, result: result,
        _gui_quitting=False, _chat_running=lambda: False,
        _user_bubble=lambda *a, **k: None, _append=lambda message: None,
        # T1049: only Ryan's own instance may hold autonomous, and these arms
        # prove the floor holds AT autonomous.
        _spawned_seat=False, _owner_seat=True,
        # T0132: `_execute_tool` births the conversation before a backgroundable
        # tool starts; this double is a seat whose conversation already exists.
        _materialise_convo=lambda: None,
    ), seen


class _Seat:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _deliver_inbox(seat, monkeypatch):
    """The real inbox door (`_deliver_inbox`) and the real turn start
    (`accept_prompt`), minus the hooks snapshot and the stream."""
    monkeypatch.setattr(hook_host, "start_prompt", hook_host.accept_prompt)
    app_mod.LiteTUI._deliver_inbox(seat, {"from": "27bec769", "body": "clean up the scratch home"})


@pytest.mark.asyncio
@pytest.mark.parametrize("launched", [tp.AUTONOMOUS, tp.INTERACTIVE])
async def test_the_incident_on_an_inbox_turn_is_denied(monkeypatch, launched):
    """`launched=INTERACTIVE` over a convo saved AUTONOMOUS is the T1027 shape:
    the inbox turn runs the SAVED profile, not the flag. The floor holds either way."""
    ran = []
    seat, modals = _seat(lambda args: ran.append(args) or "ran",
                         launched=launched, saved=tp.AUTONOMOUS)
    _deliver_inbox(seat, monkeypatch)
    assert (seat._hook_source, seat._active_tool_profile) == ("harness", tp.AUTONOMOUS)

    result, ok = await app_mod.LiteTUI._execute_tool(seat, "powershell", {"command": INCIDENT})
    assert not ok and "DENY FLOOR [home-variable-delete]" in result
    assert ran == [] and modals == []


@pytest.mark.asyncio
@pytest.mark.skipif(ct.powershell_exe() is None, reason="no PowerShell on PATH")
async def test_a_scratch_recursive_delete_still_runs_unattended_under_autonomous(
        monkeypatch, tmp_path):
    """No over-blocking: the delete is REAL, of a folder this test made."""
    scratch = tmp_path / "scratch"
    (scratch / "nested").mkdir(parents=True)
    (scratch / "nested" / "f.txt").write_text("x", encoding="utf-8")
    seat, modals = _seat(ct.tool_powershell, launched=tp.AUTONOMOUS, saved=tp.AUTONOMOUS)
    _deliver_inbox(seat, monkeypatch)

    result, ok = await app_mod.LiteTUI._execute_tool(
        seat, "powershell", {"command": f"Remove-Item -LiteralPath '{scratch}' -Recurse -Force"})
    assert ok, result
    assert not scratch.exists() and modals == []


# ── the powershell tool stops at the first failed statement ─────────────────

@pytest.mark.skipif(ct.powershell_exe() is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize("command", ["$home='x'; Write-Output ok", "$true='x'; Write-Output ok"])
def test_a_failed_assignment_stops_the_command(monkeypatch, tmp_path, command):
    """Nothing but Write-Output follows the assignment; the cwd is the test's own."""
    monkeypatch.chdir(tmp_path)
    out = ct.tool_powershell({"command": command, "timeout": 60})
    assert "ok" not in out.replace("\r", "").split("\n"), out
    assert "exited with code 1" in out, out


@pytest.mark.skipif(ct.powershell_exe() is None, reason="no PowerShell on PATH")
def test_the_control_prints_ok(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert "ok" in ct.tool_powershell({"command": "$x='x'; Write-Output ok", "timeout": 60})


# ── one rule set: the copy matches liteharness and needs nothing from it ────

def test_the_copy_is_byte_identical_to_liteharness():
    """Never a silent skip on the drift guard (review F5). It compares against
    $LITEHARNESS_SRC first, else the `liteharness-oss` checkout beside this one,
    and names the file it compared in a warning. A checkout WITHOUT the file
    FAILS: before the oss branch merges, point LITEHARNESS_SRC at its worktree.
    Only a machine with no liteharness-oss checkout at all skips."""
    source = sync_deny_floor.canonical()
    if source is None:
        pytest.skip("no liteharness-oss checkout found and LITEHARNESS_SRC unset")
    assert source.is_file(), (
        f"{source} does not exist: merge the liteharness-oss deny-floor branch, or "
        "set LITEHARNESS_SRC to a checkout that has it")
    warnings.warn(f"deny_floor.py compared against {source}", stacklevel=1)
    assert source.read_bytes() == Path(deny_floor.__file__).read_bytes(), (
        f"{source} and src/litetui/deny_floor.py differ: edit the canonical file "
        "and run scripts/sync_deny_floor.py")


def test_the_floor_works_with_liteharness_not_importable(tmp_path):
    probe = (
        "import sys; sys.modules['liteharness'] = None\n"
        "from pathlib import Path\n"
        "from litetui import tool_policy as tp\n"
        f"d = tp.evaluate('autonomous', tp.SHELL_POLICY, {{'command': {INCIDENT!r}}}, Path('.'))\n"
        "print(d.action, d.reason[:40])\n")
    src = str(Path(tp.__file__).resolve().parents[1])
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          cwd=tmp_path, env={**os.environ, "PYTHONPATH": src}, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("deny DENY FLOOR [home-variable-delete]"), proc.stdout


@pytest.mark.parametrize("shell", [None, "cmd", "bash", "powershell"])
@pytest.mark.parametrize("original", [False, True])
def test_here_string_exemption_requires_proven_shell(tmp_path, monkeypatch, shell, original):
    owner = tmp_path / "run.bat"
    owner.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(deny_floor, "_OWNER_LAUNCHER", owner)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    command = "@'\nrun\n'@"
    if original:
        command = (Path(__file__).parent / "fixtures/T0291-here-string.txt").read_text(encoding="utf-8")
    kwargs = {} if shell is None else {"shell": shell}
    reason = deny_floor.refusal(command, tmp_path, **kwargs)
    assert bool(reason) is (shell != "powershell"), reason


@pytest.mark.parametrize("tool_name, policy, command_list, refused", [
    ("powershell", tp.SHELL_POLICY, False, False),
    ("bash", tp.SHELL_POLICY, False, True),
    ("cmd", tp.SHELL_POLICY, False, True),
    ("", tp.SHELL_POLICY, False, True),
    ("mcp__litesuite-tools__shell", tp.MCP_UNKNOWN_POLICY, False, True),
    ("powershell", tp.MCP_UNKNOWN_POLICY, False, True),
    ("powershell", tp.SHELL_POLICY, True, True),
])
@pytest.mark.parametrize("original", [False, True])
def test_floor_here_string_shell_route(tmp_path, monkeypatch, tool_name, policy, command_list, refused, original):
    owner = tmp_path / "run.bat"
    owner.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(deny_floor, "_OWNER_LAUNCHER", owner)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.chdir(tmp_path)
    command = "@'\nrun\n'@"
    if original:
        command = (Path(__file__).parent / "fixtures/T0291-here-string.txt").read_text(encoding="utf-8")
    decision = tp.evaluate(tp.AUTONOMOUS, policy,
                           {"command": [command] if command_list else command, "shell": "powershell"},
                           tmp_path, tool_name=tool_name)
    assert ("[owner-launcher]" in decision.reason) is refused, decision.reason


@pytest.mark.parametrize("closer", ["\u2018", "\u2019", "\u201a", "\u201b", "\r'"])
@pytest.mark.parametrize("separator", ["; ", "\n"])
def test_here_string_ambiguous_terminator_falls_back(tmp_path, monkeypatch, closer, separator):
    owner = tmp_path / "run.bat"
    owner.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(deny_floor, "_OWNER_LAUNCHER", owner)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    command = "@'\nfoo\n" + closer + "@" + separator + "run\n'@ | Add-Content x\n'"
    if closer == "\r'":
        command = "@'\nfoo" + closer + "@" + separator + "run\n'@ | Add-Content x\n'"
    default = deny_floor.refusal(command, tmp_path)
    assert default and "[owner-launcher]" in default
    assert deny_floor.refusal(command, tmp_path, shell="powershell") == default


@pytest.mark.parametrize("command", [
    "@'\r\nrun\r\n'@ | Add-Content tests.ts",
    "Write-Output \u2019; run",
    "Write-Output ok\rrun",
    "Write-Output \u2019",
    "Write-Output ok\rWrite-Output done",
    "Write-Output \u2019; @'\nrun\n'@ | Add-Content x",
    "Write-Output ok\r\n@'\nrun\n'@ | Add-Content x\r",
])
def test_here_string_unusual_syntax_is_conservative(tmp_path, monkeypatch, command):
    owner = tmp_path / "run.bat"
    owner.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(deny_floor, "_OWNER_LAUNCHER", owner)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    if "\u2019" not in command and "\r" not in command.replace("\r\n", ""):
        assert deny_floor.refusal(command, tmp_path, shell="powershell") is None
    else:
        assert deny_floor.refusal(command, tmp_path, shell="powershell") == deny_floor.refusal(command, tmp_path)


GAP_COMMANDS_T0291A = [
    "Get-Date\rrun", "Get-Date\r\r\nrun",
    "& { run }", "1 | % {run}", "1 | ForEach-Object { run }",
    "& { & { run } }", "1 | % { & {run} }",
    "Get-Date(run", "Get-Date(run)", "Write-Output $(run)",
    "Get-Date($(run))", "Get-Date(1 | % {run})",
]

COST_COMMANDS_T0291A = [
    'python -c "print(run)"', 'python -c "print({run})"',
    'python -c "print( run )"', 'python -c "print({ run })"',
    'node -e "console.log(run)"', 'node -e "console.log({run})"',
    'node -e "console.log({ run })"',
    'git commit -m "fix(run): keep {run} as data"',
    "Get-Date(1)", "Format-Value($value)", "Invoke-Headless lst.exe @('run','tasks')",
    "$p.ArgumentList.Add('run')", "python x.py 'please do run it'",
    "Get-Content run.bat", "rg 'run(' tests", "rg '{run}' tests",
]

HERE_COST_COMMANDS_T0291A = [
    "@'\nrun = () => ({ run });\nfunction test() { run(); }\n'@ | Add-Content tests.ts",
    "@'\ndef run():\n    return {run}\nprint( run )\n'@ | Set-Content test.py",
    "@'\r\nrun = () => ({run});\r\n'@ | Add-Content tests.ts",
]


def _gap_owner_t0291a(tmp_path, monkeypatch):
    owner = tmp_path / "run.bat"
    owner.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(deny_floor, "_OWNER_LAUNCHER", owner)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return owner


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", GAP_COMMANDS_T0291A)
def test_older_owner_command_position_gaps(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    reason = deny_floor.refusal(command, owner.parent, shell=shell)
    assert reason and "[owner-launcher]" in reason


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", COST_COMMANDS_T0291A)
def test_owner_position_cost_corpus(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert deny_floor.refusal(command, owner.parent, shell=shell) is None


@pytest.mark.parametrize("command", HERE_COST_COMMANDS_T0291A)
def test_owner_position_here_string_cost(tmp_path, monkeypatch, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert deny_floor.refusal(command, owner.parent, shell="powershell") is None


QUOTE_GUARD_COMMANDS_T0291A = [
    'python -c "print(run)"; run', 'node -e "console.log(run)" & run',
    'python -c "print(run)"\nrun', 'python -c "print(run)"\rrun',
    'node -e "console.log(run)" | run',
    'python -c "print(run)"; & {run}', 'node -e "console.log(run)"; Get-Date(run)',
    'run; python -c "print(run)"', 'run | node -e "console.log(run)"',
    'python -c "print({ run })', 'node -e "console.log({ run })',
    'python -c "print(\'{ run }\')"',
    r'python -c "print(\"{ run }\")"',
    'python -c "print(\n{ run }\n)"', 'node -e "console.log(\r{ run }\r)"',
    'python -c "print($(run))"', 'node -e "console.log(`run`)"',
    'unknown -c "print({run})"', 'python -x "print({ run })"',
    'python -c other "print({ run })"', 'node -e other "console.log({ run })"',
]


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", QUOTE_GUARD_COMMANDS_T0291A)
def test_owner_position_quote_guard_fails_closed(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    reason = deny_floor.refusal(command, owner.parent, shell=shell)
    assert reason and "[owner-launcher]" in reason


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", [
    'unknown "prefix; python -c "print({ run })"',
    '$p.ArgumentList.Add(\'run)', "Invoke-Headless @('run)",
    r'python -c \"print({ run })"',
])
def test_owner_position_ambiguous_quote_context(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    reason = deny_floor.refusal(command, owner.parent, shell=shell)
    assert reason and "[owner-launcher]" in reason
