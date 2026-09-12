"""T558-B — authority and plan mode change MID-SESSION, over the wire.

FOUND BY MEASUREMENT, not design: the LiteSuite adapter had been sending
`tool_profile` on every prompt command since it was written, and the rpc prompt
handler read only `message`. Nothing had ever consumed it. So authority and mode
were whatever the SPAWN FLAGS said, and flipping Full access mid-session did
nothing until the session restarted — silently, because the pill and the footer
both showed the new value while the tools kept running under the old one.

🔴 THE POINT OF THESE ARMS IS THAT THE WIRE AND THE KEYBOARD SHARE ONE PATH.
`set_tool_profile` / `set_plan_mode` are what shift+tab and Ctrl+P call. A second
implementation for the wire is the failure this design exists to prevent, and its
symptom would be the wire moving `settings.tool_policy_profile` while leaving
`_active_tool_profile` — the field `_execute_tool` actually reads — untouched.
The footer would show the new authority and the tools would keep the old one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as m  # noqa: E402
from litetui import rpc as rpc_mod  # noqa: E402
from litetui import tool_policy  # noqa: E402


def make_app(**kw):
    a = m.LiteTUI(**kw)
    a.convo_dir = None
    a.skills = []
    a._system = lambda *args, **kwargs: None
    a._update_header = lambda: None
    a._refresh_ctx_label = lambda: None
    a._edit = lambda *args, **kwargs: None
    return a


def dispatch(app, cmd) -> list[dict]:
    """Run one rpc command, capturing what it answers."""
    out: list[dict] = []
    real = rpc_mod.rpc_emit
    rpc_mod.rpc_emit = out.append  # type: ignore[assignment]
    try:
        rpc_mod._dispatch(app, cmd)
    finally:
        rpc_mod.rpc_emit = real  # type: ignore[assignment]
    return out


# ── authority ──────────────────────────────────────────────────────────────

def test_set_moves_the_field_the_tools_actually_read():
    a = make_app()
    a._active_tool_profile = tool_policy.AUTONOMOUS
    a.settings.tool_policy_profile = tool_policy.AUTONOMOUS

    dispatch(a, {"type": "set", "id": "c1", "profile": tool_policy.INTERACTIVE})

    # BOTH, and the second is the load-bearing one: _execute_tool reads it, so
    # moving only the setting changes the footer and nothing else.
    assert a.settings.tool_policy_profile == tool_policy.INTERACTIVE
    assert a._active_tool_profile == tool_policy.INTERACTIVE


def test_set_acks_what_is_in_force_not_what_was_asked_for():
    a = make_app()
    a._active_tool_profile = tool_policy.AUTONOMOUS
    out = dispatch(a, {"type": "set", "id": "c1", "profile": tool_policy.SCHEDULED})
    ack = out[-1]
    assert ack["ok"] is True
    assert ack["result"]["tool_policy_profile"] == tool_policy.SCHEDULED
    assert ack["result"]["changed"] == {"profile": tool_policy.SCHEDULED}


def test_an_unknown_profile_is_refused_and_changes_nothing():
    a = make_app()
    a._active_tool_profile = tool_policy.AUTONOMOUS
    a.settings.tool_policy_profile = tool_policy.AUTONOMOUS

    out = dispatch(a, {"type": "set", "id": "c1", "profile": "godmode"})

    assert out[-1]["ok"] is False
    # Storing an unresolvable profile would leave the app with NO policy, which
    # is worse than telling the caller no.
    assert a._active_tool_profile == tool_policy.AUTONOMOUS
    assert a.settings.tool_policy_profile == tool_policy.AUTONOMOUS


# ── plan mode ──────────────────────────────────────────────────────────────

def test_set_mode_plan_puts_the_plan_section_in_the_prompt():
    a = make_app()
    before = a._system_prompt_text()

    dispatch(a, {"type": "set", "id": "c1", "mode": "plan"})

    assert a._plan_mode is True
    after = a._system_prompt_text()
    assert after != before
    assert "ls-plan-w-quizmaster" in after


def test_set_mode_normal_takes_it_back_out_byte_for_byte():
    a = make_app(plan_mode=True)
    planned = a._system_prompt_text()

    dispatch(a, {"type": "set", "id": "c1", "mode": "normal"})

    assert a._plan_mode is False
    plain = a._system_prompt_text()
    assert plain != planned
    assert "ls-plan-w-quizmaster" not in plain
    # And re-entering returns EXACTLY to the planned composition — an
    # instruction that half-leaves is the failure the section design prevents.
    dispatch(a, {"type": "set", "id": "c2", "mode": "plan"})
    assert a._system_prompt_text() == planned


def test_an_unknown_mode_is_refused():
    a = make_app()
    out = dispatch(a, {"type": "set", "id": "c1", "mode": "brainstorm"})
    assert out[-1]["ok"] is False
    assert a._plan_mode is False


def test_an_empty_set_is_a_no_op_that_still_reports_the_state():
    # A host that knows neither field must not get an error; it gets the truth.
    a = make_app()
    a._active_tool_profile = tool_policy.AUTONOMOUS
    out = dispatch(a, {"type": "set", "id": "c1"})
    assert out[-1]["ok"] is True
    assert out[-1]["result"]["changed"] == {}
    assert out[-1]["result"]["plan_mode"] is False


def test_both_at_once():
    a = make_app()
    a._active_tool_profile = tool_policy.AUTONOMOUS
    out = dispatch(a, {"type": "set", "id": "c1", "profile": tool_policy.INTERACTIVE, "mode": "plan"})
    assert out[-1]["result"]["changed"] == {"profile": tool_policy.INTERACTIVE, "mode": "plan"}
    assert a._active_tool_profile == tool_policy.INTERACTIVE
    assert a._plan_mode is True


# ── the dead field on `prompt` ─────────────────────────────────────────────

def test_prompt_now_honours_tool_profile():
    """The field the adapter has been sending all along.

    Before this it was read by nothing: the host sent it every turn and the
    authority never moved.
    """
    a = make_app()
    a._active_tool_profile = tool_policy.AUTONOMOUS
    submitted: list[str] = []
    def submit(text, alt_chord=False, *, source="typed"):
        assert source == "rpc"
        submitted.append(text)
    a._submit_text = submit

    out = dispatch(a, {"type": "prompt", "id": "p1", "message": "hello", "tool_profile": tool_policy.INTERACTIVE})

    assert out[-1]["ok"] is True
    assert submitted == ["hello"]
    assert a._active_tool_profile == tool_policy.INTERACTIVE


def test_a_prompt_with_an_unknown_profile_does_not_run_the_turn():
    # Running it under the OLD authority is the wrong kind of surprise: the
    # caller asked for a restriction and would get a turn without it.
    a = make_app()
    a._active_tool_profile = tool_policy.AUTONOMOUS
    submitted: list[str] = []
    def submit(text, alt_chord=False, *, source="typed"):
        assert source == "rpc"
        submitted.append(text)
    a._submit_text = submit

    out = dispatch(a, {"type": "prompt", "id": "p1", "message": "hello", "tool_profile": "godmode"})

    assert out[-1]["ok"] is False
    assert submitted == []
    assert a._active_tool_profile == tool_policy.AUTONOMOUS


def test_a_prompt_without_the_field_is_unchanged():
    a = make_app()
    a._active_tool_profile = tool_policy.SCHEDULED
    submitted: list[str] = []
    def submit(text, alt_chord=False, *, source="typed"):
        assert source == "rpc"
        submitted.append(text)
    a._submit_text = submit

    dispatch(a, {"type": "prompt", "id": "p1", "message": "hello"})

    assert submitted == ["hello"]
    assert a._active_tool_profile == tool_policy.SCHEDULED


# ── one path, not two ──────────────────────────────────────────────────────

def test_the_key_and_the_wire_call_the_same_setter(monkeypatch):
    """shift+tab and `set` must not grow separate implementations.

    Pinned by BEHAVIOUR rather than by inspecting the source: whatever the key
    does to the two fields, the wire does too.
    """
    a = make_app()
    a.settings.tool_policy_profile = tool_policy.AUTONOMOUS
    a._active_tool_profile = tool_policy.AUTONOMOUS
    monkeypatch.setattr(m.side_panel, "handle_reverse_tab", lambda _app: False)

    a.action_cycle_tool_profile()
    by_key = (a.settings.tool_policy_profile, a._active_tool_profile)

    b = make_app()
    b.settings.tool_policy_profile = tool_policy.AUTONOMOUS
    b._active_tool_profile = tool_policy.AUTONOMOUS
    dispatch(b, {"type": "set", "id": "c1", "profile": by_key[0]})

    assert (b.settings.tool_policy_profile, b._active_tool_profile) == by_key
