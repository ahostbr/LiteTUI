"""T1049-B2 — supervised children's CONFIRMs, spawn_agent, stopReason "approval", and K1.

Plan §5/S2 (ab8969c2, approved 5ef7612a). Gate Sentinel d47235da: an OWNER parent's
child CONFIRM reaches the parent's UI and is approved by a keypress, never inbox and
never a timeout-deny; CONTROL: a locked parent's child goes to its leader by inbox.
Dijkstra 79e113ce (3): a child stopped by an approval outcome is reported FAILED with
the reason. Dijkstra K1 (8bef1317): the Claude bridge's relay stops, is not cut at
315 s, and logs a cancelled wait.
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from litetui import app as m
from litetui import approval_relay, runtime_log, seat_authority, tool_approval, tool_policy
from litetui.agent_launcher import LaunchBlocked, validate_request
from litetui.agent_supervisor import AgentProcess, child_process_env
from litetui.tool_policy import INTERACTIVE

SPAWNER = "leader-4f1e2d3c-0000-0000-0000-000000000001"
DESTRUCTIVE = {"command": "rm -rf ./build"}


def _seat(spawner=SPAWNER, *, agent_launched=False, rpc=False, host=False):
    a = m.LiteTUI()
    a.settings.tool_policy_profile = INTERACTIVE
    a._active_tool_profile = INTERACTIVE
    a._spawned_seat = bool(spawner)
    a._owner_seat = False
    a._spawner_id = spawner
    a._agent_launched = agent_launched
    a._rpc = rpc
    a._approval_host = host
    a._system = lambda *_: None
    return a


def _ryans(a):
    a._spawned_seat, a._owner_seat, a._pty_term = False, True, None
    return a


@pytest.fixture
def log(monkeypatch):
    lines = []
    monkeypatch.setattr(runtime_log, "record",
                        lambda event, **kw: lines.append(kw) if event == "approval_relay" else None)
    return lines


def _wire_send(a, sent, ok=True):
    a.seat.registered = True
    a.seat.send = lambda to, body: sent.append((to, body)) or ok


async def _until_sent(sent, limit=5.0):
    for _ in range(int(limit / 0.01)):
        if sent:
            return re.search(r"appr-[0-9a-f]{12}", sent[0][1]).group(0)
        await asyncio.sleep(0.01)
    raise AssertionError("nothing was sent to the spawner")


async def _reply(a, sent, verb):
    ident = await _until_sent(sent)
    a._receive_mail({"from": SPAWNER, "body": f"{verb} {ident}"})


# ── K1: the Claude bridge (claude_tools._decide) ────────────────────────────

def _bridge(a, tmp_path):
    from litetui.claude_tools import ClaudeTools
    bridge = ClaudeTools.__new__(ClaudeTools)
    bridge.app, bridge.workspace, bridge._decisions = a, tmp_path, set()
    return bridge


@pytest.mark.asyncio
async def test_K1a_a_spawner_DENY_through_the_claude_bridge_stops_the_turn_and_logs(log, tmp_path):
    a = _seat()
    a._hook_source = "scheduled"
    sent = []
    _wire_send(a, sent)
    replying = asyncio.create_task(_reply(a, sent, "DENY"))
    refusal = await _bridge(a, tmp_path)._decide("Bash", DESTRUCTIVE, tool_policy.SHELL_POLICY)
    await replying
    assert refusal and "denied Bash" in refusal
    assert a._stop_requested and a._stop_cause == "approval"
    assert [kw["status"] for kw in log] == ["denied"]


def test_K1b_the_claude_deadline_outlasts_the_relay_timeout_not_315s():
    from litetui.claude_tools import _deadlines
    a = _seat()
    a.settings.relay_approval_timeout_s = 400
    decision, hook = _deadlines(a)
    assert decision >= 400 + 60 and hook > decision
    # CONTROL: a seat with no spawner keeps the rpc approval deadline.
    assert _deadlines(_seat(spawner=None))[0] == tool_approval.APPROVAL_TIMEOUT_S + 15


@pytest.mark.asyncio
async def test_K1c_a_cancelled_relay_wait_is_logged(log, tmp_path):
    a = _seat()
    a._hook_source = "scheduled"
    sent = []
    _wire_send(a, sent)
    turn = asyncio.create_task(a._authorize_action("bash", DESTRUCTIVE, tool_policy.SHELL_POLICY,
                                                   workspace=tmp_path))
    await _until_sent(sent)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert [kw["status"] for kw in log] == ["cancelled"]


@pytest.mark.asyncio
async def test_SHOULD_an_answer_written_as_payload_text_is_read():
    a = _seat()
    future = asyncio.get_running_loop().create_future()
    approval_relay._pending(a)["appr-0123456789ab"] = (future, SPAWNER)
    assert approval_relay.take_answer(a, {"from": SPAWNER, "payload": {"text": "APPROVE appr-0123456789ab"}})
    assert future.result() is True


# ── children: the parent answers (collect_turn -> approve_for_child) ───────

class _Stdin:
    def __init__(self):
        self.lines = []

    def write(self, data):
        self.lines.append(data.decode("utf-8"))

    async def drain(self):
        return None


def _child(events, *, delay=0.0):
    process = AgentProcess()
    process.process = SimpleNamespace(stdin=_Stdin(), returncode=None, pid=1)
    queue = list(events)

    async def receive(*, timeout):
        if delay:
            await asyncio.sleep(delay)
        return queue.pop(0)

    process.receive = receive
    return process


REQUEST = {"type": "tool_approval_requested", "id": "appr-child0000001", "tool": "powershell",
           "input": DESTRUCTIVE, "profile": "interactive", "why": "human confirmation required for deletion"}
TURN = [{"type": "turn_start"}, REQUEST, {"type": "text_delta", "text": "done"},
        {"type": "turn_end", "stopReason": "stop"}]


def _answered(process):
    import json
    return [json.loads(line) for line in process.process.stdin.lines]


@pytest.mark.asyncio
async def test_G_an_owner_parents_child_is_approved_by_his_keypress(monkeypatch, log):
    a = _ryans(_seat(spawner=None))
    asked = []

    async def keypress(*_a, **_k):
        asked.append(True)
        return tool_approval.ONCE

    monkeypatch.setattr(m, "show_dialog", keypress)
    child = _child(TURN)
    result = await child.collect_turn(timeout=5, on_approval=a.approve_for_child)
    assert asked == [True]
    assert _answered(child) == [{"type": "approve", "approval_id": REQUEST["id"], "allow": True}]
    assert result["status"] == "completed" and result["summary"] == "done"
    assert [kw["status"] for kw in log] == ["approved"]


@pytest.mark.asyncio
async def test_G_an_owner_GUI_parent_asks_its_host_with_NO_deadline(monkeypatch):
    a = _ryans(_seat(spawner=None, rpc=True))
    seen = {}

    async def host(app, name, args, decision, *, timeout=None):
        seen["timeout"] = timeout
        return tool_approval.ONCE

    monkeypatch.setattr(tool_approval, "approve_over_rpc", host)
    assert await a.approve_for_child(REQUEST) is True
    assert seen["timeout"] == 0, "0 = no deadline: Ryan's keypress is never timed out"


@pytest.mark.asyncio
async def test_G_CONTROL_a_locked_parents_child_goes_to_its_leader_by_inbox(monkeypatch, log):
    a = _seat()
    monkeypatch.setattr(m, "show_dialog", lambda *_a, **_k: pytest.fail("a modal opened"))
    sent = []
    _wire_send(a, sent)
    child = _child(TURN)
    replying = asyncio.create_task(_reply(a, sent, "APPROVE"))
    result = await child.collect_turn(timeout=5, on_approval=a.approve_for_child)
    await replying
    assert sent[0][0] == SPAWNER and "powershell" in sent[0][1]
    assert _answered(child)[0]["allow"] is True and result["status"] == "completed"
    assert [kw["status"] for kw in log] == ["approved"]


@pytest.mark.asyncio
async def test_a_parent_with_no_route_refuses_its_child_and_logs(log):
    a = _seat(spawner=None, agent_launched=True)
    assert await a.approve_for_child(REQUEST) is False
    assert [kw["status"] for kw in log] == ["no_spawner"]


@pytest.mark.asyncio
async def test_C4_clock_the_time_spent_answering_does_not_count_against_the_child():
    async def slow(_event):
        await asyncio.sleep(0.6)
        return True

    child = _child(TURN)
    result = await child.collect_turn(timeout=0.3, on_approval=slow)
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_CONTROL_without_a_relay_the_child_still_requires_one():
    with pytest.raises(LaunchBlocked, match="human relay"):
        await _child(TURN).collect_turn(timeout=5)


@pytest.mark.asyncio
async def test_79e113ce_3_a_child_stopped_by_an_approval_outcome_is_FAILED_with_the_reason():
    reason = "[stopped — leader-4 (the spawning agent) denied powershell]"
    child = _child([{"type": "turn_start"},
                    {"type": "turn_end", "stopReason": "approval", "error": reason}])
    result = await child.collect_turn(timeout=5)
    assert result["status"] == "failed" and result["error"] == reason


@pytest.mark.asyncio
async def test_a_supervised_childs_denied_approval_marks_the_approval_stop_cause(monkeypatch, tmp_path):
    a = _seat(spawner=None, rpc=True, host=True)

    async def host(*_a, **_k):
        return tool_approval.DENIED

    monkeypatch.setattr(tool_approval, "approve_over_rpc", host)
    a._hook_source = "rpc"
    await a._authorize_action("bash", DESTRUCTIVE, tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert a._stop_requested and a._stop_cause == "approval"


@pytest.mark.asyncio
async def test_CONTROL_ryans_own_modal_deny_stays_a_plain_cancel(monkeypatch, tmp_path):
    a = _ryans(_seat(spawner=None))

    async def deny(*_a, **_k):
        return tool_approval.DENIED

    monkeypatch.setattr(m, "show_dialog", deny)
    a._hook_source = "typed"
    await a._authorize_action("bash", DESTRUCTIVE, tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert a._stop_requested and getattr(a, "_stop_cause", None) is None


# ── the child's own clock (C1) and its launch env ──────────────────────────

@pytest.mark.parametrize("raw, expected", [(None, tool_approval.APPROVAL_TIMEOUT_S), ("0", None),
                                           ("5", 5.0), ("oops", tool_approval.APPROVAL_TIMEOUT_S)])
def test_C1_the_child_reads_its_approval_deadline_from_its_launcher(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(tool_approval.APPROVAL_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(tool_approval.APPROVAL_TIMEOUT_ENV, raw)
    assert tool_approval.approval_timeout_s() == expected


@pytest.mark.asyncio
async def test_C1_an_unbounded_rpc_approval_waits_and_says_so(monkeypatch):
    monkeypatch.setenv(tool_approval.APPROVAL_TIMEOUT_ENV, "0")
    emitted = []
    host = SimpleNamespace(_rpc_emit=emitted.append, _active_tool_profile=INTERACTIVE)
    decision = tool_policy.PolicyDecision(tool_policy.CONFIRM, INTERACTIVE, frozenset(), "why")

    async def answer_late():
        await asyncio.sleep(0.2)
        tool_approval.resolve_over_rpc(host, emitted[0]["id"], True)

    late = asyncio.create_task(answer_late())
    answer = await tool_approval.approve_over_rpc(host, "bash", DESTRUCTIVE, decision)
    await late
    assert bool(answer) is True and emitted[0]["timeout_s"] is None


@pytest.mark.asyncio
async def test_the_supervised_child_is_launched_with_the_host_flag_and_its_deadline(tmp_path):
    from litetui.agent_launcher import start_headless_child
    spec = validate_request({"prompt": "p", "backend": "codex", "model": "gpt-6-sol",
                             "workspace": str(tmp_path)}, parent_profile=INTERACTIVE, depth=0)
    captured = {}
    process = AgentProcess()

    async def start_python(*, module, args, cwd, env):
        captured.update(env)
        raise LaunchBlocked("stop here")

    process.start_python = start_python
    process.close = lambda **_: asyncio.sleep(0, result=True)
    with pytest.raises(LaunchBlocked, match="stop here"):
        await start_headless_child(spec, process, workspace=tmp_path, data_root=tmp_path,
                                   supported_levels=None, approval_timeout=0)
    assert captured["LITETUI_APPROVAL_HOST"] == "1"
    assert captured["LITETUI_APPROVAL_TIMEOUT_S"] == "0"


@pytest.mark.parametrize("owner, expected", [(True, 0), (False, 600 + 60)])
@pytest.mark.asyncio
async def test_run_for_app_sets_no_deadline_for_ryans_own_and_the_relay_for_a_locked_parent(
        monkeypatch, owner, expected):
    from litetui import agent_app_runtime
    a = _seat()
    if owner:
        _ryans(a)
    a.store = SimpleNamespace(convo_id="convo", owned=True, pending=False, loading=False)
    a._start_child_delivery = lambda **_: None
    seen = {}

    async def run_prepared_child(*_a, **kw):
        seen.update(kw)
        return "done"

    monkeypatch.setattr(agent_app_runtime, "run_prepared_child", run_prepared_child)
    await agent_app_runtime.run_for_app(a, SimpleNamespace(), AgentProcess(), registry=None, inbox=None,
                                        receipts=None, parent="convo", child_id="c", workspace=None,
                                        data_root=None, branch=None, evidence=[], supported_levels=None)
    assert seen["approval_timeout"] == expected
    assert seen["on_approval"] == a.approve_for_child


def test_a_child_never_inherits_the_parents_spawner(monkeypatch):
    monkeypatch.setenv("LITEHARNESS_SPAWNED_BY", SPAWNER)
    assert "LITEHARNESS_SPAWNED_BY" not in child_process_env()


# ── spawn_agent: an interactive parent WITH a route may spawn ──────────────

def _capture(a):
    from litetui.plugins.spawn_agent_plugin import capture_launch
    try:
        capture_launch(a, {"prompt": "p", "backend": "codex", "model": "gpt-6-sol", "workspace": "."})
    except Exception as exc:  # noqa: BLE001 - only WHICH refusal matters here
        return str(exc)
    return None


@pytest.mark.parametrize("seat", ["own", "spawner", "hand"])
def test_spawn_agent_an_interactive_parent_with_a_route_passes_the_authority_check(monkeypatch, seat):
    monkeypatch.delenv("LITETUI_AGENT_DEPTH", raising=False)
    a = {"own": lambda: _ryans(_seat(spawner=None)), "spawner": lambda: _seat(),
         "hand": lambda: _seat(spawner=None)}[seat]()
    why = _capture(a) or ""
    assert "autonomous parent" not in why and "No approval path" not in why, why


def test_spawn_agent_a_parent_with_no_route_is_refused_by_name(monkeypatch):
    monkeypatch.delenv("LITETUI_AGENT_DEPTH", raising=False)
    assert "No approval path" in (_capture(_seat(spawner=None, agent_launched=True)) or "")
