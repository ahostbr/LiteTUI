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
    reason = deny_floor.refusal(command, owner.parent, shell=shell)
    assert reason is None


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
    # Brace objects after a parenthesis are no longer command boundaries.
    # These code/quote ceilings deliberately retain parent classification.
    data_object = "print({" in command or "console.log({" in command or "print(\'{" in command or 'print(\\"{' in command
    assert bool(reason) is (not data_object or command == 'python -c "print(\'{ run }\')"')


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", [
    'unknown "prefix; python -c "print({ run })"',
    '$p.ArgumentList.Add(\'run)', "Invoke-Headless @('run)",
    r'python -c \"print({ run })"',
])
def test_owner_position_ambiguous_quote_context(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    reason = deny_floor.refusal(command, owner.parent, shell=shell)
    assert bool(reason) is ("ArgumentList" in command or "Invoke-Headless" in command)


READER_EXEC_COMMANDS_T0291A = [
    'git -c alias.x="!(./run.bat)" x run',
    'git -c alias.x="!(cmd /c run.bat)" x',
    'git -c alias.x="!(cmd.exe /c run.bat)" x',
    'git -c alias.x="!(start run.bat)" x',
    'git -c alias.x="!(call run.bat)" x',
    'git -c alias.x="!(powershell -c run.bat)" x',
    'git -c "alias.x=!(./run.bat)" x run',
    'git --config alias.x="!(./run.bat)" x run',
    'git -c core.pager="(./run.bat)" log',
    'git -c core.editor="(./run.bat)" commit',
    'git -c core.sshCommand="(./run.bat)" fetch',
    'git --exec="(./run.bat)" status',
    'git --upload-pack="(./run.bat)" fetch',
    'X=value git -c alias.x="!(./run.bat)" x',
    'git commit -m other -c alias.x="!(./run.bat)"',
    'git commit -m "!(./run.bat)"',
    'sed -e "(./run.bat)" file',
    'awk -e "(./run.bat)" file',
    'vim -c "(./run.bat)" file',
    'rg --pre "(./run.bat)" text',
    'git commit -m "prose $(./run.bat)"',
    'Write-Output "prose $(./run.bat)"',
]


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", READER_EXEC_COMMANDS_T0291A)
def test_owner_reader_executable_option_not_data(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    reason = deny_floor.refusal(command, owner.parent, shell=shell)
    assert reason and "[owner-launcher]" in reason


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", [
    'git commit -m "fix: ( run ) and { run } are prose"',
    'git tag -m "fix: ( run ) and { run } are prose" v1',
    'git commit --message "fix: ( run ) and { run } are prose"',
    'git tag --message "fix: ( run ) and { run } are prose" v1',
    'Write-Output "examples: { run } and (run)"',
    "Write-Host 'examples: {run} and ( run )'",
    'echo "examples: {run} and ( run )"',
    'printf "examples: {run} and ( run )"',
])
def test_owner_explicit_quoted_data_positions(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    reason = deny_floor.refusal(command, owner.parent, shell=shell)
    assert bool(reason) is command.startswith("git tag ")


SHAPE_ATTACKS_T0291A = [
    'Write-Output "prefix\u201d; run; \u201csuffix"',
    'python -c "prefix\u201d; run; \u201csuffix"',
    'echo ^"& run & ^"', 'python -c ^"& run & ^"',
    "echo 'prefix & run & suffix'", "python -c 'prefix & run & suffix'",
    'echo "prefix\u2019; run; \u2018suffix"',
    'python -c "prefix\u2019; run; \u2018suffix"',
    'echo "{ run } %UNTRUSTED%"', 'echo "%Q% & run & %Q%"',
    'python -c "print({ run })"; run',
    'run; python -c "print({ run })"',
    'git -c alias.x="!(./run.bat)" x run',
    'git -c alias.x="!(cmd /c run.bat)" x',
]


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", SHAPE_ATTACKS_T0291A)
def test_owner_whole_shape_does_not_swallow_execution(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    reason = deny_floor.refusal(command, owner.parent, shell=shell)
    assert bool(reason) is (command != 'echo "{ run } %UNTRUSTED%"')


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("char", ["^", "`", "$", "\\", "!", "%", "\r", "\n", "'",
    "\u2018", "\u2019", "\u201a", "\u201b", "\u201c", "\u201d", "\u201e", "\u201f",
    "\u00ab", "\u00bb", "\u2039", "\u203a", "\uff02", "\uff07", "\u200b", "\u202e", "\x00"])
def test_owner_whole_shape_ambiguity_is_scanned(tmp_path, monkeypatch, shell, char):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    command = 'echo "( run )' + char + '"'
    assert not deny_floor._launcher_quoted_argument(command, command.index('run'))
    assert deny_floor.refusal(command, owner.parent, shell=shell)


SHAPE_COST_COMMANDS_T0291A = [
    "python -c 'print(run)'", "python -c 'print({ run })'",
    "node -e 'console.log({run})'", "git commit -m 'fix: { run }'",
    'git tag -m "fix: { run }"',
    'git tag -m "fix: { run }" v1',
]


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", SHAPE_COST_COMMANDS_T0291A)
def test_owner_single_quotes_and_trailing_arguments_cost(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    reason = deny_floor.refusal(command, owner.parent, shell=shell)
    assert bool(reason) is (command == 'git tag -m "fix: { run }" v1')


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", [
    'git commit -c core.editor="(./run.bat)" -m text',
    'git tag -c core.pager="(./run.bat)" -m text',
    'less "!(cmd /c run.bat)"', 'more "!(cmd /c run.bat)"',
    'git commit -m "!(cmd /c run.bat)"',
    'echo "!(cmd /c run.bat)"',
    'echo "& run &"\n',
])
def test_owner_positive_shape_execution_siblings(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert deny_floor.refusal(command, owner.parent, shell=shell)


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command, exempt", [
    ('Write-Output\t"( run )"', True),
    ('git\tcommit\t-m\t"fix: { run }"', True),
    ('echo\t"{ run }"', True),
    ('echo\v"( run )"', False),
    ('echo\u00a0"( run )"', False),
    ('echo "( run )"\n', False),
])
def test_owner_shape_token_whitespace(tmp_path, monkeypatch, shell, command, exempt):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert deny_floor._launcher_quoted_argument(command, command.index('run')) is exempt
    assert bool(deny_floor.refusal(command, owner.parent, shell=shell)) is (not exempt)


