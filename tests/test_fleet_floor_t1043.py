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
    """Ryan's own instance: not spawned AND owner-marked (LITETUI_OWNER, finding F)."""
    seat = _Seat(model="gpt-5.6-sol", thinking="medium", **kw)
    seat._spawned_seat = False
    seat._owner_seat = True
    return seat


def _no_floor_text(seat):
    return not any("TURN REFUSED" in t or "fleet floor" in t for t in seat.said)


@pytest.mark.parametrize("source", sorted(seat_authority.ATTENDED_SOURCES))
def test_RYAN_his_typed_turn_goes_through_with_NO_floor_text(source):
    """Dijkstra cycle 2: EVERY attended source, so dropping one turns this red."""
    seat = _ryans()
    _typed(seat, source=source)
    assert seat.streams == 1 and _no_floor_text(seat), (source, seat.said)


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


# ── cycle 2 (Dijkstra 7905c858 via Marquee ac75c2c6) ────────────────────────
#    S1: "spawned" is a FACT OF THE CONVERSATION (convo "seat_spawned").

from litetui import app as _app_mod  # noqa: E402
from litetui import convo_settings as _cs  # noqa: E402
from litetui import settings as _st  # noqa: E402
from test_t1027_one_resolver import _BornHost  # noqa: E402


def _born(tmp_path, marker):
    host = _BornHost(_st.Settings(backend="codex"), tmp_path, "codex")
    host._spawned_marker = marker
    host._adopt_convo_settings(born=True)
    return host


def _resumed(tmp_path, monkeypatch, marker):
    monkeypatch.setattr(_app_mod.llm_backend, "make_backend", lambda s: SimpleNamespace(name=s.backend))
    host = _BornHost(_st.Settings(backend="codex"), tmp_path, "codex")
    host._spawned_marker = marker
    host._owner_seat = True          # resumed by Ryan's own (owner-marked) process
    host._adopt_convo_settings(born=False)
    host._model_id, host._thinking_level = "gpt-5.6-sol", "medium"
    return host


def test_S1_a_convo_BORN_spawned_resumed_WITHOUT_the_marker_is_REFUSED(tmp_path, monkeypatch):
    """The SpawnSmith case: `litetui --convo <id>` typed in a pane, /pty/talk,
    fleet.py send. It is also what happens when Ryan resumes a fleet seat's
    conversation himself: it IS a fleet conversation, so it stays enforced."""
    assert _born(tmp_path, True)._spawned_seat is True
    assert _cs.load(tmp_path).seat_spawned is True
    host = _resumed(tmp_path, monkeypatch, False)
    assert host._spawned_seat is True
    assert seat_authority.floor_refusal(host, "typed") is not None


def test_S1_CONTROL_a_convo_born_unmarked_resumed_unmarked_passes_silently(tmp_path, monkeypatch):
    assert _born(tmp_path, False)._spawned_seat is False
    assert _cs.load(tmp_path).seat_spawned is False
    host = _resumed(tmp_path, monkeypatch, False)
    assert host._spawned_seat is False
    assert seat_authority.floor_refusal(host, "typed") is None


def test_S1_the_marker_still_wins_over_an_unmarked_convo(tmp_path, monkeypatch):
    _born(tmp_path, False)
    host = _resumed(tmp_path, monkeypatch, True)
    assert host._spawned_seat is True


def test_S1_a_malformed_seat_spawned_is_a_diagnostic_not_a_value(tmp_path):
    import json
    _cs.path_for(tmp_path).write_text(json.dumps({"seat_spawned": "yes"}), encoding="utf-8")
    loaded = _cs.load(tmp_path)
    assert loaded.seat_spawned is None and loaded._diagnostics


# ── test gap: a message Ryan types while a turn runs is held, then flushed ────

def _below_floor_backend():
    return SimpleNamespace(name="codex", owns_native_turns=False,
                           request_overrides=lambda key: {}, shutdown=lambda *a, **k: None)


@pytest.mark.asyncio
async def test_RYAN_a_message_typed_while_BUSY_is_held_then_flushed_and_PASSES(monkeypatch):
    """Drives the real _submit_text (held as "queued") and _flush_pending_input."""
    monkeypatch.setenv("LITETUI_OWNER", "1")
    app = LiteTUI()
    app._connect = lambda: None
    assert app._spawned_seat is False
    async with app.run_test(size=(110, 40)):
        real = app._backend
        app._backend = _below_floor_backend()
        app._model_id, app._thinking_level = "gpt-5.6-sol", "medium"
        said, streams = [], []
        app._system = said.append
        app._stream = lambda: streams.append(1)
        app._chat_running = lambda: True
        try:
            app._submit_text("while you work", alt_chord=False)
            assert [item["source"] for item in app._pending_input] == ["queued"]
            app._chat_running = lambda: False
            app._flush_pending_input()
            assert streams == [1], "the held message did not run"
            assert not any("TURN REFUSED" in t or "fleet floor" in t for t in said), said
        finally:
            app._backend = real


