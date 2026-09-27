"""T1085 — jobs.json is Ryan's: a LOCKED seat may not write it by any tool.

Why (Dijkstra f0ae21c1 P1; Sentinel abb3bd01): since T1082 a job's recorded
tool_profile IS its authority, and jobs.json is the file Ryan's own LiteTUI fires
them from. A row written by an agent's tool is authority that agent does not have.
The API doors were closed by T1082; these arms close the file, in every backend,
at the one door they all pass (`LiteTUI._authorize_action`).

conftest points LITETUI_DATA_ROOT at tmp_path, so tmp_path/jobs.json IS the data
root's schedule file here, and conftest clears the owner mark, so a LiteTUI built
here is LOCKED unless an arm marks it Ryan's own.
"""
from __future__ import annotations

import pytest

from litetui import app as m
from litetui import claude_tools, scheduler, seat_authority, tool_policy
from litetui.tool_policy import INTERACTIVE

SPAWNER = "leader-4f1e2d3c-0000-0000-0000-000000000001"


def _locked(tmp_path, monkeypatch):
    """A spawned, locked seat whose every human door is booby-trapped and whose
    relay records what it would send (Dijkstra: nothing may be sent)."""
    a = m.LiteTUI()
    a.settings.tool_policy_profile = INTERACTIVE
    a._active_tool_profile = INTERACTIVE
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
    native_policy, native_args = claude_tools.native_policy("Write", {"file_path": str(jobs),
                                                                      "content": "[]"})
    patch = f"*** Begin Patch\n*** Update File: {jobs}\n@@\n-[]\n+[1]\n*** End Patch\n"
    return [
        ("write", {"path": str(jobs), "content": "[]"}, tool_policy.WRITE_POLICY),
        ("edit", {"path": str(jobs), "old_string": "[]", "new_string": "[1]"},
         tool_policy.WRITE_POLICY),
        ("Write", native_args, native_policy),
        ("apply_patch", {"command": ["apply_patch", patch]}, tool_policy.WRITE_POLICY),
        ("bash", {"command": f"echo [] > {jobs}"}, tool_policy.SHELL_POLICY),
    ]


@pytest.mark.parametrize("index", range(5))
async def test_a_locked_seat_may_not_write_jobs_json_by_any_backend(tmp_path, monkeypatch, index):
    a, sent = _locked(tmp_path, monkeypatch)
    name, args, policy = _calls(tmp_path / "jobs.json")[index]
    result = await a._authorize_action(name, args, policy, workspace=tmp_path)
    assert result, (name, "the write was allowed")
    text = result[0]
    assert "may not write" in text and "jobs.json" in text and "T1085" in text, text
    assert "/cron" in text
    assert sent == [], "a jobs.json write was relayed to the spawner for an APPROVE"


async def test_the_refusal_comes_before_the_relay(tmp_path, monkeypatch):
    """Dijkstra 0fd0f2d0: a locked spawner-route seat's `echo > jobs.json` is
    refused with NO [APPROVAL] sent; the rule is Ryan's, not the spawner's."""
    a, sent = _locked(tmp_path, monkeypatch)
    assert seat_authority.confirm_route(a) == "spawner"
    result = await a._authorize_action("bash", {"command": "echo [] > jobs.json"},
                                       tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert result and "T1085" in result[0]
    assert sent == []


def test_ryans_own_seat_may_write_its_schedule(tmp_path):
    """The exemption: Ryan's own seat is not refused by this rule, by any backend."""
    a = _ryans(m.LiteTUI())
    for _name, args, _policy in _calls(tmp_path / "jobs.json"):
        assert seat_authority.jobs_file_refusal(a, args, tmp_path) is None, args


def test_the_litetui_floor_leaves_jobs_to_the_seat(tmp_path):
    """LiteTUI's floor passes jobs=False, so a jobs.json write is not floor-denied
    in Ryan's seat. The floor still refuses its other rules (control)."""
    assert tool_policy._floor({"command": f"echo [] > {tmp_path / 'jobs.json'}"}, tmp_path) is None
    assert "[home-variable-delete]" in tool_policy._floor({"command": "rm -rf ~"}, tmp_path)


def test_scheduler_save_is_untouched_in_a_locked_seat(tmp_path, monkeypatch):
    """The scheduler's own save is in-process Python, never a tool call."""
    _locked(tmp_path, monkeypatch)
    scheduler.save([], tmp_path)
    assert (tmp_path / "jobs.json").is_file()


@pytest.mark.parametrize("args", [
    {"path": "notes.md", "content": "x"},
    {"command": "cat jobs.json"},
    {"command": "copy jobs.json backup.json"},
    {"path": "myjobs.json", "content": "x"},
])
def test_other_files_and_reads_pass_in_a_locked_seat(tmp_path, monkeypatch, args):
    a, _ = _locked(tmp_path, monkeypatch)
    assert seat_authority.jobs_file_refusal(a, args, tmp_path) is None, args


def test_another_folders_jobs_json_passes(tmp_path, monkeypatch):
    a, _ = _locked(tmp_path, monkeypatch)
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert seat_authority.jobs_file_refusal(a, {"path": str(other / "jobs.json")}, tmp_path) is None
