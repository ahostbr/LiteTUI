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


def test_the_floor_judges_only_shell_calls(tmp_path):
    """A write whose PATH mentions ~ is not a delete; the floor stays out."""
    decision = tp.evaluate(tp.AUTONOMOUS, tp.WRITE_POLICY, {"path": str(tmp_path / "x"),
                           "command": INCIDENT}, tmp_path, tool_name="write")
    assert decision.action == tp.ALLOW


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
    source = sync_deny_floor.canonical(os.environ.get("LITEHARNESS_SRC"))
    if source is None:
        pytest.skip("no liteharness-oss checkout beside this one")
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
