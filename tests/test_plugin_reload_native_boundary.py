"""Native reload boundary — pin that the Codex per-thread schema guard refuses
ANY change to an existing tool's fingerprint, including a description-only edit.

This exists so no future change can quietly assume "a cosmetic schema refresh is
safe everywhere". On the native app-server path a tool's dynamicTools entry is
registered once at thread/start and never re-sent (see
ws7-provider-cache-trace-fluxwedge.md), and build_inventory fingerprints the
WHOLE tool — name + description + inputSchema — so a description-only reload
drifts the fingerprint and codex_inventory.dispatch_denial refuses it.

Characterization tests of existing behaviour; no production change. Pure: they
call dispatch_denial / build_inventory directly with a SimpleNamespace app, so
nothing launches a real codex app-server (existing native fixtures in
tests/test_codex_app_server.py drive a fake Server; this boundary needs neither).

Run only this file:
    python -m pytest tests/test_plugin_reload_native_boundary.py
"""
from types import SimpleNamespace as NS

from litetui.codex_inventory import build_inventory, dispatch_denial


def _spec(name: str, description: str = "original", schema: dict | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema or {"type": "object", "properties": {}},
        },
    }


def _app(live_specs: list[dict], enabled: bool = True) -> NS:
    # dispatch_denial reads app.tools_enabled and app.plugins.tool_specs();
    # build_inventory (called inside it) also reads app.plugins.deferred_specs().
    return NS(
        tools_enabled=enabled,
        plugins=NS(tool_specs=lambda: live_specs, deferred_specs=lambda: []),
    )


def _fp(spec: dict) -> str:
    return build_inventory([spec], _app([spec]))[1]["schemas"]["litetui_" + spec["function"]["name"]]


# ── the boundary: a description-only change is NOT cosmetic on native ─────────
def test_description_only_change_is_refused_on_native():
    orig = _spec("a", description="original")
    changed = _spec("a", description="a slightly different description")  # name + params identical
    registered = build_inventory([orig], _app([orig]))[1]
    orig_fp = registered["schemas"]["litetui_a"]

    # live registry now serves the description-changed spec (a reload happened)
    denial = dispatch_denial(registered, _app([changed]), "a")
    assert denial is not None
    assert "definition changed" in denial          # stale-schema refusal, not "not registered"

    # the thread's registered snapshot still holds the ORIGINAL fingerprint...
    assert registered["schemas"]["litetui_a"] == orig_fp
    # ...and the description-only edit really did change the fingerprint.
    assert _fp(changed) != orig_fp


def test_unchanged_tool_is_allowed_on_native():
    orig = _spec("a")
    registered = build_inventory([orig], _app([orig]))[1]
    assert dispatch_denial(registered, _app([orig]), "a") is None   # positive control


def test_inputschema_change_is_refused_on_native():
    orig = _spec("a", schema={"type": "object", "properties": {}})
    changed = _spec("a", schema={"type": "object", "properties": {"x": {"type": "string"}}})
    registered = build_inventory([orig], _app([orig]))[1]
    denial = dispatch_denial(registered, _app([changed]), "a")
    assert denial is not None and "definition changed" in denial


def test_new_tool_added_by_reload_is_not_registered_on_native():
    registered = build_inventory([_spec("a")], _app([_spec("a")]))[1]
    # reload added tool "b"; the live registry serves it, but the thread never registered it
    denial = dispatch_denial(registered, _app([_spec("a"), _spec("b")]), "b")
    assert denial is not None
    assert "not registered in this Codex thread" in denial


def test_removed_tool_is_refused_on_native():
    registered = build_inventory([_spec("a")], _app([_spec("a")]))[1]
    denial = dispatch_denial(registered, _app([]), "a")   # reload removed it from live
    assert denial is not None
    assert "no longer available" in denial


def test_disabled_tools_refused_on_native():
    orig = _spec("a")
    registered = build_inventory([orig], _app([orig]))[1]
    denial = dispatch_denial(registered, _app([orig], enabled=False), "a")
    assert denial is not None and "disabled" in denial
