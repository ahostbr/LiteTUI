"""Tests for litetui.plugin_reload_state — session-state transfer + idle gate.

Hermetic: real PluginRegistry, no App. Run only this file:
    python -m pytest tests/test_plugin_reload_state.py
"""
from __future__ import annotations

import pytest

from litetui.plugins import PluginRegistry
import litetui.plugin_reload_state as st


def _spec(name: str) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": name,
        "parameters": {"type": "object", "properties": {}, "required": []}}}


# ── session-state transfer ───────────────────────────────────────────────────
def test_activated_copied_not_aliased():
    live = PluginRegistry()
    live.activated = {"a", "b"}
    cand = PluginRegistry()
    st.transfer_session_state(live, cand)
    assert cand.activated == {"a", "b"}
    assert cand.activated is not live.activated       # new object, not aliased
    cand.activated.add("c")
    assert "c" not in live.activated                   # mutating candidate never touches live


def test_deferred_config_transferred():
    live = PluginRegistry()
    live.deferred_static = frozenset({"studio", "listen"})
    live.defer_dynamic = True
    cand = PluginRegistry()
    st.transfer_session_state(live, cand)
    assert cand.deferred_static == frozenset({"studio", "listen"})
    assert cand.defer_dynamic is True


def test_tools_disabled_provider_stays_live():
    # The provider callable is SHARED, not snapshotted: a later change to the
    # user's denylist must be visible through the candidate too.
    disabled: set[str] = set()
    live = PluginRegistry()
    live.tools_disabled = lambda: frozenset(disabled)
    cand = PluginRegistry()
    st.transfer_session_state(live, cand)
    assert cand.tools_disabled is live.tools_disabled
    disabled.add("web_fetch")
    assert cand.tools_disabled() == frozenset({"web_fetch"})


def test_tables_and_status_not_copied():
    live = PluginRegistry()
    live.add_tool("p1", _spec("t1"), run=lambda a: "")
    live.status["p1"] = "active"
    cand = PluginRegistry()
    st.transfer_session_state(live, cand)
    assert cand.tools == []          # capability tables are host/plugin owned; not copied
    assert cand.status == {}         # status not copied blindly (INV-S4 unresolved)


def test_sibling_independence():
    # Two independent generations (App A live, App B live). A candidate built
    # from A must never expose a path to B, nor mutate A.
    live_a = PluginRegistry(); live_a.activated = {"a1"}
    live_b = PluginRegistry(); live_b.activated = {"b1"}
    cand = PluginRegistry()
    st.transfer_session_state(live_a, cand)
    cand.activated.add("x")
    assert live_a.activated == {"a1"}
    assert live_b.activated == {"b1"}


def test_transfer_rejects_live_is_candidate():
    reg = PluginRegistry()
    with pytest.raises(ValueError):
        st.transfer_session_state(reg, reg)


# ── idle-commit gate ─────────────────────────────────────────────────────────
def test_all_clear_is_committable():
    a = st.ActivitySnapshot()
    assert st.blocking_reasons(a) == ()
    assert st.can_commit_candidate(a) is True


def test_each_flag_defers():
    for flag in ("turn_active", "tool_active", "management_active",
                 "children_active", "mcp_active"):
        a = st.ActivitySnapshot(**{flag: True})
        assert st.blocking_reasons(a), f"{flag} did not defer"
        assert st.can_commit_candidate(a) is False


def test_multiple_flags_report_all_reasons():
    a = st.ActivitySnapshot(turn_active=True, mcp_active=True)
    assert len(st.blocking_reasons(a)) == 2


def test_activity_snapshot_rejects_non_bool():
    # Fail closed: None / strings / ints must not slip through as falsy.
    for bad in (None, "yes", 1, 0):
        with pytest.raises(TypeError):
            st.ActivitySnapshot(turn_active=bad)
