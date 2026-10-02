"""T1085 — jobs.json is Ryan's: only Ryan's own seat may directly write it.

T1133 removed profile locks and schedule API caps. These arms protect only direct
file writes, at the shared _authorize_action door, without restoring those caps.
The original alias bug was measured by Dijkstra (69c7c209 A1).
"""
from __future__ import annotations

import pytest

from litetui import app as m
from litetui import claude_tools, scheduler, seat_authority, tool_policy
from litetui.tool_policy import AUTONOMOUS

SPAWNER = "leader-4f1e2d3c-0000-0000-0000-000000000001"


def _agent(tmp_path, monkeypatch):
    """An autonomous agent seat; the file guard must not narrow its profile."""
    a = m.LiteTUI()
    a.settings.tool_policy_profile = AUTONOMOUS
    a._active_tool_profile = AUTONOMOUS
    a._spawned_seat = True
    a._owner_seat = False
    a._spawner_id = SPAWNER
    sent: list = []
    a.seat.registered = True
    a.seat.send = lambda to, body: sent.append((to, body)) or False
    a._system = lambda *_a, **_k: None

    def no_door(*_a, **_k):
        raise AssertionError("a human door opened; the jobs-file rule is not theirs to waive")

    monkeypatch.setattr(m, "show_dialog", no_door)

    async def no_rpc(*_a, **_k):
        raise AssertionError("the rpc host was asked")

    monkeypatch.setattr(m.tool_approval, "approve_over_rpc", no_rpc)
    return a, sent


def _ryans(a):
    a._spawned_seat, a._owner_seat, a._pty_term = False, True, None
    return a


def _calls(jobs):
    """Every backend's spelling of a write to the schedule file."""
    native_policy, native_args = claude_tools.native_policy(
        "Write", {"file_path": str(jobs), "content": "[]"})
    patch = f"*** Begin Patch\n*** Update File: {jobs}\n@@\n-[]\n+[1]\n*** End Patch\n"
    return [
        ("write", {"path": str(jobs), "content": "[]"}, tool_policy.WRITE_POLICY),
        ("edit", {"path": str(jobs), "old_string": "[]", "new_string": "[1]"},
         tool_policy.WRITE_POLICY),
        ("Write", native_args, native_policy),
        ("apply_patch", {"command": ["apply_patch", patch]}, tool_policy.WRITE_POLICY),
        ("bash", {"command": f"echo [] > {jobs}"}, tool_policy.SHELL_POLICY),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(5))
async def test_an_agent_seat_may_not_write_jobs_json_by_any_backend(tmp_path, monkeypatch, index):
    a, sent = _agent(tmp_path, monkeypatch)
    name, args, policy = _calls(tmp_path / "jobs.json")[index]
    result = await a._authorize_action(name, args, policy, workspace=tmp_path)
    assert a._active_tool_profile == AUTONOMOUS, "the file guard must not cap profiles"
    assert result, (name, "the write was allowed")
    text = result[0]
    assert "may not write" in text and "jobs.json" in text and "T1085" in text, text
    assert "/cron" in text
    assert sent == [], "a jobs.json write was relayed to the spawner for an APPROVE"


@pytest.mark.asyncio
async def test_the_refusal_comes_before_the_relay(tmp_path, monkeypatch):
    a, sent = _agent(tmp_path, monkeypatch)
    assert seat_authority.confirm_route(a) == "spawner"
    result = await a._authorize_action("bash", {"command": "echo [] > jobs.json"},
                                       tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert result and "T1085" in result[0]
    assert sent == []


def test_ryans_own_seat_may_write_its_schedule(tmp_path):
    a = _ryans(m.LiteTUI())
    for _name, args, policy in _calls(tmp_path / "jobs.json"):
        assert seat_authority.jobs_file_refusal(a, args, tmp_path, policy) is None, args


def test_the_litetui_floor_leaves_jobs_to_the_seat(tmp_path):
    assert tool_policy._floor({"command": f"echo [] > {tmp_path / 'jobs.json'}"}, tmp_path) is None
    assert "[home-variable-delete]" in tool_policy._floor({"command": "rm -rf ~"}, tmp_path)


def test_scheduler_save_is_untouched_in_an_agent_seat(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch)
    scheduler.save([], tmp_path)
    assert (tmp_path / "jobs.json").is_file()


@pytest.mark.parametrize("args", [
    {"path": "notes.md", "content": "x"},
    {"command": "cat jobs.json"},
    {"command": "copy jobs.json backup.json"},
    {"path": "myjobs.json", "content": "x"},
])
def test_other_files_and_reads_pass_in_an_agent_seat(tmp_path, monkeypatch, args):
    a, _ = _agent(tmp_path, monkeypatch)
    assert seat_authority.jobs_file_refusal(a, args, tmp_path) is None, args


def test_another_folders_jobs_json_passes(tmp_path, monkeypatch):
    a, _ = _agent(tmp_path, monkeypatch)
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert seat_authority.jobs_file_refusal(a, {"path": str(other / "jobs.json")}, tmp_path) is None


@pytest.mark.parametrize("alias", ["jobs.json::$DATA", "jobs.json.", "jobs.json ", "jobs.json:x"])
def test_a_windows_alias_of_jobs_json_is_refused_too(tmp_path, monkeypatch, alias):
    a, _ = _agent(tmp_path, monkeypatch)
    why = seat_authority.jobs_file_refusal(a, {"path": str(tmp_path / "x")[:-1] + alias}, tmp_path)
    assert why and "T1085" in why, alias


@pytest.mark.asyncio
async def test_a_read_tool_path_is_not_a_write(tmp_path, monkeypatch):
    """The original argument-only guard mistook read(path=jobs.json) for a write."""
    a, _ = _agent(tmp_path, monkeypatch)
    assert await a._authorize_action("read", {"path": str(tmp_path / "jobs.json")},
                                     tool_policy.READ_POLICY, workspace=tmp_path) is None


@pytest.mark.parametrize("clean", [True, False])
def test_protected_write_rechecks_owner_taint(tmp_path, monkeypatch, clean):
    """A cached owner mark must not exempt a bridge-tainted write mid-turn."""
    a = _ryans(m.LiteTUI())
    a._pty_term = "owner-terminal"
    monkeypatch.setattr(seat_authority, "pty_taint_clean", lambda term: clean)
    why = seat_authority.jobs_file_refusal(a, {"path": str(tmp_path / "jobs.json")}, tmp_path)
    assert (why is None) is clean
    assert a._owner_seat is clean


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["restore --", "checkout HEAD --", "rm --", "mv --", "clean -fx --"])
async def test_git_explicit_schedule_writers_are_refused(tmp_path, monkeypatch, verb):
    a, sent = _agent(tmp_path, monkeypatch)
    args = {"command": f"git {verb} {tmp_path}/jobs.json backup.json"}
    denied = await a._authorize_action("bash", args, tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert denied and "T1085" in denied[0]
    assert sent == []
    assert a._active_tool_profile == AUTONOMOUS
    assert seat_authority.jobs_file_refusal(_ryans(a), args, tmp_path) is None


@pytest.mark.parametrize("verb", ["show HEAD:", "diff -- ", "status -- "])
def test_git_schedule_readers_still_pass(tmp_path, monkeypatch, verb):
    a, _ = _agent(tmp_path, monkeypatch)
    args = {"command": f"git {verb}{tmp_path}/jobs.json"}
    assert seat_authority.jobs_file_refusal(a, args, tmp_path) is None
