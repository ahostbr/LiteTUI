"""A read-only compound PowerShell command is not a write to a protected data file.

The owner-file guards (jobs.json, settings.json) ask the deny floor about the
command. Given no proven interpreter the floor reads it under every dialect, and
under cmd `;` is data and `'` is not a quote: a `;`-joined run of Git reads becomes
ONE malformed relocated Git write, and a single-quoted briefing that merely names
the file becomes a redirect target. The core PowerShell tool proves its own
interpreter, so the door passes it. Writes stay refused, mixed or not.

Classifier inputs only: nothing here runs a command or touches a schedule.
"""
from __future__ import annotations

import pytest

from litetui import app as m
from litetui import seat_authority, tool_policy
from litetui.tool_policy import AUTONOMOUS
from _owned_seat import owned_app

SPAWNER = "leader-4f1e2d3c-0000-0000-0000-000000000001"
SHELL = tool_policy.SHELL_POLICY


def _agent(monkeypatch):
    a = owned_app()
    a.settings.tool_policy_profile = AUTONOMOUS
    a._active_tool_profile = AUTONOMOUS
    a._spawned_seat, a._owner_seat, a._spawner_id = True, False, SPAWNER
    a.seat.registered = True
    a.seat.send = lambda to, body: False
    a._system = lambda *_a, **_k: None

    def no_door(*_a, **_k):
        raise AssertionError("a human door opened")

    monkeypatch.setattr(m, "show_dialog", no_door)
    return a


_BRIEF = (
    "$brief=@'\n"
    "Fix the floor. Never touch the live .litetui-data.json, lock, jobs.json or settings.json.\n"
    "Own worktree C:/ExampleProjects/.scratch/T0001 branch work/T0001; no push.\n"
    "'@; python -m example.cli spawn --split --name Builder --cwd C:/ExampleProjects/.scratch/T0001 --prompt $brief"
)

READS = [
    # two relocated Git reads, then a disk-space read (the original refusal)
    "git -C C:/ExampleProjects/RepoA show 3324bc2 -- pkg/cli.py; "
    "git -C C:/ExampleProjects/RepoB diff 45c94d5cc f4a2001f7 -- apps/app.ts; "
    "python -c \"import shutil; print('C free GB:',round(shutil.disk_usage('C:/').free/2**30,2))\"",
    "git -C C:/ExampleProjects/RepoA worktree list; git -C C:/ExampleProjects/RepoA rev-parse HEAD; "
    "git -C C:/ExampleProjects/RepoA ls-files",
    "git -C C:/ExampleProjects/RepoA status; echo done",
    # a tool prefix, Set-Location into a quoted folder, then reads naming settings.json
    "tool run tasks action=claim task_id=T1 assignee=a; tool run tasks action=update task_id=T1 status=building; "
    "Set-Location 'C:/ExampleHome/.config'; git branch --show-current; git status --porcelain; "
    "git diff --stat -- settings.json; git log -1 --format='%H %s' -- settings.json",
    "$c=git -C C:/ExampleProjects/RepoA show HEAD:src/a.py; git -C C:/ExampleProjects/RepoA log -1",
    _BRIEF,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", READS)
async def test_read_only_compound_is_allowed_at_the_door(tmp_path, monkeypatch, command):
    a = _agent(monkeypatch)
    assert await a._authorize_action("powershell", {"command": command}, SHELL,
                                     workspace=tmp_path) is None, command


@pytest.mark.parametrize("command", READS)
def test_read_only_compound_passes_both_owner_guards(tmp_path, monkeypatch, command):
    a = _agent(monkeypatch)
    for guard in (seat_authority.jobs_file_refusal, seat_authority.settings_file_refusal):
        assert guard(a, {"command": command}, tmp_path, SHELL, shell="powershell") is None, command


@pytest.mark.parametrize("command", [c for c in READS if c is not _BRIEF][:3] + [_BRIEF])
def test_the_unproven_reading_is_still_conservative(tmp_path, monkeypatch, command):
    """No proof, no waiver: a tool that cannot prove its interpreter keeps the
    all-dialect scan, which refuses these shapes. The fix is the proof, not a weaker scan."""
    a = _agent(monkeypatch)
    refused = [guard(a, {"command": command}, tmp_path, SHELL) is not None
               for guard in (seat_authority.jobs_file_refusal, seat_authority.settings_file_refusal)]
    assert any(refused), command


WRITES = [
    ("jobs.json", "git -C C:/ExampleProjects/RepoA status; git -C C:/ExampleProjects/RepoA commit -m x"),
    ("jobs.json", "git -C C:/ExampleProjects/RepoA show HEAD; Set-Content {root}/jobs.json '[]'"),
    ("jobs.json", "git -C C:/ExampleProjects/RepoA log -1; echo [] > {root}/jobs.json"),
    ("jobs.json", "git status; echo [] > jobs.json"),
    ("jobs.json", "git -C {root} checkout -- jobs.json; git -C {root} status"),
    ("jobs.json", _BRIEF + "; Set-Content {root}/jobs.json $brief"),
    ("settings.json", "git -C C:/ExampleProjects/RepoA diff; Set-Content {root}/settings.json '{{}}'"),
    ("settings.json", "git -C {root} log -1 -- settings.json; git -C {root} commit -m x"),
    ("settings.json", _BRIEF + "; Add-Content settings.json $brief"),
    ("settings.json", "Set-Location 'C:/ExampleHome/.config'; git status; echo x > {root}/settings.json"),
]


@pytest.mark.parametrize("name, template", WRITES)
def test_a_real_write_mixed_with_reads_is_still_refused(tmp_path, monkeypatch, name, template):
    a = _agent(monkeypatch)
    command = template.format(root=tmp_path.as_posix())
    guard = (seat_authority.jobs_file_refusal if name == "jobs.json"
             else seat_authority.settings_file_refusal)
    assert guard(a, {"command": command}, tmp_path, SHELL, shell="powershell"), command


@pytest.mark.asyncio
@pytest.mark.parametrize("name, template", WRITES[:4] + WRITES[6:8])
async def test_a_real_write_is_refused_at_the_door_too(tmp_path, monkeypatch, name, template):
    a = _agent(monkeypatch)
    command = template.format(root=tmp_path.as_posix())
    result = await a._authorize_action("powershell", {"command": command}, SHELL, workspace=tmp_path)
    assert result and ("T1085" in result[0] or "T0306" in result[0] or "DENY FLOOR" in result[0]), command


def test_only_the_core_powershell_route_proves_its_interpreter():
    assert tool_policy.proven_shell("powershell", SHELL) == "powershell"
    assert tool_policy.proven_shell("PowerShell", SHELL) is None
    assert tool_policy.proven_shell("powershell", tool_policy.READ_POLICY) is None
    assert tool_policy.proven_shell("shell", tool_policy.MCP_UNKNOWN_POLICY) is None
    assert tool_policy.proven_shell("bash", SHELL) is None
