"""T1133: autonomy is a user's selectable level, not a seat-ownership lock."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from litetui import app as m
from litetui import rpc, seat_authority, tool_policy
from litetui.tool_policy import AUTONOMOUS, INTERACTIVE, STRICT


def _app(profile=STRICT, **kw):
    a = m.LiteTUI(**kw)
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a.settings.tool_policy_profile = profile
    a._active_tool_profile = profile
    a._system = lambda *_: None
    a._refresh_ctx_label = lambda: None
    return a


@pytest.mark.parametrize("seat", ["spawned", "resumed", "owner", "unmarked"])
def test_ryan_can_always_manually_set_autonomous(monkeypatch, seat):
    """please just make that one of the fixes is that I can always manually change to auto mode if I so choose to in Lite UI."""
    a = _app(STRICT, tool_profile=INTERACTIVE)
    a._spawned_seat = seat == "spawned"
    a._owner_seat = seat == "owner"
    if seat == "resumed":
        a._convo_settings = SimpleNamespace(tool_policy_profile=STRICT)
        a.convo_dir = "resume-test"
        monkeypatch.setattr(m.convo_settings_mod, "save", lambda *_: None)
    monkeypatch.setattr(m.settings_runtime, "persist_or_raise", lambda *_: None)

    # Human cycle key and the footer toggle both use the same setter.
    a.action_cycle_tool_profile()
    assert a.settings.tool_policy_profile == a._active_tool_profile == AUTONOMOUS
    assert a._cli_tool_profile is None
    assert a.set_tool_profile(STRICT, source="footer") is True
    assert a.set_tool_profile(AUTONOMOUS, source="footer") is True
    assert a._active_tool_profile == AUTONOMOUS
    assert seat_authority.turn_profile(a, "typed", AUTONOMOUS) == AUTONOMOUS
    assert seat_authority.turn_profile(a, "harness", AUTONOMOUS) == AUTONOMOUS
    assert seat_authority.seat_profile(a) == AUTONOMOUS
    # Resuming or applying a settings reload with the selected level is not a cap.
    a._active_tool_profile = a.chosen_tool_profile
    assert a._active_tool_profile == AUTONOMOUS
    a.settings.tool_policy_profile = AUTONOMOUS
    assert seat_authority.resolve(a, "typed", a.settings.tool_policy_profile).profile == AUTONOMOUS


def test_launch_flag_stays_ceiling_until_explicit_human_choice(monkeypatch):
    a = _app(STRICT, tool_profile=INTERACTIVE)
    a.settings.tool_policy_profile = AUTONOMOUS  # shared file is not an explicit seat act
    assert seat_authority.seat_profile(a) == INTERACTIVE
    assert seat_authority.turn_profile(a, "harness", AUTONOMOUS) == INTERACTIVE
    assert seat_authority.turn_profile(a, "goal", AUTONOMOUS) == INTERACTIVE
    assert seat_authority.withheld(a, AUTONOMOUS) is not None
    monkeypatch.setattr(m.settings_runtime, "persist_or_raise", lambda *_: None)
    assert a.set_tool_profile(AUTONOMOUS, source="footer")
    assert a._cli_tool_profile is None  # explicit human choice retires the launch flag
    assert seat_authority.turn_profile(a, "harness", AUTONOMOUS) == AUTONOMOUS


def test_spawned_seat_rpc_and_schedule_accept_autonomous(monkeypatch):
    a = _app(INTERACTIVE)
    a._spawned_seat = True
    monkeypatch.setattr(m.settings_runtime, "persist_or_raise", lambda *_: None)
    responses = []
    monkeypatch.setattr(rpc, "_respond", lambda *args, **kw: responses.append(kw))
    rpc._dispatch(a, {"type": "set", "id": "p", "profile": AUTONOMOUS})
    assert responses[-1]["ok"] is True
    assert a._active_tool_profile == AUTONOMOUS
    assert seat_authority.schedule_level(a, AUTONOMOUS) == AUTONOMOUS
    assert seat_authority.withheld(a, AUTONOMOUS) is None


def test_confirm_route_still_uses_ownership_not_profile():
    seat = SimpleNamespace(_spawned_seat=True, _owner_seat=False, _spawner_id="leader", _rpc=False)
    assert seat_authority.confirm_route(seat) == "spawner"
    seat._rpc = seat._approval_host = True
    assert seat_authority.confirm_route(seat) == "host"
    assert tool_policy.cycle(STRICT) == AUTONOMOUS