@pytest.mark.asyncio
async def test_RYAN_a_mark_taken_while_BUSY_is_held_as_typed_then_PASSES(tmp_path, monkeypatch):
    """The queued /mark item (app._mark_wait) is labelled "typed", so it is his."""
    import json
    handoff = tmp_path / "mark.json"
    handoff.write_text(json.dumps({"x": 1, "y": 2, "mon": 0, "mon_x": 1, "mon_y": 2,
                                   "png": str(tmp_path / "fixture.png")}))
    monkeypatch.setattr("litetui.app.appsvc.load_image_file", lambda *a: "fixture-image")
    monkeypatch.setenv("LITETUI_OWNER", "1")
    app = LiteTUI()
    app._connect = lambda: None
    async with app.run_test(size=(110, 40)):
        real = app._backend
        app._backend = _below_floor_backend()
        app._model_id, app._thinking_level = "gpt-5.6-sol", "medium"
        said, streams = [], []
        app._system = said.append
        app._stream = lambda: streams.append(1)
        app._chat_running = lambda: True
        try:
            await app._mark_wait(handoff, None).wait()
            assert [item.get("source") for item in app._pending_input] == ["typed"]
            app._chat_running = lambda: False
            app._flush_pending_input()
            assert streams == [1]
            assert not any("TURN REFUSED" in t for t in said), said
        finally:
            app._backend = real


# ── small items ──────────────────────────────────────────────────────────────

def test_a_refused_GOAL_turn_says_goal_loops_run_unattended():
    """Marquee's ruling (a): the first goal delivery below the floor is refused,
    and says why plainly."""
    seat = _ryans()
    seat._chat_running = lambda: False
    seat._user_bubble = lambda *a, **k: None
    goal_loop._deliver_goal_turn(seat, goal_loop.GoalState(objective="x"), "continue")
    assert "goal loops run unattended and meet the fleet floor" in seat.said[-1].lower()


def test_an_rpc_turn_refused_for_want_of_gui_hello_says_so():
    seat = _ryans()
    _typed(seat, "x", source="rpc")
    assert "(rpc host did not identify)" in seat.said[-1]


def test_the_effective_thinking_EXCEPT_branch_matches_the_no_builder_branch():
    """Dijkstra nit: a builder that raises falls back like no builder at all."""
    seat = _Seat(thinking="medium")
    seat._cli_effective_thinking = "high"

    def boom():
        raise RuntimeError("unbuildable")

    seat._effective_request_overrides = boom
    assert seat_authority.effective_thinking(seat) == "high"


# ── Ryan, liteask a-04692a60 (answer 545c38e9): "Exempt it: my /goal is mine" ──

def _goal_seat(**kw):
    seat = _ryans(**kw)
    seat._user_bubble = lambda *a, **k: None
    return seat


def _ryans_goal():
    return goal_loop.GoalState(objective="ship it", started_by="typed")


def test_GOAL_a_ryan_started_goal_TURN_1_passes_with_NO_floor_text():
    seat = _goal_seat()
    seat._chat_running = lambda: False
    goal_loop._deliver_goal_turn(seat, _ryans_goal(), "start")
    assert seat.streams == 1 and _no_floor_text(seat), seat.said


def test_GOAL_a_ryan_started_goal_TURN_2_continuation_passes():
    """Every continuation, not just turn 1: the queued item carries the loop's
    origin in its SOURCE ("goal-ryan")."""
    seat = _goal_seat()
    seat._chat_running = lambda: True
    seat._pending_input = []
    goal_loop._deliver_goal_turn(seat, _ryans_goal(), "continue")
    [item] = seat._pending_input
    assert item["source"] == "goal-ryan"
    hook_host.start_prompt(seat, item)
    assert seat.streams == 1 and _no_floor_text(seat), seat.said


