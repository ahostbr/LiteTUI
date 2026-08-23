"""The plugin substrate, exercised standalone — no Textual, no app.

Every enforcement claim gets both arms: the thing registering cleanly, and
the collision actually RAISING. A registry that promises "one owner per
fact" and is only ever tested with one owner has proven nothing.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui.plugins import (  # noqa: E402
    PluginManifest,
    PluginContext,
    PluginRegistry,
    activate_plugins,
    register_plugins,
)


def _spec(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": name}}


def _ctx(reg: PluginRegistry, owner: str, app=None) -> PluginContext:
    return PluginContext(app, reg, owner)


# ── tools ───────────────────────────────────────────────────────────────────

def test_gated_specs_and_ungated_dispatch():
    reg = PluginRegistry()
    ctx = _ctx(reg, "p1")
    ctx.tool(_spec("always"), lambda a: "A")
    ctx.tool(_spec("gated"), lambda a: "G", gate=lambda: False)

    names = [s["function"]["name"] for s in reg.tool_specs()]
    assert names == ["always"], "a False gate must remove the OFFER"
    # ...but never the dispatch: gates guard what is advertised, not what
    # resolves — exactly _dispatch_for's historical behavior.
    assert reg.dispatch_for("gated")({}) == "G"


def test_dynamic_tools_merge_after_static_and_dispatch_falls_through():
    reg = PluginRegistry()
    _ctx(reg, "static").tool(_spec("s1"), lambda a: "s1")
    _ctx(reg, "mcp").dynamic_tools(
        lambda: [_spec("m1"), _spec("m2")],
        lambda name: (lambda a: "M") if name == "m1" else None,
    )
    names = [s["function"]["name"] for s in reg.tool_specs()]
    assert names == ["s1", "m1", "m2"], "dynamic specs append AFTER static — MCP-last order"
    assert reg.dispatch_for("s1")({}) == "s1"
    assert reg.dispatch_for("m1")({}) == "M"
    assert reg.dispatch_for("nope") is None


def test_tool_name_collision_raises_naming_both_owners():
    reg = PluginRegistry()
    _ctx(reg, "first").tool(_spec("bash"), lambda a: "1")
    with pytest.raises(ValueError) as e:
        _ctx(reg, "second").tool(_spec("bash"), lambda a: "2")
    assert "first" in str(e.value) and "second" in str(e.value)


# ── commands ────────────────────────────────────────────────────────────────

def test_command_aliases_share_one_entry_and_collision_raises():
    reg = PluginRegistry()
    _ctx(reg, "convo").command(("/clear", "/new"), lambda app, n, a: None)
    assert reg.commands["/clear"] is reg.commands["/new"]
    # A colliding ALIAS is as fatal as a colliding primary token.
    with pytest.raises(ValueError) as e:
        _ctx(reg, "rogue").command(("/fresh", "/new"), lambda app, n, a: None)
    assert "convo" in str(e.value) and "rogue" in str(e.value)


# ── prompt sections ─────────────────────────────────────────────────────────

def test_prompt_sections_sort_by_order_and_slot_collision_raises():
    reg = PluginRegistry()
    _ctx(reg, "host").prompt_section(20, lambda: "tools")
    _ctx(reg, "host").prompt_section(0, lambda: "base")
    _ctx(reg, "skills").prompt_section(30, lambda: "index")
    assert [s.order for s in reg.sections_sorted()] == [0, 20, 30]
    with pytest.raises(ValueError) as e:
        _ctx(reg, "late").prompt_section(20, lambda: "usurper")
    assert "host" in str(e.value) and "late" in str(e.value)


# ── unload ──────────────────────────────────────────────────────────────────

def test_unload_sweeps_every_table_for_one_owner_only():
    reg = PluginRegistry()
    a, b = _ctx(reg, "a"), _ctx(reg, "b")
    a.tool(_spec("ta"), lambda x: "")
    b.tool(_spec("tb"), lambda x: "")
    a.command(("/ca",), lambda app, n, x: None)
    b.command(("/cb",), lambda app, n, x: None)
    a.dynamic_tools(lambda: [], lambda n: None)
    a.prompt_section(5, lambda: "")
    a.palette_row("row", "", lambda: None)

    reg.unload("a")

    assert [e.owner for e in reg.tools] == ["b"]
    assert reg.dispatch_for("ta") is None, "unload must also clear the name index"
    assert list(reg.commands) == ["/cb"]
    assert not reg.dynamic and not reg.prompt_sections and not reg.palette_rows


# ── loader ──────────────────────────────────────────────────────────────────

def _fake_module(name: str, manifest: PluginManifest) -> str:
    mod = types.ModuleType(name)
    mod.PLUGIN = manifest
    sys.modules[name] = mod
    return name


def test_loader_isolates_noncritical_failures_and_reraises_critical():
    reg = PluginRegistry()
    calls = []
    order = (
        _fake_module("_fp_ok", PluginManifest("ok", register=lambda c: calls.append("ok"))),
        _fake_module("_fp_boom", PluginManifest("boom", register=lambda c: 1 / 0)),
        _fake_module("_fp_after", PluginManifest("after", register=lambda c: calls.append("after"))),
    )
    manifests = register_plugins(None, reg, order=order)
    assert calls == ["ok", "after"], "one broken plugin must not cost the rest"
    assert reg.status["ok"] == "active"
    assert reg.status["boom"].startswith("failed: ZeroDivisionError")
    assert [m.id for m in manifests] == ["ok", "after"]

    crit = (_fake_module("_fp_crit", PluginManifest("core", critical=True, register=lambda c: 1 / 0)),)
    with pytest.raises(ZeroDivisionError):
        register_plugins(None, PluginRegistry(), order=crit)


def test_loader_disabled_skips_and_unknown_id_is_inert():
    reg = PluginRegistry()
    hits = []
    order = (
        _fake_module("_fp_d1", PluginManifest("wanted", register=lambda c: hits.append("wanted"))),
        _fake_module("_fp_d2", PluginManifest("unwanted", register=lambda c: hits.append("unwanted"))),
        _fake_module("_fp_d3", PluginManifest("floor", critical=True, register=lambda c: hits.append("floor"))),
    )
    # negative control: an id that names nothing must change nothing.
    register_plugins(None, reg, order=order, disabled={"unwanted", "floor", "ghost-id"})
    assert hits == ["wanted", "floor"], "critical ignores disable; ghost id is inert"
    assert reg.status["unwanted"] == "disabled"
    assert reg.status["floor"] == "active"


def test_loader_isolates_import_failures_unless_module_is_critical():
    # A module that cannot even IMPORT has no manifest to declare anything —
    # isolation must still hold, keyed by the module name.
    reg = PluginRegistry()
    hits = []
    order = (
        "_fp_does_not_exist_anywhere",
        _fake_module("_fp_survivor", PluginManifest("survivor", register=lambda c: hits.append("s"))),
    )
    register_plugins(None, reg, order=order)
    assert hits == ["s"]
    assert reg.status["_fp_does_not_exist_anywhere"].startswith("failed: ModuleNotFoundError")
    # ...but a CRITICAL module's import failure is a broken checkout: raise.
    from litetui import plugins as plugins_pkg
    saved = plugins_pkg.CRITICAL_MODULES
    plugins_pkg.CRITICAL_MODULES = frozenset({"_fp_missing_critical"})
    try:
        with pytest.raises(ModuleNotFoundError):
            register_plugins(None, PluginRegistry(), order=("_fp_missing_critical",))
    finally:
        plugins_pkg.CRITICAL_MODULES = saved


def test_activate_runs_in_order_and_isolates_noncritical_failures():
    reg = PluginRegistry()
    seen = []
    manifests = [
        PluginManifest("m1", activate=lambda app: seen.append(("m1", app))),
        PluginManifest("m2", activate=lambda app: 1 / 0),
        PluginManifest("m3", activate=lambda app: seen.append(("m3", app))),
    ]
    for m in manifests:
        reg.status[m.id] = "active"
    activate_plugins("APP", reg, manifests)
    assert seen == [("m1", "APP"), ("m3", "APP")]
    assert reg.status["m2"].startswith("failed at activate")
    assert reg.status["m1"] == "active"


def test_registries_are_isolated_instances():
    # The reason the registry is built per LiteTUI instance: two registries
    # in one process must share nothing, or one test's app leaks into the next.
    r1, r2 = PluginRegistry(), PluginRegistry()
    _ctx(r1, "p").tool(_spec("only-in-r1"), lambda a: "")
    assert r2.dispatch_for("only-in-r1") is None
    assert not r2.tools and not r2.status
