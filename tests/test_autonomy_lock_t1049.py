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
from litetui import rpc, seat_authority, tool_policy
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
    "scheduled",                         # a cron/loop fire (the T085 autonomous default)
])
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
    assert said == [seat_authority.LOCK_REFUSAL + " Authority stays interactive."]
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
    assert rpc._profile_error(AUTONOMOUS) == seat_authority.LOCK_REFUSAL
    assert rpc._profile_error("wide-open") == "unknown tool profile 'wide-open'"


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
    assert said == [seat_authority.LOCK_REFUSAL + " Authority stays interactive; the other settings are saved as usual."]
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
    assert said == ["⚠ " + seat_authority.LOCK_REFUSAL + " Authority is interactive."]


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