@pytest.mark.asyncio
async def test_GOAL_a_ryan_continuation_STEERED_through_the_ledger_passes():
    """The ledger keeps "source" and drops goal_continuation; the origin survives."""
    from litetui.codex_steering import HostSteering

    seat = _goal_seat()
    seat._chat_running, seat._pending_input, seat._stop_requested = (lambda: True), [], False
    goal_loop._deliver_goal_turn(seat, _ryans_goal(), "continue")
    [item] = seat._pending_input
    sent = []

    async def request(method, params):
        sent.append(method)
        return {"turnId": "turn"}

    steering = HostSteering(seat, SimpleNamespace(request=request), "thread", "turn", {}, lambda: None)
    entry = steering.ledger.enqueue(item, "thread", "turn")
    assert entry["item"]["source"] == "goal-ryan" and "goal_continuation" not in entry["item"]
    allowed, context = await steering.admit(entry["item"])
    assert allowed, context


def test_GOAL_CONTROL_a_goal_in_a_SPAWNED_seat_is_refused_at_TURN_1_with_the_text():
    seat = _Seat(model="gpt-5.6-sol", thinking="medium")
    seat._spawned_seat = True
    seat._chat_running = lambda: False
    seat._user_bubble = lambda *a, **k: None
    goal_loop._deliver_goal_turn(seat, _ryans_goal(), "start")
    assert seat.streams == 0
    assert "Goal loops run unattended and meet the fleet floor." in seat.said[-1]


@pytest.mark.parametrize("started_by", ["", "rpc", "unknown"])
def test_GOAL_a_goal_NOT_started_by_ryan_is_refused_in_his_instance(started_by):
    """A goal saved before started_by existed (""), or issued by an rpc host that
    did not identify, is not his."""
    seat = _goal_seat()
    seat._chat_running = lambda: False
    goal_loop._deliver_goal_turn(seat, goal_loop.GoalState(objective="x", started_by=started_by), "go")
    assert seat.streams == 0 and "TURN REFUSED" in seat.said[-1]


def test_GOAL_origin_reads_typed_gui_and_rpc():
    seat = _ryans()
    seat._command_source = "typed"
    assert seat_authority.command_origin(seat) == "typed"
    seat._command_source = "rpc"
    assert seat_authority.command_origin(seat) == "rpc"
    seat._gui_rpc_enabled = True
    assert seat_authority.command_origin(seat) == "gui"


def test_GOAL_goal_command_records_the_origin(monkeypatch):
    seat = _goal_seat()
    seat.backend.owns_native_turns = False
    seat.convo_dir = None
    seat._materialise_convo = lambda: None
    seat._chat_running = lambda: True
    seat._pending_input = []
    saved = []
    monkeypatch.setattr(goal_loop, "save_goal", lambda d, state: saved.append(state))
    monkeypatch.setattr(goal_loop, "load_goal", lambda d: None)
    seat._command_source = "typed"
    goal_loop.goal_command(seat, "ship it")
    assert saved[-1].started_by == "typed"
    assert seat._pending_input[-1]["source"] == "goal-ryan"


@pytest.mark.asyncio
async def test_GOAL_submit_scopes_the_command_origin_to_its_dispatch():
    app = LiteTUI()
    app._connect = lambda: None
    seen = []
    async with app.run_test(size=(110, 40)):
        app._handle_command = lambda text: seen.append(seat_authority.command_origin(app))
        app._submit_text("/goal ship it", alt_chord=False)
        assert seen == ["typed"]
        assert app._command_source is None


# ── finding F (Dijkstra 90a4ba0c): POSITIVE owner identification ─────────────
#    "not spawned" is not "Ryan's own": 102 of 105 convos on disk carry no marker,
#    fleet seats launched by typing `litetui` into a pane included.

def test_F_an_unmarked_NON_OWNER_instances_typed_turn_is_REFUSED():
    """The case F found: a fleet seat typed into a pane (OpenBolt, CyanBrace, ...):
    no spawn marker, no owner mark. Its --prompt / pane-typed turns meet the floor."""
    seat = _Seat(model="gpt-5.6-sol", thinking="medium")
    seat._spawned_seat = False           # no LITETUI_SPAWN_IDENTITY
    _typed(seat)
    assert seat.streams == 0 and "TURN REFUSED" in seat.said[-1]


def test_F_an_unmarked_non_owner_instances_goal_and_skill_are_REFUSED(monkeypatch):
    from litetui.plugins import skills_plugin
    monkeypatch.setattr(skills_plugin.skills_mod, "load", lambda skills, want: "do it")
    seat = _Seat(model="gpt-5.6-sol", thinking="medium")
    seat._spawned_seat = False
    seat.skills, seat._user_bubble = [], (lambda *a, **k: None)
    skills_plugin._invoke(seat, "demo")
    seat._chat_running = lambda: False
    goal_loop._deliver_goal_turn(seat, goal_loop.GoalState(objective="x", started_by="typed"), "go")
    assert seat.streams == 0 and len([t for t in seat.said if "TURN REFUSED" in t]) == 2


