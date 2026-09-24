"""Negative-first tests for litetui.plugin_reload.stage_candidate.

Hermetic: tiny in-test fake manifests and a SimpleNamespace app, so nothing
imports a real App or the shipped plugins. Uses the real PluginRegistry /
PluginManifest / PluginContext / ToolPolicy.

Run only this file:
    python -m pytest tests/test_plugin_reload.py
"""
from __future__ import annotations

from types import SimpleNamespace

from litetui.plugins import PluginManifest, PluginRegistry
from litetui.tool_policy import (
    NETWORK, READ_ONLY, WORKSPACE_WRITE, MCP_UNKNOWN_POLICY, ToolPolicy,
)
import litetui.plugin_reload as pr


# ── helpers ───────────────────────────────────────────────────────────────
def _spec(name: str, params: dict | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": params or {"type": "object", "properties": {}, "required": []},
        },
    }


def _pol(*caps: str, confirm_always: bool = False) -> ToolPolicy:
    return ToolPolicy(capabilities=frozenset(caps), summary="t", confirm_always=confirm_always)


def _tool_manifest(owner: str, tools: list[tuple[dict, ToolPolicy | None]]) -> PluginManifest:
    def _register(ctx):
        for spec, policy in tools:
            if policy is None:
                ctx.tool(spec, run=lambda a: "")          # -> default MCP_UNKNOWN_POLICY
            else:
                ctx.tool(spec, run=lambda a: "", policy=policy)
    return PluginManifest(id=owner, register=_register)


def _app() -> SimpleNamespace:
    return SimpleNamespace(plugins=PluginRegistry())


def _kinds(result) -> set[str]:
    return {i.kind for i in result.issues}


def _live_with(owner: str, spec: dict, policy: ToolPolicy) -> PluginRegistry:
    live = PluginRegistry()
    live.add_tool(owner, spec, run=lambda a: "LIVE", policy=policy)
    return live


# ── happy path ─────────────────────────────────────────────────────────────
def test_clean_reload_of_existing_tool_ok():
    spec = _spec("t1")
    pol = _pol(READ_ONLY)
    live = _live_with("p1", spec, pol)
    m = _tool_manifest("p1", [(spec, pol)])
    res = pr.stage_candidate(_app(), [m], live=live,
                             reviewed_owners=frozenset({"p1"}))
    assert res.ok is True
    assert res.issues == ()
    assert isinstance(res.registry, PluginRegistry)
    assert [e.name for e in res.registry.tools] == ["t1"]


def test_new_tool_allowed_only_when_approved():
    live = PluginRegistry()
    m = _tool_manifest("p1", [(_spec("t2"), _pol(READ_ONLY))])
    rejected = pr.stage_candidate(_app(), [m], live=live,
                                  reviewed_owners=frozenset({"p1"}))
    assert rejected.ok is False
    assert pr.AUTHORITY_ADDED in _kinds(rejected)
    assert rejected.registry is None

    approved = pr.stage_candidate(_app(), [m], live=live,
                                  reviewed_owners=frozenset({"p1"}),
                                  approved_new_tools=frozenset({"t2"}))
    assert approved.ok is True
    assert approved.registry is not None


# ── structural rejections ───────────────────────────────────────────────────
def test_duplicate_manifest_id_rejected_before_registration():
    called = []
    def _reg(ctx):
        called.append(ctx)          # must never run
    dup = [PluginManifest(id="p1", register=_reg), PluginManifest(id="p1", register=_reg)]
    res = pr.stage_candidate(_app(), dup, live=PluginRegistry(),
                             reviewed_owners=frozenset({"p1"}))
    assert res.ok is False
    assert pr.DUPLICATE_MANIFEST_ID in _kinds(res)
    assert res.registry is None
    assert called == []             # rejected before any register()


def test_unreviewed_owner_is_restart_required_and_never_invoked():
    ran = []
    def _reg(ctx):
        ran.append(True)
    m = PluginManifest(id="p9", register=_reg)
    res = pr.stage_candidate(_app(), [m], live=PluginRegistry(),
                             reviewed_owners=frozenset())     # p9 not reviewed
    assert res.ok is False
    assert pr.RESTART_REQUIRED in _kinds(res)
    assert ran == []                # register() was never called


