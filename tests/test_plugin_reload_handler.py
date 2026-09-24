"""Tests for the handler-generation reload orchestration (``plugin_reload_handler``).

Fake-only: the "module" being reloaded is a tiny inline source string, the swap
seams are recording lambdas, and the app is a SimpleNamespace. No real module
import, no App-owned resources, no engine/process/model load. These tests pin
the fail-closed guarantee: every failure path leaves ``app.plugins`` on the live
generation and claims no rollback, while a clean reload installs the new handler.
"""
from types import SimpleNamespace

from litetui.plugins import PluginContext, PluginManifest, PluginRegistry
from litetui.plugin_reload_handler import (
    HandlerReloadResult,
    render_handler_reload,
    reload_handler_generation,
)
from litetui.plugin_reload_state import ActivitySnapshot
from litetui.plugin_reload_swap import SwapSeams

_MOD = "litetui.plugins._ws7_handler_reloaded"
_OWNER = "reloaded"
_SPEC = {
    "type": "function",
    "function": {
        "name": "alpha",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _src_ok():
    return (
        "from litetui.plugins import PluginManifest\n"
        "_SPEC = " + repr(_SPEC) + "\n"
        "def new_alpha():\n"
        "    return 'fresh-alpha'\n"
        "def _register(ctx):\n"
        "    ctx.tool(_SPEC, new_alpha)\n"
        "PLUGIN = PluginManifest(id='reloaded', critical=False, register=_register, activate=None)\n"
    )


def _src_bad_load():
    return "raise RuntimeError('bad new code')\n"


def _src_adds_unapproved():
    # alpha (new handler) PLUS a brand-new tool 'gamma' that is in neither the
    # live registry nor approved_new_tools -> AUTHORITY_ADDED rejection.
    g = ("{'type':'function','function':{'name':'gamma','parameters':{"
         "'type':'object','properties':{},'required':[]}}}")
    return (
        "from litetui.plugins import PluginManifest\n"
        "_SPEC = " + repr(_SPEC) + "\n"
        "_G = " + g + "\n"
        "def new_alpha():\n"
        "    return 'fresh-alpha'\n"
        "def gamma():\n"
        "    return 'g'\n"
        "def _register(ctx):\n"
        "    ctx.tool(_SPEC, new_alpha)\n"
        "    ctx.tool(_G, gamma)\n"
        "PLUGIN = PluginManifest(id='reloaded', critical=False, register=_register, activate=None)\n"
    )


def _build_live():
    def _register(ctx):
        ctx.tool(_SPEC, _old_alpha)
    manifest = PluginManifest(id=_OWNER, critical=False, register=_register, activate=None)
    reg = PluginRegistry()
    manifest.register(PluginContext(None, reg, _OWNER))
    return manifest, reg


def _old_alpha():
    return "live-alpha"


def _app(reg):
    return SimpleNamespace(plugins=reg, backend=SimpleNamespace())


def _seams(log):
    return SwapSeams(validate_new=lambda: log.append("v"),
                     deactivate_old=lambda: log.append("d"),
                     activate_new=lambda: log.append("a"))


def test_happy_path_reloads_handler():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg)
    log = []
    r = reload_handler_generation(
        app, module_name=_MOD, source=_src_ok(),
        live_manifests=[live_manifest], reloaded_owner=_OWNER,
        reviewed_owners=frozenset({_OWNER}),
        eligible={_OWNER: True}, swap_seams={_OWNER: _seams(log)},
        activity=lambda: ActivitySnapshot())
    assert isinstance(r, HandlerReloadResult)
    assert r.status == "swapped"
    assert r.swapped is True
    assert r.token is not None
    assert log == ["v", "d", "a"]
    # The NEW generation's handler is now live on the swapped-in registry.
    entry = app.plugins._tool_by_name["alpha"]
    assert entry.run() == "fresh-alpha"
    assert entry.owner == _OWNER


def test_fresh_load_failure_preserves_live_and_has_no_token():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg)
    log = []
    r = reload_handler_generation(
        app, module_name=_MOD, source=_src_bad_load(),
        live_manifests=[live_manifest], reloaded_owner=_OWNER,
        reviewed_owners=frozenset({_OWNER}),
        eligible={_OWNER: True}, swap_seams={_OWNER: _seams(log)},
        activity=lambda: ActivitySnapshot())
    assert r.status == "failed"
    assert r.token is None
    assert r.swapped is False
    assert log == []  # no seam ran
    assert app.plugins is live_reg
    assert app.plugins._tool_by_name["alpha"].run() == "live-alpha"
    assert "no rollback" in " ".join(r.reasons)


