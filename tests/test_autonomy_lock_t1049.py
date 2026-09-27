"""T1049-A — the autonomy lock: a LiteTUI that is not Ryan's own never runs autonomous.

Ryan (liteask a-29047520): "light TUI instances that are spawned by other agents
cannot be set to auto mode. They can only go to interactive mode and must be
handled by their leaders". Marquee f4d49382 approved the design: the gate is
`app._active_tool_profile` itself (9 writers, ~12 readers, only 3 writers through
the resolver), autonomous reads as interactive, explicit asks are refused in words
and nothing is persisted, shift+tab skips autonomous, the implicit cap says one line.

"Not Ryan's own" is seat_authority.is_ryans_own, the ONE definition (T1043):
owner-marked AND not spawned. conftest clears the owner mark, so an app built here
is locked unless an arm marks it with _ryans().
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from litetui import app as m
from litetui import gui_rpc, rpc, seat_authority, tool_policy
from litetui.tool_policy import AUTONOMOUS, INTERACTIVE, STRICT


def _app(profile=AUTONOMOUS, **kw):
    a = m.LiteTUI(**kw)
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.settings.tool_policy_profile = profile
    a._active_tool_profile = profile
    return a


def _ryans(a):
    """Ryan's own instance: owner-marked by his launcher, not spawned."""
    a._spawned_seat = False
    a._owner_seat = True
    a._pty_term = None
    return a


def _said(a):
    """Every _system line, recorded without needing a mounted chat log."""
    lines: list[str] = []
    a._system = lines.append
    return lines


# ── the gate: every writer, one field ───────────────────────────────────────

def test_LOCK_a_direct_write_of_autonomous_READS_interactive():
    """The direct writers (settings_runtime.py:190, gui_rpc.py:694, the resume in
    _adopt_convo_settings, __init__) are plain assignments to this field, so the
    property is what each of them meets."""
    a = _app()
    a._active_tool_profile = AUTONOMOUS
    assert a._active_tool_profile == INTERACTIVE
    assert a.__dict__["_active_tool_profile_raw"] == AUTONOMOUS, "the raw write is kept, only the READ is capped"


def test_LOCK_init_from_the_autonomous_settings_default():
    """settings.py's default authority is autonomous (T084); __init__ stamps it."""
    a = m.LiteTUI()
    assert a.__dict__["_active_tool_profile_raw"] == a.chosen_tool_profile
    a.settings.tool_policy_profile = AUTONOMOUS
    a._active_tool_profile = a.chosen_tool_profile
    assert a._active_tool_profile == INTERACTIVE


def test_LOCK_the_launch_flag_and_the_rpc_default_are_capped():
    """--tool-profile autonomous, and cli.py:130's rpc default (left as is)."""
    a = m.LiteTUI(tool_profile=AUTONOMOUS)
    assert a._active_tool_profile == INTERACTIVE
    assert seat_authority.seat_profile(a) == INTERACTIVE


@pytest.mark.parametrize("source", [
    "typed", "rpc", "queued",            # hook_host.accept_prompt (hook_host.py:179)
    "harness",                           # inbox mail
    "child-result",                      # agent_parent_wake.py:38
    "goal", "goal-ryan",                 # goal_loop.py:344
])   # "scheduled" (a cron/loop fire) is C4's own arm: test_C4_a_locked_seats_schedule_runs_interactive
def test_LOCK_the_resolver_never_answers_autonomous(source):
    a = _app()
    assert seat_authority.turn_profile(a, source, AUTONOMOUS) == INTERACTIVE
    assert seat_authority.resolve(a, source, AUTONOMOUS).profile == INTERACTIVE


def test_LOCK_seat_profile_is_capped_for_the_gui_tools_path():
    """gui_rpc.py:689 stamps seat_profile for a GUI-run tool."""
    a = _app()
    assert seat_authority.seat_profile(a) == INTERACTIVE


def test_LOCK_only_autonomous_is_touched():
    a = _app()
    for level in (STRICT, INTERACTIVE):
        a._active_tool_profile = level
        assert a._active_tool_profile == level
        assert seat_authority.turn_profile(a, "typed", level) == level


def test_LOCK_unknown_ownership_is_locked():
    """No markers recorded at all (a partial host): the floor, never autonomous."""
    host = SimpleNamespace(_cli_tool_profile=None, settings=SimpleNamespace(tool_policy_profile=AUTONOMOUS),
                           chosen_tool_profile=AUTONOMOUS)
    assert seat_authority.locked(host) is True
    assert seat_authority.seat_profile(host) == INTERACTIVE


# ── CONTROL: Ryan's own instance ────────────────────────────────────────────