def test_F_the_connect_warning_is_said_in_an_unmarked_non_owner_instance():
    seat = _Seat(model="gpt-5.6-sol", thinking="medium")
    seat._spawned_seat = False
    seat_authority.warn_if_below_floor(seat)
    assert "every turn will be refused" in seat.said[-1]


def test_F_OWNER_marked_AND_spawned_is_REFUSED_spawned_wins():
    seat = _ryans()
    seat._spawned_seat = True
    _typed(seat)
    assert seat.streams == 0 and "TURN REFUSED" in seat.said[-1]


def test_F_an_OWNER_marked_instance_keeps_every_exemption(monkeypatch):
    """typed/queued/interrupted/wakes/codex-question/goal-ryan (the parametrized arm
    covers each ATTENDED source), and here: hello'd rpc and /goal turn 2."""
    seat = _ryans()
    seat._gui_rpc_enabled = True
    _typed(seat, "gui", source="rpc")
    _typed(seat, "turn 2", source="goal-ryan")
    assert seat.streams == 2 and _no_floor_text(seat), seat.said


def test_F_the_app_CONSUMES_the_owner_mark(monkeypatch):
    """Read once and popped: no shell, tool or child of Ryan's process inherits it."""
    from litetui import harness
    monkeypatch.setenv(harness.OWNER_MARKER, "1")
    app = LiteTUI()
    assert app._owner_seat is True
    assert harness.OWNER_MARKER not in os.environ
    assert LiteTUI()._owner_seat is False, "a launch without the mark read as Ryan's"


def test_F_the_owner_mark_does_NOT_leak_into_a_managed_child(monkeypatch):
    from litetui.agent_supervisor import child_process_env
    monkeypatch.setenv("LITETUI_OWNER", "1")
    assert "LITETUI_OWNER" not in child_process_env()
    assert "LITETUI_OWNER" not in child_process_env({"LITETUI_OWNER": "1"}), (
        "an explicit override re-granted ownership to a child")


def test_F_the_owner_mark_is_PROCESS_ONLY_not_a_conversation_fact(tmp_path, monkeypatch):
    """A fleet agent resuming one of Ryan's conversations is enforced: nothing about
    ownership is written to .convos/<id>/settings.json."""
    host = _BornHost(_st.Settings(backend="codex"), tmp_path, "codex")
    host._spawned_marker, host._owner_seat = False, True
    host._adopt_convo_settings(born=True)
    raw = _cs.path_for(tmp_path).read_text(encoding="utf-8")
    assert "owner" not in raw.lower()
    fleet = _resumed(tmp_path, monkeypatch, False)
    fleet._owner_seat = False            # a fleet process, not Ryan's launcher
    assert seat_authority.floor_refusal(fleet, "typed") is not None


def test_F_run_bat_marks_RYANS_launch_and_scopes_it():
    """Ryan named run.bat as his launcher (Marquee 7048cfff). setlocal keeps the mark
    from outliving the script in a console that runs it (a later `litetui`, the
    same shim fleet agents type, would read as his). It is set after `uv sync` and
    immediately before `uv run`, and the file stays CRLF (cmd.exe)."""
    raw = (Path(__file__).resolve().parents[1] / "run.bat").read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b""), "run.bat lost its CRLF endings"
    lines = raw.decode("ascii").split("\r\n")
    assert lines[0] == "@echo off" and lines[1] == "setlocal"
    # Dijkstra's guard: never from inside Claude Code's Bash or a LiteTUI tool shell.
    mark = lines.index('if not defined CLAUDECODE if not defined LITETUI_AGENT_SHELL '
                       'set "LITETUI_OWNER=1"')
    assert lines[mark + 1].startswith("uv run "), "the mark must sit right before uv run"
    assert any(line.startswith("uv sync") for line in lines[:mark]), "the mark reached uv sync"



# ── consumption side (Marquee 1b8a423e / 68aec657, Sentinel 3d610d15) ─────────
#    The mark is VOID inside an agent's shell, and inside a LiteSuite terminal it
#    also needs that terminal untainted by the bridge.

