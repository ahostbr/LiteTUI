"""Tests for the handler-reload App-state assembly (``plugin_reload_command``) and
the ``/reload-plugins`` handler-reload branch (``plugin_reload_ui``).

Fake-only: the "module" being reloaded is a tiny inline source string, the app is
a SimpleNamespace, and the App-state resolution is an injected ``resolve``. No
real plugin import, no App-owned resources, no engine/process/model load. One
test drives the production branch end-to-end against a FAKE litetui plugin module
in sys.modules (still no real plugin / engine). These pin:

  (a) the assembly is fail-closed -- every non-eligible path leaves app.plugins on
      the live generation and claims no rollback;
  (b) a declaring + reviewed + stateless + seam-complete plugin swaps the handler;
  (c) the branch is a clean fall-through (None) for any target that does not
      declare RELOAD_COMPATIBLE, so the verified-green metadata-only path -- and
      the metadata-only command behaviour for a tool name -- is preserved exactly.
"""
import sys
import types
from types import SimpleNamespace

from litetui.plugins import PluginContext, PluginManifest, PluginRegistry
from litetui.plugin_reload_command import (
    ReloadCompatibility,
    ReloadTarget,
    handler_reload,
)
from litetui.plugin_reload_eligibility import LifecycleSeams
from litetui.plugin_reload_state import ActivitySnapshot

_MOD = "litetui.plugins._ws7_cmd_reloaded"
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


def _old_alpha():
    return "live-alpha"


def _build_live():
    def _register(ctx):
        ctx.tool(_SPEC, _old_alpha)
    manifest = PluginManifest(id=_OWNER, critical=False, register=_register, activate=None)
    reg = PluginRegistry()
    manifest.register(PluginContext(None, reg, _OWNER))
    return manifest, reg


def _app(reg, manifest):
    return SimpleNamespace(plugins=reg, backend=SimpleNamespace(), _plugin_manifests=[manifest])


def _seams(complete: bool = True):
    if complete:
        return LifecycleSeams(prepare=lambda: None, validate=lambda: None,
                              activate=lambda: None, deactivate=lambda: None)
    # Missing `deactivate` -> incomplete seam set (the gate must refuse).
    return LifecycleSeams(prepare=lambda: None, validate=lambda: None,
                          activate=lambda: None, deactivate=None)


def _compat(**kw):
    kw.setdefault("seams", _seams())
    return ReloadCompatibility(**kw)


def _target(compat):
    return ReloadTarget(owner=_OWNER, module_name=_MOD, source=_src_ok(), compat=compat)


def _run(app, compat, *, reviewed=frozenset({_OWNER}), activity=lambda: ActivitySnapshot()):
    live_manifest = app._plugin_manifests[0]
    return handler_reload(
        app, _OWNER,
        resolve=lambda _t: _target(compat),
        live_manifests=[live_manifest],
        reviewed_owners=reviewed, activity=activity)


# ── assembly: fail-closed eligibility gates (no swap on refusal) ──────────

def test_declaring_eligible_reloads_handler():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg, live_manifest)
    r = _run(app, _compat(stateless=True))
    assert r.status == "swapped"
    assert r.swapped is True
    assert app.plugins is not live_reg
    assert app.plugins._tool_by_name["alpha"].run() == "fresh-alpha"


def test_no_declaration_is_restart_required():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg, live_manifest)
    r = handler_reload(app, _OWNER, resolve=lambda _t: None,
                       live_manifests=[live_manifest],
                       reviewed_owners=frozenset({_OWNER}),
                       activity=lambda: ActivitySnapshot())
    assert r.status == "restart-required"
    assert "no handler-reload compatibility declared" in " ".join(r.reasons)
    assert r.swapped is False
    assert app.plugins is live_reg


def test_not_reviewed_is_restart_required():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg, live_manifest)
    r = _run(app, _compat(stateless=True), reviewed=frozenset())
    assert r.status == "restart-required"
    assert "not in reviewed" in " ".join(r.reasons)
    assert app.plugins is live_reg


def test_stateful_is_restart_required():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg, live_manifest)
    r = _run(app, _compat(stateless=False))
    assert r.status == "restart-required"
    assert "stateful" in " ".join(r.reasons)
    assert app.plugins is live_reg