def test_candidate_rejection_preserves_live_but_carries_token():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg)
    log = []
    r = reload_handler_generation(
        app, module_name=_MOD, source=_src_adds_unapproved(),
        live_manifests=[live_manifest], reloaded_owner=_OWNER,
        reviewed_owners=frozenset({_OWNER}),
        eligible={_OWNER: True}, swap_seams={_OWNER: _seams(log)},
        activity=lambda: ActivitySnapshot())
    assert r.status == "failed"
    assert r.token is not None  # fresh load succeeded; the candidate was the failure
    assert r.swapped is False
    assert log == []
    assert app.plugins is live_reg
    joined = " ".join(r.reasons)
    assert "candidate rejected" in joined
    assert "authority_added" in joined


def test_not_eligible_is_restart_required_without_swap():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg)
    log = []
    r = reload_handler_generation(
        app, module_name=_MOD, source=_src_ok(),
        live_manifests=[live_manifest], reloaded_owner=_OWNER,
        reviewed_owners=frozenset({_OWNER}),
        eligible={_OWNER: False},  # the eligibility gate says no
        swap_seams={_OWNER: _seams(log)},
        activity=lambda: ActivitySnapshot())
    assert r.status == "restart-required"
    assert r.swapped is False
    assert r.token is not None
    assert log == []  # the gate refuses before any seam
    assert app.plugins is live_reg


def test_not_idle_defers_without_swap():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg)
    log = []
    r = reload_handler_generation(
        app, module_name=_MOD, source=_src_ok(),
        live_manifests=[live_manifest], reloaded_owner=_OWNER,
        reviewed_owners=frozenset({_OWNER}),
        eligible={_OWNER: True}, swap_seams={_OWNER: _seams(log)},
        activity=lambda: ActivitySnapshot(turn_active=True))
    assert r.status == "deferred"
    assert r.swapped is False
    assert r.token is not None
    assert log == []
    assert app.plugins is live_reg


def test_activate_failure_after_swap_is_degraded():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg)

    def _bad_activate():
        raise RuntimeError("new gen activate failed")
    seams = SwapSeams(validate_new=lambda: None,
                      deactivate_old=lambda: None,
                      activate_new=_bad_activate)
    r = reload_handler_generation(
        app, module_name=_MOD, source=_src_ok(),
        live_manifests=[live_manifest], reloaded_owner=_OWNER,
        reviewed_owners=frozenset({_OWNER}),
        eligible={_OWNER: True}, swap_seams={_OWNER: seams},
        activity=lambda: ActivitySnapshot())
    assert r.status == "degraded"
    assert r.swapped is True
    assert r.token is not None
    # The point of no return was crossed: the pointer was swapped.
    assert app.plugins is not live_reg


# ── operator feedback (the concise result line) ──────────────────────────


def test_render_reloaded_names_the_generation_and_no_rollback():
    line = render_handler_reload("reloaded", HandlerReloadResult(
        "swapped", (), token="abc", swapped=True))
    assert line.startswith("[reload-plugins] reloaded:")
    assert "reloaded" in line and "no rollback" in line


def test_render_unchanged():
    line = render_handler_reload("reloaded", HandlerReloadResult("no-op"))
    assert line.startswith("[reload-plugins] unchanged:")


def test_render_deferred_says_not_auto_retried():
    line = render_handler_reload("reloaded", HandlerReloadResult(
        "deferred", ("a tool call is executing",)))
    assert line.startswith("[reload-plugins] deferred:")
    assert "a tool call is executing" in line
    assert "Not retried automatically" in line


def test_render_restart_required_and_failed_keep_live_note():
    assert "restart required:" in render_handler_reload(
        "reloaded", HandlerReloadResult("restart-required", ("not eligible",)))
    failed = render_handler_reload("reloaded", HandlerReloadResult(
        "failed", ("fresh generation failed (ValueError); live generation preserved, no rollback",)))
    assert failed.startswith("[reload-plugins] failed:")
    assert "Live handlers unchanged" in failed


def test_render_degraded_never_claims_rollback():
    line = render_handler_reload("reloaded", HandlerReloadResult(
        "degraded", ("activate failed after swap (RuntimeError)",), token="t", swapped=True))
    assert line.startswith("[reload-plugins] degraded:")
    assert "restart is required" in line
    assert "not rolled back" in line.lower()
    assert "restored" not in line.lower()