CODE_SINK_COMMANDS_T0291A = [
    'python -c "import os; run = 0; os.system(dir()[-1])"',
    'python -c "import os; run = 0; os.system([*locals()][-1])"',
    'python -c "import os; run = 0; os.system(max(dir()))"',
    'python.exe -c "import os; run = 0; os.system(max(dir()))"',
    'node -e "var run,child_process;require(Object.keys({child_process})[0]).execSync(Object.keys({run})[0])"',
    'node.exe -e "var run,child_process;require(Object.keys({child_process})[0]).execSync(Object.keys({run})[0])"',
]

EXEC_IDENT_HEADS_T0291A = [
    "Start-Process", "saps", "start", "Invoke-Item", "ii", "iex", "Invoke-Expression",
    "Invoke-Command", "icm", "Start-Job", "sajb", "call", "cmd", "cmd.exe",
    "powershell", "powershell.exe", "pwsh", "pwsh.exe", "bash", "sh", "Start-ThreadJob",
]


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", CODE_SINK_COMMANDS_T0291A)
def test_owner_interpreter_code_not_data(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    # D1: richer anchored interpreter code mentioning the launcher is refused.
    # Classification only: never invoke the candidates.
    assert deny_floor.refusal(command, owner.parent, shell=shell)


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("head", EXEC_IDENT_HEADS_T0291A)
@pytest.mark.parametrize("upper", [False, True])
def test_owner_execution_sink_ident_argument(tmp_path, monkeypatch, shell, head, upper):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    command = (head.upper() if upper else head) + '("run")'
    assert deny_floor.refusal(command, owner.parent, shell=shell)


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", [
    'Invoke-Expression("run.bat")', '& { run }', '1 | % { run }',
    'if ($true) { run }', 'try { run } catch {}', 'function f { run }',
    '& { & { run } }', '1 | % { & { run } }', '. { run }',
])
def test_owner_brace_command_bodies_remain_scanned(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert deny_floor.refusal(command, owner.parent, shell=shell)


@pytest.mark.parametrize("shell", [None, "powershell"])
@pytest.mark.parametrize("command", [
    'rg "{run}" tests', "rg '{run}' tests", 'echo \'{"run": 1}\'',
    'git grep -n "{ run }"', 'sed s/{run}/{exec}/',
    'python -c \'print(run)\'', 'node -e \'console.log({run})\'',
])
def test_owner_brace_data_cost_reduction(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert deny_floor.refusal(command, owner.parent, shell=shell) is None


@pytest.mark.parametrize("shell", [None, "cmd", "powershell", "bash"])
@pytest.mark.parametrize("command, refused", [('Get-Date\rrun', True), ('& { run }', True), ('1 | % {run}', True), ('1 | ForEach-Object { run }', True), ('& { if ($true) { run } }', True), ('Get-Date(run)', True), ('git commit -m "fix(run): keep owner launcher"', False), ("rg '{run}' tests", False), ('rg "{run}" tests', False), ("node -e 'console.log({run})'", False)])
def test_owner_sentinel_corpus_regressions(tmp_path, monkeypatch, shell, command, refused):
    """Sentinel corpus_a/corpus4 data; no candidate string is executed."""
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert bool(deny_floor.refusal(command, owner.parent, shell=shell)) is refused


FINAL_ALLOWED_T0291A = [
    "git commit -m 'fix(run): x'", "git commit -am 'fix(run): x'",
    "git commit --amend -m 'fix(run): x'", 'git commit --amend -m "fix(run): x"',
    'git commit -am "fix(run): x"', "git commit -a --amend --message 'fix(run): x'",
    'echo \'{"run": 1}\'', "python -c 'print(run)'", 'python -c \'print("x")\'',
    'python -c "print(run)"', 'PYTHON.EXE -c "print(run)"',
    'python -c "print ( run )"', "node -e 'console.log({run})'",
    'NODE.EXE -e "console.log( {run} )"',
]
FINAL_RICH_CODE_T0291A = [
    'python -c "print(dir())"', 'python -c "print(dir()[-1])"',
    'python -c "print([*locals()][-1])"', 'python -c "print(max(dir()))"',
    'python -c "print(run())"', 'node -e "console.log(run())"',
    'python -c "print(run);x=1"', 'node -e "console.log(`run`)"',
    'python -c "print(run[0])"', 'python -c "print(run+1)"',
    'python -c "Print(run)"', 'node -e "Console.log(run)"',
]

@pytest.mark.parametrize("shell", [None, "cmd", "powershell", "bash"])
@pytest.mark.parametrize("command", FINAL_ALLOWED_T0291A)
def test_owner_final_data_and_one_call_allow_shapes(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert deny_floor.refusal(command, owner.parent, shell=shell) is None
    assert deny_floor._launcher_quoted_argument(command, command.index('run') if 'run' in command else command.index('x'))

@pytest.mark.parametrize("command", FINAL_RICH_CODE_T0291A)
def test_owner_final_one_call_richer_code_has_no_exemption(command):
    # No run token means no public owner refusal; this pins the shape boundary.
    assert not deny_floor._launcher_quoted_argument(command, command.index('(') + 1)

@pytest.mark.parametrize("shell", [None, "cmd", "powershell", "bash"])
@pytest.mark.parametrize("command, refused", [
    ('git commit -m "$(cat <<\'EOF\'\nfix(run): x\nEOF\n)"', True),
    ('git commit -m "$(cat <<\'EOF\'\nplain message\nEOF\n)"', False),
])
def test_owner_heredoc_message_current_classification(tmp_path, monkeypatch, shell, command, refused):
    # By design for now: needs its own multi-line grammar (T0291-A-A).
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert bool(deny_floor.refusal(command, owner.parent, shell=shell)) is refused

@pytest.mark.parametrize("shell", [None, "cmd", "powershell", "bash"])
@pytest.mark.parametrize("char", ["&", "|", "<", ">", "^", "%", "`", "$", "\\", "!", "\r", "\n", "'", "\u2018", "\u2019", "\u201c", "\uff02", "\u200b", "\x00"])
def test_owner_single_quote_token_ambiguity_no_exemption(tmp_path, monkeypatch, shell, char):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    command = "echo '( run )" + char + "'"
    assert not deny_floor._launcher_quoted_argument(command, command.index('run'))
    assert deny_floor.refusal(command, owner.parent, shell=shell)


@pytest.mark.parametrize("shell", [None, "cmd", "powershell", "bash"])
@pytest.mark.parametrize("command", [c for c in FINAL_RICH_CODE_T0291A if "run" in c])
def test_owner_final_richer_code_mentions_launcher_refused(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert deny_floor.refusal(command, owner.parent, shell=shell)

@pytest.mark.parametrize("shell", [None, "cmd", "powershell", "bash"])
@pytest.mark.parametrize("command", [c for c in FINAL_RICH_CODE_T0291A if "run" not in c])
def test_owner_final_richer_code_without_launcher_untouched(tmp_path, monkeypatch, shell, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert deny_floor.refusal(command, owner.parent, shell=shell) is None


@pytest.mark.parametrize("head, flag, call", [("python", "-c", "print"), ("PYTHON.EXE", "-c", "print"), ("node", "-e", "console.log"), ("NODE.EXE", "-e", "console.log")])
@pytest.mark.parametrize("space", [" ", "\t", "  "])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_owner_final_call_case_spacing_matrix(tmp_path, monkeypatch, head, flag, call, space, quote):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    command = space + head + space + flag + space + quote + call + space + "( run )" + quote + space
    assert deny_floor.refusal(command, owner.parent) is None
    bad = command.replace(call, call.upper())
    assert not deny_floor._launcher_quoted_argument(bad, bad.index("run"))
    assert deny_floor.refusal(bad, owner.parent)

@pytest.mark.parametrize("command", [
    'node -e "console.log(run())"', 'python -c "print(run[0])"',
    'node -e "Object.keys({run})"',
])
def test_owner_rich_code_still_requires_identity(tmp_path, monkeypatch, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    (other / "run.bat").write_text("@echo off\n", encoding="utf-8")
    assert deny_floor.refusal(command, other) is None
    owner.unlink()
    assert deny_floor.refusal(command, tmp_path) is None

@pytest.mark.parametrize("command", [
    "git commit -m 'prefix & run & suffix'", "git commit -am 'prefix | run | suffix'",
    "git commit --amend -m 'prefix & run & suffix'",
])
def test_owner_git_message_options_do_not_hide_operators(tmp_path, monkeypatch, command):
    owner = _gap_owner_t0291a(tmp_path, monkeypatch)
    assert not deny_floor._launcher_quoted_argument(command, command.index('run'))
    assert deny_floor.refusal(command, owner.parent)