def test_duplicate_tool_name_within_candidate():
    m1 = _tool_manifest("p1", [(_spec("dup"), _pol(READ_ONLY))])
    m2 = _tool_manifest("p2", [(_spec("dup"), _pol(READ_ONLY))])
    res = pr.stage_candidate(_app(), [m1, m2], live=PluginRegistry(),
                             reviewed_owners=frozenset({"p1", "p2"}),
                             approved_new_tools=frozenset({"dup"}))
    assert res.ok is False
    assert pr.DUPLICATE_NAME in _kinds(res)


def test_prompt_slot_conflict():
    def _mk(owner):
        def _register(ctx):
            ctx.prompt_section(42, render=lambda: "x")
        return PluginManifest(id=owner, register=_register)
    res = pr.stage_candidate(_app(), [_mk("p1"), _mk("p2")], live=PluginRegistry(),
                             reviewed_owners=frozenset({"p1", "p2"}))
    assert res.ok is False
    assert pr.PROMPT_SLOT_CONFLICT in _kinds(res)


def test_register_failure_is_captured_not_raised():
    def _register(ctx):
        raise RuntimeError("boom")
    m = PluginManifest(id="p1", register=_register)
    res = pr.stage_candidate(_app(), [m], live=PluginRegistry(),
                             reviewed_owners=frozenset({"p1"}))
    assert res.ok is False
    assert pr.REGISTER_FAILED in _kinds(res)


# ── schema validation ───────────────────────────────────────────────────────
def test_malformed_schema_variants():
    bad_specs = [
        {"type": "tool", "function": {"name": "a", "parameters": {"type": "object",
                                                                  "properties": {}}}},  # wrong type
        {"type": "function", "function": {"name": "", "parameters": {"type": "object",
                                                                     "properties": {}}}},  # empty name
        {"type": "function", "function": {"name": "b"}},  # no parameters
        {"type": "function", "function": {"name": "c", "parameters": {"type": "object"}}},  # no properties
        {"type": "function", "function": {"name": "d", "parameters": {
            "type": "object", "properties": {}, "required": ["x"]}}},  # required not subset
    ]
    for i, spec in enumerate(bad_specs):
        m = _tool_manifest(f"p{i}", [(spec, _pol(READ_ONLY))])
        res = pr.stage_candidate(_app(), [m], live=PluginRegistry(),
                                 reviewed_owners=frozenset({f"p{i}"}),
                                 approved_new_tools=frozenset({"a", "b", "c", "d", ""}))
        assert pr.MALFORMED_SCHEMA in _kinds(res), f"variant {i} not flagged"
        assert res.ok is False


# ── authority validation ────────────────────────────────────────────────────
def test_policy_enlarged():
    spec = _spec("t1")
    live = _live_with("p1", spec, _pol(READ_ONLY))
    m = _tool_manifest("p1", [(spec, _pol(READ_ONLY, NETWORK))])
    res = pr.stage_candidate(_app(), [m], live=live, reviewed_owners=frozenset({"p1"}))
    assert res.ok is False
    assert pr.POLICY_ENLARGED in _kinds(res)


def test_confirmation_weakened():
    spec = _spec("t1")
    live = _live_with("p1", spec, _pol(WORKSPACE_WRITE, confirm_always=True))
    m = _tool_manifest("p1", [(spec, _pol(WORKSPACE_WRITE, confirm_always=False))])
    res = pr.stage_candidate(_app(), [m], live=live, reviewed_owners=frozenset({"p1"}))
    assert res.ok is False
    assert pr.CONFIRMATION_WEAKENED in _kinds(res)


def test_policy_omitted():
    spec = _spec("t1")
    live = _live_with("p1", spec, _pol(READ_ONLY))
    m = _tool_manifest("p1", [(spec, None)])   # falls back to MCP_UNKNOWN_POLICY
    res = pr.stage_candidate(_app(), [m], live=live, reviewed_owners=frozenset({"p1"}))
    assert res.ok is False
    assert pr.POLICY_OMITTED in _kinds(res)