def test_CONTROL_ryans_own_instance_still_runs_autonomous():
    a = _ryans(_app())
    assert a._active_tool_profile == AUTONOMOUS
    assert seat_authority.seat_profile(a) == AUTONOMOUS
    assert seat_authority.turn_profile(a, "typed", AUTONOMOUS) == AUTONOMOUS
    assert seat_authority.turn_profile(a, "scheduled") == AUTONOMOUS


def test_CONTROL_a_spawned_seat_is_locked_even_when_owner_marked():
    a = _ryans(_app())
    a._spawned_seat = True
    assert a._active_tool_profile == INTERACTIVE


def test_the_getter_never_asks_the_bridge(monkeypatch):
    """The getter runs on every footer paint: recheck=False, no HTTP."""
    a = _ryans(_app())
    a._pty_term = "pty-1-1"
    monkeypatch.setattr(seat_authority, "pty_taint_clean",
                        lambda *_a, **_k: pytest.fail("the getter asked the bridge"))
    assert a._active_tool_profile == AUTONOMOUS


def test_a_taint_seen_at_turn_start_locks_the_getter(monkeypatch):
    a = _ryans(_app())
    a._pty_term = "pty-1-1"
    monkeypatch.setattr(seat_authority, "pty_taint_clean", lambda *_a, **_k: False)
    assert seat_authority.is_ryans_own(a) is False   # the per-turn re-ask (recheck=True)
    assert a._active_tool_profile == INTERACTIVE


# ── explicit asks: refused in words, nothing persisted ──────────────────────

def _no_persist(monkeypatch, a):
    calls: list = []
    monkeypatch.setattr(m.settings_runtime, "persist_or_raise", lambda *args: calls.append(args))
    remembered: list = []
    a._remember_for_this_convo = lambda *args: remembered.append(args)
    a._refresh_ctx_label = lambda: None
    return calls, remembered


def test_REFUSED_set_tool_profile_autonomous(monkeypatch):
    a = _app(INTERACTIVE)
    said = _said(a)
    calls, remembered = _no_persist(monkeypatch, a)
    a._cli_tool_profile = STRICT
    assert a.set_tool_profile(AUTONOMOUS, source="wire") is False
    assert said == [seat_authority.lock_refusal(a) + " Authority stays interactive."]
    assert "they can only go to interactive mode" in said[0]
    assert a.settings.tool_policy_profile == INTERACTIVE
    assert calls == [] and remembered == [], "a refusal persisted something"
    assert a._cli_tool_profile == STRICT, "a refusal retired the launch flag"


def test_CONTROL_set_tool_profile_interactive_in_a_locked_seat_is_accepted(monkeypatch):
    a = _app(STRICT)
    _said(a)
    calls, remembered = _no_persist(monkeypatch, a)
    assert a.set_tool_profile(INTERACTIVE, source="wire") is True
    assert a._active_tool_profile == INTERACTIVE and calls and remembered


def test_CONTROL_ryans_own_set_tool_profile_autonomous_is_accepted(monkeypatch):
    a = _ryans(_app(INTERACTIVE))
    _said(a)
    calls, _ = _no_persist(monkeypatch, a)
    assert a.set_tool_profile(AUTONOMOUS, source="wire") is True
    assert a._active_tool_profile == AUTONOMOUS and calls


def test_REFUSED_over_rpc_names_the_lock_not_an_unknown_profile():
    a = _app()
    assert rpc._profile_error(a, AUTONOMOUS) == seat_authority.lock_refusal(a)
    assert rpc._profile_error(a, "wide-open") == "unknown tool profile 'wide-open'"


def test_REFUSED_a_settings_save_of_autonomous_before_it_persists(monkeypatch):
    a = _app(INTERACTIVE)
    said = _said(a)
    from dataclasses import replace
    new = replace(a.settings, tool_policy_profile=AUTONOMOUS)
    # Stop at the first step after the refusal: what matters is that the object
    # handed on to persistence no longer says autonomous.
    a._settings_persist_error = "unset"

    class _Stop(Exception):
        pass

    def stop():
        assert a._settings_persist_error is None   # the line right after the refusal ran
        raise _Stop

    a._refresh_prompt_controls = stop
    with pytest.raises(_Stop):
        a._on_settings_saved(new)
    assert said == [seat_authority.lock_refusal(a) + " Authority stays interactive; the other settings are saved as usual."]
    assert new.tool_policy_profile == INTERACTIVE, "autonomous would have been persisted"


# ── shift+tab ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("start, expected", [
    (INTERACTIVE, STRICT),       # one step down
    (STRICT, INTERACTIVE),       # would land on autonomous: skipped
    (AUTONOMOUS, INTERACTIVE),   # a settings default of autonomous: off it, never back
])
def test_shift_tab_in_a_locked_seat_never_lands_on_autonomous(monkeypatch, start, expected):
    a = _app(start)
    said = _said(a)
    _no_persist(monkeypatch, a)
    a.action_cycle_tool_profile()
    assert a.settings.tool_policy_profile == expected
    assert not any(line.startswith("autonomous refused") for line in said), "the cycle hit the refusal"


