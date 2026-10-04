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
import json
import re
from types import SimpleNamespace

import pytest

from litetui import app as m
from litetui import approval_relay, harness, runtime_log, seat_authority, tool_approval, tool_policy
from litetui.agent_launcher import LaunchBlocked, validate_request
from litetui.agent_supervisor import AgentProcess, child_process_env
from litetui.tool_policy import INTERACTIVE

SPAWNER = "leader-4f1e2d3c-0000-0000-0000-000000000001"
DESTRUCTIVE = {"command": "rm -rf ./build"}


@pytest.fixture(autouse=True)
def disposable_presence(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "AGENTS_DIR", tmp_path)


def _registered_parent(a, parent):
    if parent:
        (harness.AGENTS_DIR / f"{parent}.json").write_text(json.dumps({"agent_id": parent}), encoding="utf-8")
    a.seat.registered = True
    (harness.AGENTS_DIR / f"{a.seat.agent_id}.json").write_text(
        json.dumps({"agent_id": a.seat.agent_id, "spawned_by": parent}), encoding="utf-8")


def _seat(spawner=SPAWNER, *, agent_launched=False, rpc=False, host=False):
    a = m.LiteTUI()
    a.settings.tool_policy_profile = INTERACTIVE
    a._active_tool_profile = INTERACTIVE
    a._spawned_seat = bool(spawner)
    a._owner_seat = False
    a._spawner_id = spawner
    _registered_parent(a, spawner)
    a._agent_launched = agent_launched
    a._rpc = rpc
    a._approval_host = host
    a._system = lambda *_: None
    from approval_store_fixture_t0340 import bind_origin
    bind_origin(a, harness.AGENTS_DIR / a.seat.agent_id, actual_edit=True)
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
    a.seat.send = lambda to, body, **metadata: sent.append((to, body)) or ok


async def _until_sent(sent, limit=5.0):
    for _ in range(int(limit / 0.01)):
        if sent:
            return re.search(r"appr-[0-9a-f]{12}", sent[0][1]).group(0)
        await asyncio.sleep(0.01)
    raise AssertionError("nothing was sent to the spawner")


async def _reply(a, sent, verb):
    ident = await _until_sent(sent)
    a._receive_mail({"to": a.seat.agent_id, "from": SPAWNER, "body": f"{verb} {ident}"})


# ── T0340: timeout wording is request-scoped, not an operation ban ─────────

TIMEOUT_REQUEST_NOTICE = (
    "This request was not run and will not be retried automatically. "
    "A new request for the same operation is allowed and gets its own approval."
)


@pytest.mark.parametrize("timeout", [600, 42])
def test_timeout_stop_line_allows_a_new_request_but_not_automatic_retry(timeout):
    a = SimpleNamespace(settings=SimpleNamespace(relay_approval_timeout_s=timeout))
    assert approval_relay.stop_line(a, "powershell", "timeout") == (
        "[stopped — the request's spawning agent did not answer the approval "
        f"for powershell within {timeout}s; refused and logged] "
        + TIMEOUT_REQUEST_NOTICE
    )
    # The timeout clarification must not weaken an explicit denial.
    assert approval_relay.stop_line(a, "powershell", "denied") == (
        "[stopped — the request's spawning agent denied powershell]"
    )


@pytest.mark.parametrize("timeout", [600, 42])
def test_relay_message_conditions_request_scoped_notice_on_no_answer(timeout):
    a = SimpleNamespace(seat=SimpleNamespace(name="worker", agent_id="worker-id"))
    decision = SimpleNamespace(danger="write", reason="confirmation", capabilities=frozenset())
    message = approval_relay._message(
        a, "appr-0123456789ab", "powershell", {"command": "operation"},
        decision, "typed", timeout, approver=SPAWNER,
    )
    assert message == (
        "[APPROVAL appr-0123456789ab] worker (worker-i) asks to run powershell "
        "during a typed turn\n"
        "Danger: write; why: confirmation\n"
        'Input: {"command": "operation"}\n'
        f"[DELIVERY requester=worker-id approver={SPAWNER}]\n"
        "Answer by inbox with exactly one line: APPROVE appr-0123456789ab  "
        "or  DENY appr-0123456789ab\n"
        f"If no answer within {timeout} s, the turn stops and this is logged:\n"
        + TIMEOUT_REQUEST_NOTICE
    )


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
    from litetui.textfmt import tool_denied
    assert refusal == tool_denied(
        "profile", name="Bash", reason=approval_relay.stop_line(a, "Bash", "denied"))
    assert "do not retry" in refusal.lower()
    assert TIMEOUT_REQUEST_NOTICE not in refusal
    assert a._stop_requested and a._stop_cause == "approval"
    assert [kw["status"] for kw in log] == ["denied"]