# Pinned, not read from AGENT_SHELL_MARKERS: cases derived from the code under test
# cannot catch a marker dropped from it.
@pytest.mark.parametrize(
    "marker", ["CLAUDECODE", "LITETUI_AGENT_SHELL", "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_SANDBOX"]
)
def test_VOID_the_owner_mark_inside_an_agents_shell(marker):
    env = {"LITETUI_OWNER": "1", marker: "1"}
    assert seat_authority.owner_mark_valid(env) is False
    assert seat_authority.owner_mark_valid({"LITETUI_OWNER": "1"}) is True, "CONTROL"


def test_VOID_claude_in_a_UI_panel_launching_litetui_is_ENFORCED(monkeypatch):
    """A UI-made panel carries LITETUI_OWNER to Claude Code started in it; its
    Bash (CLAUDECODE=1) launching litetui must not inherit Ryan's exemption."""
    monkeypatch.setenv("LITETUI_OWNER", "1")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert LiteTUI()._owner_seat is False


def test_VOID_a_litetui_tool_shell_launching_litetui_is_ENFORCED(monkeypatch):
    from litetui import harness
    monkeypatch.setenv("LITETUI_OWNER", "1")
    LiteTUI()                                   # Ryan's own: exports the marker
    assert os.environ.get(harness.AGENT_SHELL_MARKER) == "1"
    monkeypatch.setenv("LITETUI_OWNER", "1")    # a tool shell of it, relaunching
    assert LiteTUI()._owner_seat is False


def test_TAINT_is_checked_only_inside_a_LiteSuite_terminal(monkeypatch):
    monkeypatch.setenv("LITETUI_OWNER", "1")
    assert LiteTUI()._pty_term is None, "outside a LiteSuite pty nothing is asked"
    monkeypatch.setenv("LITETUI_OWNER", "1")
    monkeypatch.setenv("LITESUITE_PTY_TERM", "pty-1-1")
    monkeypatch.delenv("LITETUI_AGENT_SHELL", raising=False)
    assert LiteTUI()._pty_term == "pty-1-1"


def test_TAINT_a_bridge_typed_UI_shell_LOSES_the_mark_for_good(monkeypatch):
    """/pty/talk typed `litetui` into a shell Ryan opened: the bridge recorded the
    taint BEFORE writing, so the owner mark is gone, and stays gone."""
    answers = [False]
    monkeypatch.setattr(seat_authority, "pty_taint_clean", lambda term, timeout=1.0: answers[0])
    seat = _ryans()
    seat._pty_term = "pty-9-9"
    _typed(seat)
    assert seat.streams == 0 and "TURN REFUSED" in seat.said[-1]
    answers[0] = True
    assert seat_authority.is_owner(seat) is False, "a taint must be permanent"


def test_TAINT_CONTROL_an_untouched_UI_shell_keeps_the_mark(monkeypatch):
    monkeypatch.setattr(seat_authority, "pty_taint_clean", lambda term, timeout=1.0: True)
    seat = _ryans()
    seat._pty_term = "pty-9-9"
    _typed(seat)
    assert seat.streams == 1 and _no_floor_text(seat), seat.said


class _Answer:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


@pytest.mark.parametrize("body,expected", [
    (b'{"tainted": false}', True),
    (b'{"tainted": true}', False),
    (b'{}', False),
    (b'not json', False),
])
def test_TAINT_only_a_clear_untainted_answer_keeps_the_mark(monkeypatch, body, expected):
    import urllib.request
    seen = {}

    def urlopen(request, timeout):
        seen["url"], seen["auth"], seen["timeout"] = (request.full_url,
                                                     request.get_header("Authorization"), timeout)
        return _Answer(body)

    monkeypatch.setenv("LITESUITE_BRIDGE_TOKEN", "tok")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert seat_authority.pty_taint_clean("pty-1-1") is expected
    assert seen["url"].endswith("/pty/owner-ok?term=pty-1-1")
    assert seen["auth"] == "Bearer tok" and seen["timeout"] <= 1.0


def test_TAINT_no_answer_or_no_token_is_NOT_the_owner(monkeypatch, tmp_path):
    import urllib.request

    def refused(request, timeout):
        raise OSError("connection refused")

    monkeypatch.setenv("LITESUITE_BRIDGE_TOKEN", "tok")
    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert seat_authority.pty_taint_clean("pty-1-1") is False
    monkeypatch.delenv("LITESUITE_BRIDGE_TOKEN")
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", staticmethod(lambda: tmp_path))   # no token file
    assert seat_authority.pty_taint_clean("pty-1-1") is False