def test_CONTROL_shift_tab_in_ryans_own_reaches_autonomous(monkeypatch):
    a = _ryans(_app(STRICT))
    _said(a)
    _no_persist(monkeypatch, a)
    a.action_cycle_tool_profile()
    assert a.settings.tool_policy_profile == AUTONOMOUS


# ── the implicit cap: one line at connect ───────────────────────────────────

def test_the_implicit_cap_is_said_once():
    a = _app()
    said = _said(a)
    seat_authority.warn_if_capped(a)
    seat_authority.warn_if_capped(a)
    assert said == ["⚠ " + seat_authority.lock_refusal(a) + " Authority is interactive."]


def test_CONTROL_nothing_is_said_when_nothing_asked_for_autonomous():
    a = _app(INTERACTIVE)
    said = _said(a)
    seat_authority.warn_if_capped(a)
    assert said == []


def test_CONTROL_nothing_is_said_in_ryans_own():
    a = _ryans(_app())
    said = _said(a)
    seat_authority.warn_if_capped(a)
    assert said == []


# ── F2: the refusal is true to the seat (Dijkstra e1f6a89c) ─────────────────

def test_REFUSAL_text_a_spawned_seat_is_told_an_agent_spawned_it():
    a = _app()
    a._spawned_seat = True
    text = seat_authority.lock_refusal(a)
    assert "an agent spawned it" in text and "its leader" in text
    assert "they can only go to interactive mode" in text


def test_REFUSAL_text_an_unmarked_seat_ryan_launched_is_NOT_told_an_agent_spawned_it():
    """A plain `python -m litetui`: not spawned, not owner-marked, so locked."""
    a = _app()
    a._spawned_seat, a._owner_seat = False, False
    assert seat_authority.locked(a)
    text = seat_authority.lock_refusal(a)
    assert "spawned" not in text
    assert "not owner-marked" in text and "run.bat or LiteGUI" in text


# ── F1: no raw read of authority around the gate (Dijkstra e1f6a89c) ────────
# Each arm runs in a LOCKED host whose settings hold autonomous (settings.py's
# default), so chosen_tool_profile, the raw value the three sites read, is autonomous.

def _gui_state_profile(monkeypatch, a):
    monkeypatch.setattr(gui_rpc, "_settings", lambda *_a: {})
    monkeypatch.setattr(gui_rpc, "_conversations", lambda *_a: [])
    monkeypatch.setattr(gui_rpc, "_jobs", lambda *_a: [])
    monkeypatch.setattr(gui_rpc, "active_work", lambda *_a: [])
    a._rpc_model_state = lambda: None
    a._chat_running = lambda: False
    return gui_rpc.dispatch(a, {"type": "gui.state"})["tool_profile"]


def _rpc_prompt(monkeypatch, a, profile):
    """LiteGUI's Send: request('prompt', {message, tool_profile}) (App.tsx:144)."""
    replies: list = []
    submitted: list = []
    monkeypatch.setattr(rpc, "_respond", lambda *args, **kw: replies.append(kw))
    monkeypatch.setattr(a.store, "acquire", lambda *_a: None)
    a._submit_text = lambda text, **kw: submitted.append((text, kw.get("source")))
    rpc._dispatch(a, {"type": "prompt", "id": "p", "message": "hi", "tool_profile": profile})
    return replies, submitted


class _Decided(Exception):
    pass


async def _hook_test_decision(monkeypatch, a, tmp_path):
    """What the hook Test buttons do (gui_rpc gui.hooks.test, hooks_screen Test):
    hook_host.invoke(app, hook, document, app.chosen_tool_profile) reaches the
    door as profile=<raw>. Stopped right after the policy decision."""
    real = tool_policy.evaluate
    seen: list = []

    def spy(profile, policy, args, workspace, **kw):
        seen.append(real(profile, policy, args, workspace, **kw))
        raise _Decided

    monkeypatch.setattr(tool_policy, "evaluate", spy)
    with pytest.raises(_Decided):
        await a._authorize_action("hook:fixture", {"command": "rm -rf ./build"}, tool_policy.SHELL_POLICY,
                                  profile=a.chosen_tool_profile, workspace=tmp_path,
                                  allow_prompt=False, stop_on_denial=False)
    return seen[0]


def test_F1_gui_state_reports_interactive_in_a_locked_seat(monkeypatch):
    a = _app()
    assert a.chosen_tool_profile == AUTONOMOUS, "the arm needs the raw value to be autonomous"
    assert _gui_state_profile(monkeypatch, a) == INTERACTIVE


