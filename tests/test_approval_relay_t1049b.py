"""T1049-B1 — a locked seat's CONFIRM goes to the agent that spawned it, by inbox.

Ryan (04169351): "whatever agent spawned the light qi instance should be babysitting it".
Ryan (6e280dd4): typed CONFIRM in an agent-launched seat -> "The launching agent";
an agent-launched LiteGUI's CONFIRM -> "No, launching agent".
Plan ab8969c2, approved by Dijkstra 5ef7612a; Marquee da3eb064 (E2, Q1), 26d5b28c.

Every arm drives the REAL door (`LiteTUI._authorize_action`) on a real LiteTUI with a
destructive argv under INTERACTIVE, so the decision is a CONFIRM. The seat's `send` is
stubbed (the harness is disabled in the suite); answers arrive through
`_receive_mail`, the monitor's per-message step.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

from litetui import app as m
from litetui import approval_relay, runtime_log, seat_authority, tool_policy
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
    return a


def _ryans(a):
    a._spawned_seat, a._owner_seat, a._pty_term = False, True, None
    return a


@pytest.fixture
def wire(monkeypatch):
    """Record sends, log lines, system lines; booby-trap every human door."""
    state = {"sent": [], "log": [], "said": []}
    monkeypatch.setattr(runtime_log, "record",
                        lambda event, **kw: state["log"].append((event, kw)) or True)

    def no_dialog(*_a, **_k):
        raise AssertionError("a modal opened; the spawner owns this CONFIRM")

    monkeypatch.setattr(m, "show_dialog", no_dialog)

    def arm(a, *, send_ok=True, registered=True):
        a.seat.registered = registered

        def send(to, body):
            state["sent"].append((to, body))
            return send_ok

        a.seat.send = send
        a._system = state["said"].append

        async def no_rpc(*_a, **_k):
            raise AssertionError("the rpc host was asked; the spawner owns this CONFIRM")

        monkeypatch.setattr(m.tool_approval, "approve_over_rpc", no_rpc)
        return state

    return arm


async def _door(a, tmp_path, source):
    a._hook_source = source
    return await a._authorize_action("bash", DESTRUCTIVE, tool_policy.SHELL_POLICY, workspace=tmp_path)


async def _until_sent(state, limit=5.0):
    """Bounded: where nothing is ever sent (the pre-B code), fail instead of hang."""
    for _ in range(int(limit / 0.01)):
        if state["sent"]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("nothing was sent to the spawner")


async def _answer(a, state, verb, sender=SPAWNER):
    await _until_sent(state)
    ident = re.search(r"appr-[0-9a-f]{12}", state["sent"][0][1]).group(0)
    delivered = []
    a._deliver_inbox = delivered.append
    a._receive_mail({"from": sender, "body": f"{verb} {ident}"})
    return ident, delivered


def _relay_log(state):
    return [kw for event, kw in state["log"] if event == "approval_relay"]


# ── R3 (i) and M2: every source of a seat WITH a spawner goes to the spawner ─

@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["scheduled", "harness", "child-result", "goal", "typed", "rpc"])
async def test_RELAY_the_spawner_approves_and_the_turn_continues(wire, tmp_path, source):
    a = _seat()
    state = wire(a)
    answering = asyncio.create_task(_answer(a, state, "APPROVE"))
    refusal = await _door(a, tmp_path, source)
    ident, delivered = await answering
    assert refusal is None, refusal
    assert state["sent"][0][0] == SPAWNER
    body = state["sent"][0][1]
    assert body.startswith(f"[APPROVAL {ident}]") and f"APPROVE {ident}" in body and "rm -rf ./build" in body
    assert delivered == [], "the answer was delivered as a turn"
    assert _relay_log(state) == [dict(site="app.authorize", component="relay", operation=source,
                                      name="bash", id=ident, status="approved")]
    assert not a._stop_requested


@pytest.mark.asyncio
async def test_RELAY_a_spawner_DENY_stops_the_turn(wire, tmp_path):
    a = _seat()
    state = wire(a)
    answering = asyncio.create_task(_answer(a, state, "DENY"))
    text, ok = await _door(a, tmp_path, "scheduled")
    await answering
    assert ok is False and a._stop_requested
    assert a._stop_reason.startswith(f"[stopped — {SPAWNER[:8]} (the spawning agent) denied bash")
    assert _relay_log(state)[0]["status"] == "denied"


@pytest.mark.asyncio
async def test_RELAY_ii_an_absent_spawner_is_refused_logged_and_stops(wire, tmp_path):
    """`send` refused by the registry (the spawner is gone): no wait."""
    a = _seat()
    state = wire(a, send_ok=False)
    text, ok = await _door(a, tmp_path, "scheduled")
    assert ok is False and a._stop_requested and "not reachable" in a._stop_reason
    assert [kw["status"] for kw in _relay_log(state)] == ["absent"]


@pytest.mark.asyncio
async def test_RELAY_ii_a_silent_spawner_times_out_logged_and_stops(wire, tmp_path):
    a = _seat()
    a.settings.relay_approval_timeout_s = 0.05
    state = wire(a)
    text, ok = await _door(a, tmp_path, "scheduled")
    assert ok is False and a._stop_requested and "did not answer" in a._stop_reason
    assert [kw["status"] for kw in _relay_log(state)] == ["timeout"]


@pytest.mark.asyncio
async def test_RELAY_an_unregistered_seat_cannot_relay(wire, tmp_path):
    a = _seat()
    state = wire(a, registered=False)
    text, ok = await _door(a, tmp_path, "scheduled")
    assert ok is False and state["sent"] == [] and [kw["status"] for kw in _relay_log(state)] == ["absent"]


@pytest.mark.asyncio
async def test_RELAY_stop_on_denial_False_callers_get_the_refusal_without_a_stop(wire, tmp_path):
    """The Claude native bridge (claude_tools) decides its own stop."""
    a = _seat()
    state = wire(a, send_ok=False)
    a._hook_source = "scheduled"
    text, ok = await a._authorize_action("bash", DESTRUCTIVE, tool_policy.SHELL_POLICY,
                                         workspace=tmp_path, stop_on_denial=False)
    assert ok is False and not a._stop_requested


# ── accidents are not answers ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ACCIDENT_a_reply_from_another_agent_or_to_an_unknown_id_is_ordinary_mail(wire, tmp_path):
    a = _seat()
    a.settings.relay_approval_timeout_s = 0.3
    state = wire(a)
    turn = asyncio.create_task(_door(a, tmp_path, "scheduled"))
    await _until_sent(state)
    ident = re.search(r"appr-[0-9a-f]{12}", state["sent"][0][1]).group(0)
    delivered = []
    a._deliver_inbox = delivered.append
    stray = {"from": "someone-else", "body": f"APPROVE {ident}"}
    unknown = {"from": SPAWNER, "body": "APPROVE appr-000000000000"}
    chatter = {"from": SPAWNER, "body": "hello"}
    for msg in (stray, unknown, chatter):
        a._receive_mail(msg)
    assert delivered == [stray, unknown, chatter]
    text, ok = await turn
    assert ok is False and [kw["status"] for kw in _relay_log(state)] == ["timeout"]
    late = {"from": SPAWNER, "body": f"APPROVE {ident}"}
    a._receive_mail(late)
    assert delivered[-1] is late, "a late answer to an expired id must reach the model as mail"


# ── no spawner ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["typed", "scheduled", "rpc"])
async def test_REFUSE_agent_launched_without_a_spawner_stops_and_logs_never_a_human(wire, tmp_path, source):
    """Ryan 6e280dd4 (b): never the GUI human; typed or rpc, no modal and no host."""
    a = _seat(spawner=None, agent_launched=True, rpc=(source == "rpc"))
    state = wire(a)
    text, ok = await _door(a, tmp_path, source)
    assert ok is False and a._stop_requested and "without naming itself" in a._stop_reason
    assert state["sent"] == [] and [kw["status"] for kw in _relay_log(state)] == ["no_spawner"]


@pytest.mark.asyncio
async def test_HAND_ryans_unmarked_launch_refuses_ONE_action_logs_and_goes_on(wire, tmp_path):
    """Marquee Q1: (d) keeps refuse-one-action; it gains only the log."""
    a = _seat(spawner=None, agent_launched=False)
    state = wire(a)
    text, ok = await _door(a, tmp_path, "scheduled")
    assert ok is False and not a._stop_requested
    assert "nobody is here to confirm" in text
    assert [kw["status"] for kw in _relay_log(state)] == ["no_spawner"]


@pytest.mark.asyncio
async def test_CONTROL_HAND_typed_still_gets_the_modal(monkeypatch, tmp_path):
    a = _seat(spawner=None, agent_launched=False)
    opened = []

    async def dialog(*_a, **_k):
        opened.append(True)
        return None

    monkeypatch.setattr(m, "show_dialog", dialog)
    a._system = lambda *_: None
    await _door(a, tmp_path, "typed")
    assert opened == [True]


# ── Ryan's own: unchanged, and out of B ─────────────────────────────────────

@pytest.mark.asyncio
async def test_CONTROL_ryans_own_unattended_is_refused_with_no_relay_no_log_no_stop(wire, tmp_path):
    a = _ryans(_seat(spawner=SPAWNER))
    state = wire(a)
    text, ok = await _door(a, tmp_path, "scheduled")
    assert ok is False and not a._stop_requested and "nobody is here to confirm" in text
    assert state["sent"] == [] and _relay_log(state) == []


# ── (a) a supervised child's host ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("answer, status", [(True, "approved"), (None, "no_host")])
async def test_HOST_a_supervised_child_asks_its_host_for_every_source_and_logs(monkeypatch, tmp_path, answer, status):
    a = _seat(spawner=SPAWNER, rpc=True, host=True)
    log = []
    monkeypatch.setattr(runtime_log, "record", lambda event, **kw: log.append(kw) or True)
    asked = []

    async def host(app, name, args, decision, **_):
        asked.append(name)
        return m.tool_approval.ONCE if answer else None

    monkeypatch.setattr(m.tool_approval, "approve_over_rpc", host)
    result = await _door(a, tmp_path, "scheduled")
    assert asked == ["bash"]
    assert [kw["status"] for kw in log] == [status]
    assert (result is None) == bool(answer)
    assert a._stop_requested == (not answer)


# ── Ryan (b): an agent-launched LiteGUI with the envelope ───────────────────

@pytest.mark.asyncio
async def test_LITEGUI_an_enveloped_gui_seat_asks_the_launching_agent_not_the_gui(wire, tmp_path):
    a = _seat(spawner=SPAWNER, rpc=True, host=False)
    state = wire(a)   # approve_over_rpc is booby-trapped: the GUI human is never asked
    answering = asyncio.create_task(_answer(a, state, "APPROVE"))
    assert await _door(a, tmp_path, "scheduled") is None
    await answering
    assert state["sent"][0][0] == SPAWNER


# ── E2: the startup record, and what it pops ───────────────────────────────

def _clear(monkeypatch):
    for name in ("LITETUI_SPAWN_IDENTITY", "LITEHARNESS_SPAWNED_BY", "LITETUI_APPROVAL_HOST",
                 "CLAUDE_CODE_SESSION_ID", *seat_authority.AGENT_SHELL_MARKERS):
        monkeypatch.delenv(name, raising=False)


def test_E2_the_spawner_comes_only_from_the_marker_and_both_vars_are_popped(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LITETUI_SPAWN_IDENTITY", "1")
    monkeypatch.setenv("LITEHARNESS_SPAWNED_BY", SPAWNER)
    monkeypatch.setenv("LITETUI_APPROVAL_HOST", "1")
    a = m.LiteTUI()
    assert a._spawner_id == SPAWNER and a._approval_host is True
    assert "LITEHARNESS_SPAWNED_BY" not in os.environ and "LITETUI_APPROVAL_HOST" not in os.environ


def test_E2_M1_no_marker_means_no_spawner_whatever_the_env_says(monkeypatch):
    """The measured stale case: an ambient SPAWNED_BY (a dead leader id) and the
    shell's CLAUDE_CODE_SESSION_ID name nobody without the marker."""
    _clear(monkeypatch)
    monkeypatch.setenv("LITEHARNESS_SPAWNED_BY", "27bec769-a21d-4219-a659-a3dd692260a9")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "e24ec61f-ee78-472e-931b-3ce4819c638e")
    a = m.LiteTUI()
    assert a._spawner_id is None
    assert "LITEHARNESS_SPAWNED_BY" not in os.environ