def test_owns_resource_is_restart_required():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg, live_manifest)
    r = _run(app, _compat(stateless=True, owns=("timers",)))
    assert r.status == "restart-required"
    assert "owns" in " ".join(r.reasons)
    assert app.plugins is live_reg


def test_incomplete_seams_is_restart_required():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg, live_manifest)
    r = _run(app, _compat(stateless=True, seams=_seams(complete=False)))
    assert r.status == "restart-required"
    assert "seams" in " ".join(r.reasons)
    assert app.plugins is live_reg


def test_not_idle_defers():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg, live_manifest)
    r = _run(app, _compat(stateless=True),
             activity=lambda: ActivitySnapshot(turn_active=True))
    assert r.status == "deferred"
    assert app.plugins is live_reg


def test_activate_failure_is_degraded():
    live_manifest, live_reg = _build_live()
    app = _app(live_reg, live_manifest)

    def _bad():
        raise RuntimeError("new gen activate failed")
    seams = LifecycleSeams(prepare=lambda: None, validate=lambda: None,
                           activate=_bad, deactivate=lambda: None)
    r = _run(app, _compat(stateless=True, seams=seams))
    assert r.status == "degraded"
    assert r.swapped is True  # the point of no return was crossed
    assert app.plugins is not live_reg


# ── production branch: fall-through + end-to-end (fake module) ────────────

def test_branch_falls_through_for_tool_name():
    # A tool name that is not a plugin declaring RELOAD_COMPATIBLE -> None, so
    # the metadata-only path (unchanged) runs for it.
    import litetui.plugins.plugin_reload_ui as ui
    line = ui._handler_reload_branch(SimpleNamespace(plugins=PluginRegistry()), "alpha")
    assert line is None


def test_branch_end_to_end_with_declaring_module(monkeypatch):
    import litetui.plugins.plugin_reload_ui as ui

    fake_mod_name = "litetui.plugins._ws7_cmd_fakeplug"
    fake_owner = "fakeplug"
    fresh = (
        "from litetui.plugins import PluginManifest\n"
        "_SPEC = " + repr(_SPEC) + "\n"
        "def new_alpha():\n"
        "    return 'fresh-alpha'\n"
        "def _register(ctx):\n"
        "    ctx.tool(_SPEC, new_alpha)\n"
        "PLUGIN = PluginManifest(id='fakeplug', critical=False, register=_register, activate=None)\n"
    )

    def _live_register(ctx):
        ctx.tool(_SPEC, _old_alpha)
    live_manifest = PluginManifest(id=fake_owner, critical=False, register=_live_register, activate=None)
    live_reg = PluginRegistry()
    live_manifest.register(PluginContext(None, live_reg, fake_owner))

    # A FAKE litetui plugin module in sys.modules: it declares RELOAD_COMPATIBLE
    # and its host-reviewed id. read_live_module_source is patched to hand back
    # the fresh source (bypassing the real __file__ read; the swap still execs it
    # into an isolated generation exactly as production would).
    mod = types.ModuleType(fake_mod_name)
    mod.PLUGIN = live_manifest
    mod.RELOAD_COMPATIBLE = ReloadCompatibility(
        stateless=True, owns=(),
        seams=LifecycleSeams(prepare=lambda: None, validate=lambda: None,
                             activate=lambda: None, deactivate=lambda: None))
    sys.modules[fake_mod_name] = mod
    monkeypatch.setattr(ui, "read_live_module_source", lambda _n: fresh)
    monkeypatch.setattr(ui, "_HANDLER_RELOAD_REVIEWED", frozenset({fake_owner}))
    app = SimpleNamespace(plugins=live_reg, backend=SimpleNamespace(),
                          _plugin_manifests=[live_manifest], convo_id="test-convo")
    try:
        line = ui._handler_reload_branch(app, fake_owner, activity=lambda: ActivitySnapshot())
    finally:
        sys.modules.pop(fake_mod_name, None)
    assert line is not None
    assert line.startswith("[reload-plugins] reloaded:")
    assert app.plugins is not live_reg
    assert app.plugins._tool_by_name["alpha"].run() == "fresh-alpha"
