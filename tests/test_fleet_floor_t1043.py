"""T1043 — the SEAT enforces the fleet model/thinking floor on EVERY turn.

Ryan (2026-09-26 17:5x): "this MUST NEVER happen again" (a codex seat ran
gpt-5.6-sol at medium). Every spawn-time check is a snapshot: a reconnect onto
the pin, a LITETUI_NO_HARNESS seat, a /model or /think after launch. Only the
seat sees every turn, so the seat refuses a turn below the floor — nothing is
substituted and nothing is sent.

Rulings: Marquee 7d1e2cd3 (design), aef193e8 (steering), Sentinel a9aa4df0 via
5349c9de (the fallback floor IS the module's default).
conftest points LITESUITE_FLEET_POLICY at an absent file: the built-in default.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import fleet_policy, goal_loop, hook_host, seat_authority
from litetui.app import LiteTUI

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_deny_floor  # noqa: E402


class _Seat:
    """Just enough app for accept_prompt, the resolver and the floor."""

    chosen_tool_profile = LiteTUI.chosen_tool_profile

    def __init__(self, backend="codex", model="gpt-6-sol", thinking="high"):
        self.settings = SimpleNamespace(tool_policy_profile="interactive")
        self._convo_settings = None
        self._cli_tool_profile = None
        self.backend = SimpleNamespace(name=backend, owns_native_turns=False)
        self.model_id, self._thinking_level = model, thinking
        self.conversation: list = []
        self.said: list[str] = []
        self.emitted: list[dict] = []
        self.streams = 0
        self.rejected_prompts: list = []

    def _append(self, message):
        self.conversation.append(message)

    def _system(self, text):
        self.said.append(text)

    def _rpc_emit(self, event):
        self.emitted.append(event)

    def _stream(self):
        self.streams += 1


def _typed(seat, text="hello", source="typed"):
    hook_host.start_prompt(seat, {"content": text, "source": source,
                                  "tool_profile": "interactive"})


# ── one rule set: the copy is liteharness's own ─────────────────────────────

def test_the_copy_is_byte_identical_to_liteharness():
    source = sync_deny_floor.canonical(name="fleet_policy.py")
    if source is None:
        pytest.skip("no liteharness-oss checkout found and LITEHARNESS_SRC unset")
    assert source.is_file(), f"{source} does not exist"
    warnings.warn(f"fleet_policy.py compared against {source}", stacklevel=1)
    assert source.read_bytes() == Path(fleet_policy.__file__).read_bytes(), (
        f"{source} and src/litetui/fleet_policy.py differ: edit the canonical file "
        "and run scripts/sync_deny_floor.py")


# ── the incident, and the ways a spawn-time snapshot misses it ──────────────

def test_a_reconnect_onto_the_pin_REFUSES_the_next_turn():
    """The pin put the seat on gpt-5.6-sol/medium after a reconnect."""
    seat = _Seat(model="gpt-5.6-sol", thinking="medium")
    _typed(seat)

    assert seat.streams == 0 and seat.conversation == [], "a below-floor turn ran"
    [text] = seat.said
    for needle in ("TURN REFUSED", "floors.codex.models", "min_thinking_level",
                   "Nothing was substituted", "built-in default"):
        assert needle in text, (needle, text)
    assert seat.rejected_prompts[0]["reason"] == text, "the prompt was not retained"
    assert seat.emitted == [{"type": "turn_end", "stopReason": "fleet_floor", "error": text}]


def test_a_model_switch_after_launch_then_an_INBOX_turn_is_refused():
    seat = _Seat()
    _typed(seat)
    assert seat.streams == 1, "CONTROL: a seat at the floor runs"
    seat.model_id = "gpt-5.6-sol"            # /model after launch
    _typed(seat, "mail", source="harness")
    assert seat.streams == 1 and "TURN REFUSED" in seat.said[-1]


def test_a_think_change_below_the_floor_is_refused():
    seat = _Seat(thinking="medium")
    _typed(seat)
    assert seat.streams == 0


def test_an_UNKNOWN_model_under_codex_is_refused_by_name():
    seat = _Seat(model="o4-mini")
    _typed(seat)
    assert seat.streams == 0
    assert "'o4-mini' is not in the codex list" in seat.said[-1]


@pytest.mark.parametrize("backend,model,thinking", [
    ("lmstudio", "gpt-oss-120b", None),
    ("lmstudio", "qwen/qwen3.8-27b", None),
    ("llamacpp", "gemma-3-4b-it", "off"),
])
def test_local_and_gpt_oss_seats_are_UNAFFECTED(backend, model, thinking):
    seat = _Seat(backend=backend, model=model, thinking=thinking)
    _typed(seat)
    assert seat.streams == 1 and seat.said == []


def test_the_floor_also_holds_on_a_custom_backend_serving_gpt():
    """match_prefixes govern on ANY backend: custom can be OpenAI itself."""
    seat = _Seat(backend="custom", model="gpt-5.6-sol", thinking="high")
    _typed(seat)
    assert seat.streams == 0


# ── every turn path ──────────────────────────────────────────────────────────

def test_a_queued_item_delivered_at_a_round_boundary_is_refused():
    seat = _Seat(model="gpt-5.6-sol")
    seat._pending_input = [{"content": "queued", "source": "queued"}]
    seat._stop_requested = False
    assert LiteTUI._deliver_queued_input(seat) is False
    assert seat.conversation == []


def test_a_goal_continuation_on_a_below_floor_seat_appends_nothing():
    seat = _Seat(model="gpt-5.6-sol")
    seat._chat_running = lambda: False
    bubbles = []
    seat._user_bubble = lambda *a, **k: bubbles.append(a)
    state = goal_loop.GoalState(objective="ship it", tool_profile="interactive")
    goal_loop._deliver_goal_turn(seat, state, "continue")
    assert (seat.conversation, bubbles, seat.streams) == ([], [], 0)


@pytest.mark.asyncio
async def test_a_child_result_wake_on_a_below_floor_seat_claims_nothing(monkeypatch):
    from litetui import agent_parent_wake

    seat = _Seat(model="gpt-5.6-sol")
    seat._chat_running = lambda: False
    seat._pending_input, seat._stop_requested, seat.convo_id = [], False, "c1"
    seat.store = SimpleNamespace(convo_id="c1", owned=True, pending=False, loading=False,
                                 convo_path="p", read=lambda p: (None, seat.conversation))
    monkeypatch.setattr(hook_host, "snapshot", lambda app: SimpleNamespace(hooks=[], error=None))
    claimed = []
    receipts = SimpleNamespace(claim_wake=lambda *a: claimed.append(a) or ["r1"])

    assert await agent_parent_wake.wake_parent(seat, parent="p", receipts=receipts) == []
    assert claimed == [] and seat.streams == 0, "the wake was claimed on a below-floor seat"
    assert "TURN REFUSED" in seat.said[-1]


@pytest.mark.asyncio
async def test_a_STEERED_item_on_a_below_floor_seat_is_denied_and_never_sent():
    """Marquee aef193e8: refused at HostSteering.admit, so the app-server
    receives NO steer request, and the denied path retains it."""
    from litetui.codex_steering import HostSteering

    seat = _Seat(model="gpt-5.6-sol")
    seat._stop_requested = False
    sent = []

    async def request(method, params):
        sent.append(method)
        return {"turnId": "turn"}

    steering = HostSteering(seat, SimpleNamespace(request=request), "thread", "turn", {}, lambda: None)
    item = {"content": "steer me", "source": "queued", "tool_profile": "interactive"}
    entry = steering.ledger.enqueue(item, "thread", "turn")
    state = await steering.ledger.deliver(entry, admit=steering.admit, request=request)

    assert state == "denied" and sent == [], (state, sent)
    assert entry["admission"]["reason"].startswith("TURN REFUSED")


def test_a_FORGED_accepted_state_is_not_a_bypass():
    """Marquee aef193e8. The only skip is the native_accepted KEYWORD, passed by
    codex_steering.accept_steered alone; item data saying "accepted" is checked."""
    seat = _Seat(model="gpt-5.6-sol")
    forged = {"content": "x", "source": "queued",
              "_codex_entry": {"id": "e", "threadId": "t", "state": "accepted"}}
    assert hook_host.accept_prompt(seat, forged) is False
    assert seat.conversation == []


# ── the policy file ──────────────────────────────────────────────────────────

def test_a_MALFORMED_policy_file_warns_and_the_seat_keeps_the_default_floor(tmp_path, monkeypatch):
    """Marquee 7d1e2cd3: asymmetric with spawn (which refuses everything) so a
    JSON typo can never lock Ryan out of his own local LiteTUI."""
    bad = tmp_path / "fleet-policy.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(fleet_policy.POLICY_ENV, str(bad))

    local = _Seat(backend="lmstudio", model="gpt-oss-120b", thinking=None)
    _typed(local)
    assert local.streams == 1, "a malformed policy file locked out a local seat"
    assert any("malformed" in t or "unreadable" in t for t in local.said), local.said

    below = _Seat(model="gpt-5.6-sol", thinking="medium")
    _typed(below)
    assert below.streams == 0, "a malformed file loosened the floor"


def test_the_fallback_floor_IS_the_modules_default(tmp_path, monkeypatch):
    """Sentinel a9aa4df0: the same OBJECT, so a future change to the default can
    never silently loosen the seat."""
    bad = tmp_path / "fleet-policy.json"
    bad.write_text("[]", encoding="utf-8")
    monkeypatch.setenv(fleet_policy.POLICY_ENV, str(bad))
    policy, where = seat_authority.seat_policy(_Seat())
    assert policy is fleet_policy.DEFAULT_POLICY
    assert "built-in default" in where


def test_the_malformed_warning_is_said_ONCE_not_every_turn(tmp_path, monkeypatch):
    bad = tmp_path / "fleet-policy.json"
    bad.write_text("{", encoding="utf-8")
    monkeypatch.setenv(fleet_policy.POLICY_ENV, str(bad))
    seat = _Seat(backend="lmstudio", model="qwen", thinking=None)
    _typed(seat)
    _typed(seat)
    assert len(seat.said) == 1, seat.said


def test_a_policy_file_that_RAISES_the_floor_is_honoured(tmp_path, monkeypatch):
    import json
    stricter = json.loads(json.dumps(fleet_policy.DEFAULT_POLICY))
    stricter["floors"]["codex"]["min_model"] = "gpt-6-astra"
    path = tmp_path / "fleet-policy.json"
    path.write_text(json.dumps(stricter), encoding="utf-8")
    monkeypatch.setenv(fleet_policy.POLICY_ENV, str(path))
    seat = _Seat(model="gpt-6-sol")
    _typed(seat)
    assert seat.streams == 0 and str(path) in seat.said[-1]


# ── connect ──────────────────────────────────────────────────────────────────

def test_a_connect_below_the_floor_warns_before_anyone_types():
    seat = _Seat(model="gpt-5.6-sol", thinking="medium")
    seat_authority.warn_if_below_floor(seat)
    assert "every turn will be refused" in seat.said[-1]


def test_CONTROL_a_connect_at_the_floor_is_quiet():
    seat = _Seat()
    seat_authority.warn_if_below_floor(seat)
    assert seat.said == []


# ── review round (Dijkstra ef12509e, via Marquee 7015de6f) ───────────────────

def test_B1_a_skill_on_a_below_floor_seat_starts_no_turn(monkeypatch):
    """/skill appends and calls _stream itself (skills_plugin._invoke)."""
    from litetui.plugins import skills_plugin

    monkeypatch.setattr(skills_plugin.skills_mod, "load", lambda skills, want: "do the thing")
    seat = _Seat(model="gpt-5.6-sol")
    seat.skills, bubbles = [], []
    seat._user_bubble = lambda *a, **k: bubbles.append(a)
    skills_plugin._invoke(seat, "demo")
    assert (seat.conversation, bubbles, seat.streams) == ([], [], 0)
    assert "TURN REFUSED" in seat.said[-1]


def test_B2_the_post_compact_wake_on_a_below_floor_seat_starts_no_turn():
    seat = _Seat(model="gpt-5.6-sol")
    seat._chat_running = lambda: False
    seat._pending_input, seat._turn_abandoned = [], False
    seat._materialise_convo = lambda: None
    seat._user_bubble = lambda *a, **k: None
    LiteTUI._wake_after_compact(seat)
    assert seat.conversation == [] and seat.streams == 0
    assert len(seat.said) == 1 and "TURN REFUSED" in seat.said[0]


class _Overridden(_Seat):
    """A codex seat whose /modelcfg reasoning_effort overrides /think."""

    _effective_request_overrides = LiteTUI._effective_request_overrides

    def __init__(self, override, thinking):
        super().__init__(model="gpt-6-sol", thinking=thinking)
        self._cli_effective_thinking = None
        self._launch_options = None
        self.backend.request_overrides = lambda key: {"reasoning_effort": override}


def test_E1_the_floor_judges_the_effort_the_request_SENDS():
    """/modelcfg reasoning_effort "medium" + /think high on gpt-6-sol passed the
    floor and SENT medium (chat_request: overrides' effort, else thinking_level)."""
    seat = _Overridden("medium", "high")
    assert seat_authority.effective_thinking(seat) == "medium"
    _typed(seat)
    assert seat.streams == 0 and "'medium' is below high" in seat.said[-1]


def test_E1_CONTROL_an_override_AT_the_floor_runs_under_a_low_think():
    seat = _Overridden("high", "low")
    _typed(seat)
    assert seat.streams == 1


def test_E1_presence_reports_the_effort_the_request_sends():
    from litetui import app as app_mod
    seat = _Overridden("medium", "high")
    seat.seat = SimpleNamespace()
    app_mod._sync_seat_resolution(seat)
    assert seat.seat.thinking_level == "medium"


@pytest.mark.asyncio
async def test_Q3_a_refused_child_is_a_FAILED_child_carrying_the_reason():
    """Was LaunchBlocked("Child completed without a started turn"): the parent
    never saw why."""
    from litetui.agent_supervisor import AgentProcess

    why = "TURN REFUSED: FLEET FLOOR: model 'gpt-5.6-sol' is below gpt-6-sol."
    events = [{"type": "turn_end", "stopReason": "fleet_floor", "error": why}]
    child = AgentProcess()

    async def receive(*, timeout):
        return events.pop(0)

    child.receive = receive
    result = await child.collect_turn(timeout=5)
    assert result == {"status": "failed", "summary": why, "stop_reason": "fleet_floor", "error": why}


@pytest.mark.asyncio
async def test_Q3_CONTROL_any_other_unstarted_turn_end_still_blocks():
    from litetui.agent_launcher import LaunchBlocked
    from litetui.agent_supervisor import AgentProcess

    child = AgentProcess()

    async def receive(*, timeout):
        return {"type": "turn_end", "stopReason": "stop"}

    child.receive = receive
    with pytest.raises(LaunchBlocked, match="without a started turn"):
        await child.collect_turn(timeout=5)


def test_Q3_the_seat_emits_through_the_one_turn_end_door():
    seat = _Seat(model="gpt-5.6-sol")
    ends = []
    seat._emit_turn_end = lambda reason, tps, source=None, **extra: ends.append((reason, extra))
    _typed(seat)
    assert ends == [("fleet_floor", {"error": seat.said[-1]})]


def test_E1_the_fallback_is_UNREACHABLE_in_the_real_app():
    """Marquee 04768348: effective_thinking's fallback (no request builder) exists
    only for partial test hosts. The real class always has the builder, so in
    production the floor judges exactly what chat_request sends."""
    assert callable(getattr(LiteTUI, "_effective_request_overrides", None))


# ── scope: Ryan, form 4 (via Marquee 1e92852f): "no leave that unchanged no
#    warning nothing". His OWN instance (not spawned): the turns he drives are
#    exempt with no text at all; unattended turns and spawned seats stay enforced.

def _ryans(**kw):
    seat = _Seat(model="gpt-5.6-sol", thinking="medium", **kw)
    seat._spawned_seat = False
    return seat


def _no_floor_text(seat):
    return not any("TURN REFUSED" in t or "fleet floor" in t for t in seat.said)


def test_RYAN_his_typed_turn_goes_through_with_NO_floor_text():
    seat = _ryans()
    _typed(seat)
    assert seat.streams == 1 and _no_floor_text(seat), seat.said


def test_RYAN_his_skill_goes_through_with_NO_floor_text(monkeypatch):
    from litetui.plugins import skills_plugin
    monkeypatch.setattr(skills_plugin.skills_mod, "load", lambda skills, want: "do it")
    seat = _ryans()
    seat.skills = []
    seat._user_bubble = lambda *a, **k: None
    skills_plugin._invoke(seat, "demo")
    assert seat.streams == 1 and _no_floor_text(seat), seat.said


def test_RYAN_the_wake_after_his_compact_goes_through_with_NO_floor_text():
    seat = _ryans()
    seat._chat_running = lambda: False
    seat._pending_input, seat._turn_abandoned = [], False
    seat._materialise_convo = lambda: None
    seat._user_bubble = lambda *a, **k: None
    LiteTUI._wake_after_compact(seat)
    assert seat.streams == 1 and _no_floor_text(seat), seat.said


@pytest.mark.parametrize("source", ["harness", "scheduled", "rpc", "child-result"])
def test_RYAN_an_UNATTENDED_turn_in_his_own_instance_is_still_REFUSED(source):
    seat = _ryans()
    _typed(seat, "mail", source=source)
    assert seat.streams == 0 and "TURN REFUSED" in seat.said[-1]


def test_RYAN_a_goal_continuation_in_his_own_instance_is_still_REFUSED():
    seat = _ryans()
    seat._chat_running = lambda: False
    seat._user_bubble = lambda *a, **k: None
    goal_loop._deliver_goal_turn(seat, goal_loop.GoalState(objective="x"), "continue")
    assert seat.streams == 0 and "TURN REFUSED" in seat.said[-1]


def test_a_SPAWNED_seats_typed_turn_is_REFUSED():
    seat = _Seat(model="gpt-5.6-sol", thinking="medium")
    seat._spawned_seat = True
    _typed(seat)
    assert seat.streams == 0 and "TURN REFUSED" in seat.said[-1]


def test_the_connect_warning_is_said_in_a_SPAWNED_seat_and_NOT_in_ryans():
    spawned = _Seat(model="gpt-5.6-sol", thinking="medium")
    spawned._spawned_seat = True
    seat_authority.warn_if_below_floor(spawned)
    assert "every turn will be refused" in spawned.said[-1]

    ryans = _ryans()
    seat_authority.warn_if_below_floor(ryans)
    assert ryans.said == []


def test_the_app_records_spawned_BEFORE_the_marker_is_consumed(monkeypatch):
    """spawned_seat_identity pops LITETUI_SPAWN_IDENTITY, so the app must read it
    first; otherwise every seat would look like Ryan's own instance."""
    from litetui import harness
    monkeypatch.setenv(harness.SPAWN_IDENTITY_MARKER, "1")
    spawned = LiteTUI()
    assert spawned._spawned_seat is True
    assert harness.SPAWN_IDENTITY_MARKER not in os.environ, "CONTROL: the marker was consumed"
    assert LiteTUI()._spawned_seat is False, "Ryan's own launch read as spawned"


def test_RYAN_an_UNLABELLED_item_in_his_own_instance_is_REFUSED():
    """Marquee f1e1c415: "queued" must come only from a typed submit. An item with
    no source is "unlabelled" (hook_host._turn_source), which is unattended."""
    seat = _ryans()
    hook_host.start_prompt(seat, {"content": "x", "tool_profile": "interactive"})
    assert seat.streams == 0 and "TURN REFUSED" in seat.said[-1]


@pytest.mark.asyncio
async def test_RYAN_a_goal_continuation_STEERED_through_the_ledger_is_REFUSED():
    """The steering ledger copies content/text/source/tool_profile/operation_id and
    DROPS goal_continuation, so a steered goal item used to arrive as "queued"."""
    from litetui.codex_steering import HostSteering

    seat = _ryans()
    seat._stop_requested = False
    sent = []

    async def request(method, params):
        sent.append(method)
        return {"turnId": "turn"}

    steering = HostSteering(seat, SimpleNamespace(request=request), "thread", "turn", {}, lambda: None)
    goal_item = {"content": "[goal continuation]", "text": "g", "tool_profile": "interactive",
                 "goal_continuation": True}
    entry = steering.ledger.enqueue(goal_item, "thread", "turn")
    assert "goal_continuation" not in entry["item"], "premise: the ledger drops the flag"
    assert await steering.ledger.deliver(entry, admit=steering.admit, request=request) == "denied"
    assert sent == []


def test_RYAN_a_typed_item_steered_through_the_ledger_stays_ATTENDED():
    """CONTROL: the label that survives the ledger is the one a typed submit set."""
    seat = _ryans()
    assert seat_authority.floor_refusal(seat, hook_host._turn_source({"source": "queued"})) is None


def test_RYAN_his_reply_to_a_codex_question_goes_through_with_NO_floor_text():
    """codex_async_questions' "shared human UI": Ryan typed the answer."""
    seat = _ryans()
    _typed(seat, "yes, go", source="codex-question")
    assert seat.streams == 1 and _no_floor_text(seat), seat.said


# ── LiteGUI host (Marquee f2af2bcc): "rpc" is attended only in a hello'd,
#    non-spawned instance. gui.hello is a host claim, not authentication.

def _litegui(**kw):
    seat = _ryans(**kw)
    seat._gui_rpc_enabled = True
    return seat


def test_LITEGUI_a_hellod_non_spawned_rpc_turn_passes_with_NO_floor_text():
    seat = _litegui()
    _typed(seat, "from the LiteGUI composer", source="rpc")
    assert seat.streams == 1 and _no_floor_text(seat), seat.said


def test_LITEGUI_a_non_hello_rpc_child_is_REFUSED():
    """agent_supervisor drives subagent children over rpc and strips their spawn
    marker, so a non-spawned rpc instance without the handshake is agent-driven."""
    seat = _ryans()
    _typed(seat, "from a parent agent", source="rpc")
    assert seat.streams == 0 and "TURN REFUSED" in seat.said[-1]


def test_LITEGUI_a_SPAWNED_hellod_seat_is_REFUSED():
    seat = _litegui()
    seat._spawned_seat = True
    _typed(seat, "x", source="rpc")
    assert seat.streams == 0 and "TURN REFUSED" in seat.said[-1]


@pytest.mark.parametrize("source", ["harness", "scheduled", "child-result"])
def test_LITEGUI_unattended_turns_in_a_hellod_instance_are_still_REFUSED(source):
    seat = _litegui()
    _typed(seat, "mail", source=source)
    assert seat.streams == 0 and "TURN REFUSED" in seat.said[-1]