@pytest.mark.parametrize("marker, expected", [(None, False), ("CLAUDECODE", True), ("CODEX_SANDBOX", True)])
def test_agent_launched_is_read_before_the_seat_exports_its_own_marker(monkeypatch, marker, expected):
    _clear(monkeypatch)
    if marker:
        monkeypatch.setenv(marker, "1")
    assert m.LiteTUI()._agent_launched is expected


# ── B5, D1, the setting ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_B5_steering_admit_re_asks_the_bridge_taint(monkeypatch):
    """codex_steering HostSteering.admit -> floor_refusal -> floor_applies ->
    is_ryans_own(recheck=True) -> is_owner -> pty_taint_clean (a proof, not a fix)."""
    from litetui.codex_steering import HostSteering
    a = _ryans(_seat(spawner=None))
    a._pty_term = "pty-1-1"
    a.hook_config = None
    asked = []
    monkeypatch.setattr(seat_authority, "pty_taint_clean", lambda term, **_: asked.append(term) or False)
    await HostSteering(a, None, "thread", "turn", {}, lambda: None).admit({"content": "x", "source": "harness"})
    assert asked == ["pty-1-1"] and a._owner_seat is False


def test_D1_one_turn_source_and_a_profile_write_does_not_touch_it():
    a = _seat()
    a._hook_source = "scheduled"
    a._active_tool_profile = INTERACTIVE   # a goal, a parent wake, a settings write
    assert a._hook_source == "scheduled"
    assert "_active_turn_source" not in Path(m.__file__).read_text(encoding="utf-8")


def test_the_relay_timeout_is_a_controlled_setting_read_by_the_relay():
    from litetui.settings import Settings
    from litetui.settings_scope import SETTING_SPECS
    from litetui.settings_ui_model import SETTINGS_SECTIONS
    assert Settings().relay_approval_timeout_s == 600
    assert "relay_approval_timeout_s" in SETTING_SPECS
    assert any(f.name == "relay_approval_timeout_s" for s in SETTINGS_SECTIONS for f in s.fields)
    a = _seat()
    a.settings.relay_approval_timeout_s = 42
    assert approval_relay.timeout_s(a) == 42.0