def test_ownership_changed():
    spec = _spec("t1")
    live = _live_with("p1", spec, _pol(READ_ONLY))
    m = _tool_manifest("p2", [(spec, _pol(READ_ONLY))])   # same tool, new owner
    res = pr.stage_candidate(_app(), [m], live=live,
                             reviewed_owners=frozenset({"p2"}))
    assert res.ok is False
    assert pr.OWNERSHIP_CHANGED in _kinds(res)


# ── dynamic providers ───────────────────────────────────────────────────────
def test_dynamic_provider_restart_required_and_not_evaluated():
    evaluated = []
    def _register(ctx):
        ctx.dynamic_tools(
            specs_fn=lambda: (evaluated.append(True) or []),
            dispatch_fn=lambda name: None,
        )
    m = PluginManifest(id="mcp", register=_register)
    res = pr.stage_candidate(_app(), [m], live=PluginRegistry(),
                             reviewed_owners=frozenset({"mcp"}))
    assert res.ok is False
    assert pr.RESTART_REQUIRED in _kinds(res)
    assert evaluated == []          # specs_fn was never called
    assert any("dynamic" in lim for lim in res.limitations)


# ── review hardening (OpenBolt c52c453 review) ───────────────────────────────
def test_parameters_type_must_be_object():
    spec = _spec("arr", params={"type": "array", "properties": {}, "required": []})
    m = _tool_manifest("p1", [(spec, _pol(READ_ONLY))])
    res = pr.stage_candidate(_app(), [m], live=PluginRegistry(),
                             reviewed_owners=frozenset({"p1"}),
                             approved_new_tools=frozenset({"arr"}))
    assert res.ok is False
    assert pr.MALFORMED_SCHEMA in _kinds(res)


def test_spec_that_raises_in_add_tool_is_register_failed():
    # No function.name key -> registry.add_tool raises KeyError inside register();
    # honest classification is a registration failure, not malformed_schema.
    bad = {"type": "function", "function": {"parameters": {"type": "object", "properties": {}}}}
    m = _tool_manifest("p1", [(bad, _pol(READ_ONLY))])
    res = pr.stage_candidate(_app(), [m], live=PluginRegistry(),
                             reviewed_owners=frozenset({"p1"}))
    assert res.ok is False
    assert pr.REGISTER_FAILED in _kinds(res)


def test_approved_new_tool_requires_explicit_policy():
    # In approved_new_tools, but rides the MCP_UNKNOWN_POLICY fallback => rejected.
    m = _tool_manifest("p1", [(_spec("t2"), None)])
    res = pr.stage_candidate(_app(), [m], live=PluginRegistry(),
                             reviewed_owners=frozenset({"p1"}),
                             approved_new_tools=frozenset({"t2"}))
    assert res.ok is False
    assert pr.POLICY_OMITTED in _kinds(res)


def test_invalid_policy_type_reported_not_crashed():
    # A non-ToolPolicy object must be reported, not crash on .capabilities.
    m = _tool_manifest("p1", [(_spec("t3"), "not-a-policy")])
    res = pr.stage_candidate(_app(), [m], live=PluginRegistry(),
                             reviewed_owners=frozenset({"p1"}),
                             approved_new_tools=frozenset({"t3"}))
    assert res.ok is False
    assert pr.POLICY_INVALID in _kinds(res)


# ── isolation guarantee ─────────────────────────────────────────────────────
def test_live_registry_untouched_on_rejection():
    spec = _spec("t1")
    live = _live_with("p1", spec, _pol(READ_ONLY))
    before_names = [e.name for e in live.tools]
    # a candidate that will be rejected (capability enlargement)
    m = _tool_manifest("p1", [(spec, _pol(READ_ONLY, NETWORK))])
    res = pr.stage_candidate(_app(), [m], live=live, reviewed_owners=frozenset({"p1"}))
    assert res.ok is False
    assert [e.name for e in live.tools] == before_names
    # the live generation is still callable with its original run
    assert live.dispatch_for("t1")({}) == "LIVE"