@pytest.mark.parametrize("prompt_state", ["shipped", "missing", "broken"])
@pytest.mark.asyncio
async def test_timeout_final_model_response_is_request_scoped(log, tmp_path, monkeypatch, prompt_state):
    from litetui import paths, textfmt
    a = _seat()
    if prompt_state != "shipped":
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        if prompt_state == "broken":
            # Missing required reason must select the safe timeout fallback,
            # not a profile refusal with a blanket retry ban.
            (prompts / "tool-denied.md").write_text(
                "## approval-timeout\n\n{name}: do not retry\n", encoding="utf-8")
        monkeypatch.setattr(paths, "PROMPTS_DIR", prompts)
    a._hook_source = "scheduled"
    a.settings.relay_approval_timeout_s = 0.08
    sent = []
    _wire_send(a, sent)
    try:
        # Real unanswered relay -> authorization -> final native denial envelope.
        # Only transport is inert; the pre-tool hook never dispatches a command.
        from litetui.claude_tools import ClaudeTools
        a.backend = SimpleNamespace(segment_id="timeout-test")
        bridge = ClaudeTools(a, a.backend, "timeout-test", workspace=tmp_path)
        result = await bridge.pre_tool({"tool_name": "Bash", "tool_input": DESTRUCTIVE}, "call-test", None)
        refusal = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert result == {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": refusal,
        }}
        assert sent and len(sent) == 1
        assert [kw["status"] for kw in log] == ["timeout"]
        assert refusal == (
            "[approval timeout] Bash: Nothing ran and nothing changed. "
            + approval_relay.stop_line(a, "Bash", "timeout")
        )
        assert refusal.endswith(TIMEOUT_REQUEST_NOTICE)
        assert "do not retry" not in refusal.lower()
        assert a._stop_requested and a._stop_cause == "approval"
        assert a._stop_reason == approval_relay.stop_line(a, "Bash", "timeout")
        assert not approval_relay._pending(a)
        # Sibling policy-denial rendering remains unchanged even on fallback.
        expected = (textfmt._tool_denied_sections()["profile"] if prompt_state == "shipped"
                    else textfmt.TOOL_DENIED_FALLBACK["profile"])
        expected = " ".join(expected.replace("{name}", "Bash").replace("{reason}", "floor denial").split())
        assert textfmt.tool_denied("profile", name="Bash", reason="floor denial") == expected
        assert "do not retry" in expected.lower()
    finally:
        a.store.release()


@pytest.mark.asyncio
async def test_immutable_floor_final_response_keeps_retry_ban(log, tmp_path):
    from litetui.claude_tools import ClaudeTools
    from litetui.textfmt import tool_denied
    a = _seat()
    a.backend = SimpleNamespace(segment_id="floor-test")
    args = {"command": "rm -rf /"}
    decision = tool_policy.evaluate(INTERACTIVE, tool_policy.SHELL_POLICY, args,
                                    tmp_path, tool_name="Bash", shell="bash")
    assert decision.action == tool_policy.DENY and "DENY FLOOR" in decision.reason
    try:
        result = await ClaudeTools(a, a.backend, "floor-test", workspace=tmp_path).pre_tool(
            {"tool_name": "Bash", "tool_input": args}, "call-floor", None)
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert reason == tool_denied("profile", name="Bash", reason=decision.reason)
        assert "do not retry" in reason.lower()
        assert TIMEOUT_REQUEST_NOTICE not in reason
        assert log == []  # immutable refusals never ask for approval
    finally:
        a.store.release()


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
    from litetui import approval_authority
    approval_authority.create(a, "appr-0123456789ab", approver=SPAWNER, route="spawner", timeout=600)
    assert approval_relay.take_answer(a, {"to": a.seat.agent_id, "from": SPAWNER, "payload": {"text": "APPROVE appr-0123456789ab"}})
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
    host = _seat(spawner=None, rpc=True)
    host._rpc_emit = emitted.append
    store = host.store
    decision = tool_policy.PolicyDecision(tool_policy.CONFIRM, INTERACTIVE, frozenset(), "why")

    async def answer_late():
        await asyncio.sleep(0.2)
        tool_approval.resolve_over_rpc(host, emitted[0]["id"], True)

    late = asyncio.create_task(answer_late())
    try:
        answer = await tool_approval.approve_over_rpc(host, "bash", DESTRUCTIVE, decision)
        await late
        assert bool(answer) is True and emitted[0]["timeout_s"] is None
    finally:
        if not late.done():
            late.cancel()
        await asyncio.gather(late, return_exceptions=True)
        store.release()


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


@pytest.mark.parametrize("seat, expected", [("own", 0), ("hand", 0), ("spawner", 600 + 60)])
@pytest.mark.asyncio
async def test_run_for_app_sets_no_deadline_for_a_human_and_the_relay_for_a_locked_parent(
        monkeypatch, seat, expected):
    """Dijkstra H1: a keypress is never timed out at Ryan's own parent AND at a
    hand-launch modal; only a parent that relays to its spawner bounds the child."""
    from litetui import agent_app_runtime
    a = {"own": lambda: _ryans(_seat(spawner=None)), "hand": lambda: _seat(spawner=None),
         "spawner": lambda: _seat()}[seat]()
    assert seat_authority.confirm_route(a) == seat
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