def test_F1_an_rpc_prompt_carrying_gui_states_profile_starts_a_turn(monkeypatch):
    a = _app()
    _said(a)
    _no_persist(monkeypatch, a)
    replies, submitted = _rpc_prompt(monkeypatch, a, _gui_state_profile(monkeypatch, a))
    assert replies == [{"ok": True, "result": {"turn": "accepted"}}], replies
    assert submitted == [("hi", "rpc")]


@pytest.mark.asyncio
async def test_F1_a_hook_test_authorizes_under_interactive_in_a_locked_seat(monkeypatch, tmp_path):
    a = _app()
    assert a.chosen_tool_profile == AUTONOMOUS
    decision = await _hook_test_decision(monkeypatch, a, tmp_path)
    assert decision.profile == INTERACTIVE
    assert decision.action == tool_policy.CONFIRM, "a destructive argv ran without asking"


def test_CONTROL_F1_ryans_own_gui_state_and_rpc_prompt_stay_autonomous(monkeypatch):
    a = _ryans(_app())
    _said(a)
    _no_persist(monkeypatch, a)
    profile = _gui_state_profile(monkeypatch, a)
    assert profile == AUTONOMOUS
    replies, submitted = _rpc_prompt(monkeypatch, a, profile)
    assert replies == [{"ok": True, "result": {"turn": "accepted"}}] and submitted == [("hi", "rpc")]
    assert a._active_tool_profile == AUTONOMOUS


@pytest.mark.asyncio
async def test_CONTROL_F1_ryans_own_hook_test_authorizes_under_autonomous(monkeypatch, tmp_path):
    a = _ryans(_app())
    decision = await _hook_test_decision(monkeypatch, a, tmp_path)
    assert decision.profile == AUTONOMOUS and decision.action == tool_policy.ALLOW


# ── F3: the child-delegation invariant, which no arm held (Dijkstra e1f6a89c) ─

def test_a_child_that_ASKS_for_autonomous_under_an_autonomous_parent_is_blocked():
    """test_invalid_or_authority_increasing_request_is_rejected meant to hold this,
    but its parent_profile='scheduled' is no profile, so every case dies at
    "Unknown parent policy" (pre-existing; Marquee aa29fbe8 lists it separately)."""
    from litetui.agent_launcher import LaunchBlocked, validate_request
    request = {"prompt": "p", "backend": "codex", "model": "m", "workspace": "C:/fixture"}
    with pytest.raises(LaunchBlocked, match="delegation"):
        validate_request({**request, "tool_profile": AUTONOMOUS}, parent_profile=AUTONOMOUS, depth=0)
    # CONTROL: the same parent, no explicit ask: the child is delegated interactive.
    assert validate_request(request, parent_profile=AUTONOMOUS, depth=0).tool_profile == INTERACTIVE


# ── C4: a locked seat's cron/loop fire, at ONE site ─────────────────────────

def _stamp(a, source, requested=None):
    """hook_host.accept_prompt's two lines: the stamp, then the turn's source."""
    a._active_tool_profile = seat_authority.turn_profile(a, source, requested)
    a._active_turn_source = source


def test_C4_a_locked_seats_schedule_runs_interactive():
    """PINS TODAY'S ANSWER at seat_authority.LOCKED_SCHEDULE_PROFILE. Ryan ruled
    2026-09-27 (per Sentinel 04169351) that the spawning agent babysits the seat:
    B routes its CONFIRMs to that parent. Changing the profile is that line plus
    this test."""
    a = _app()
    assert seat_authority.turn_profile(a, "scheduled") == INTERACTIVE
    assert seat_authority.resolve(a, "scheduled", AUTONOMOUS).profile == INTERACTIVE
    _stamp(a, "scheduled")
    assert a._active_tool_profile == INTERACTIVE


def test_C4_the_site_is_the_only_line_a_change_needs(monkeypatch):
    """The C4 constant alone moves a scheduled turn, through the stamp and the
    property, and nothing else: an attended stamp stays capped, and any later
    write of the field drops the scheduled answer."""
    monkeypatch.setattr(seat_authority, "LOCKED_SCHEDULE_PROFILE", AUTONOMOUS)
    a = _app()
    _stamp(a, "scheduled")
    assert a._active_tool_profile == AUTONOMOUS
    _stamp(a, "typed", AUTONOMOUS)
    assert a._active_tool_profile == INTERACTIVE
    _stamp(a, "scheduled")
    a._active_tool_profile = AUTONOMOUS          # any other writer (goal, parent wake, settings)
    assert a._active_tool_profile == INTERACTIVE
    assert seat_authority.locked_profile(a, AUTONOMOUS) == INTERACTIVE   # no source: capped
