"""Deterministic native tool registration and schema fingerprints."""

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar

from litetui.model_transport import ProviderError

_NOT_NATIVE = object()
_registered = ContextVar("codex_registered_inventory", default=_NOT_NATIVE)


@contextmanager
def registered_tools(inventory):
    token = _registered.set(inventory)
    try:
        yield
    finally:
        _registered.reset(token)


def current_dispatch_denial(app, name):
    inventory = _registered.get()
    return None if inventory is _NOT_NATIVE else dispatch_denial(inventory, app, name)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def build_inventory(specs, app=None):
    active, deferred, known = [], [], {}
    if app is not None and not getattr(app, "tools_enabled", True):
        specs = []
        deferred_specs = []
    else:
        deferred_specs = app.plugins.deferred_specs() if app is not None else []
    for source, target, delayed in (
        (specs, active, False),
        (deferred_specs, deferred, True),
    ):
        for spec in source:
            function = spec["function"]
            name = "litetui_" + function["name"]
            tool = {
                "type": "function",
                "name": name,
                "description": function.get("description", ""),
                "inputSchema": function.get("parameters", {}),
            }
            signature = fingerprint(tool)
            if name in known:
                if known[name] != signature:
                    raise ProviderError(
                        f"Conflicting Codex tool definitions for {name}."
                    )
                continue
            known[name] = signature
            if delayed:
                tool["deferLoading"] = True
            target.append(tool)
    # Stable order avoids context/cache churn from registry enumeration order.
    tools = sorted(active, key=lambda tool: tool["name"])
    if deferred:
        tools.append(
            {
                "type": "namespace",
                "name": "litetui",
                "description": "Additional LiteTUI host tools available through tool search.",
                "tools": sorted(deferred, key=lambda tool: tool["name"]),
            }
        )
    return tools, {
        "version": 1,
        "digest": fingerprint(tools),
        "schemas": dict(sorted(known.items())),
    }


def dispatch_denial(registered, app, name):
    """Reject remembered schemas that no longer match the live gated registry."""
    if registered is None:
        return "This Codex thread has no verified host-tool registration snapshot; the tool was not executed."
    if not isinstance(registered, dict) or registered.get("version") != 1:
        return "Codex tool registration metadata is unavailable or unsupported."
    schemas = registered.get("schemas")
    if not isinstance(schemas, dict) or "litetui_" + name not in schemas:
        return "This tool was not registered in this Codex thread."
    if not getattr(app, "tools_enabled", True):
        return "Host tools are disabled."
    registry = getattr(app, "plugins", None)
    if registry is None or not callable(getattr(registry, "tool_specs", None)):
        return "The current host tool inventory could not be verified."
    try:
        _, current = build_inventory(registry.tool_specs(), app)
    except (ProviderError, KeyError, TypeError, ValueError):
        return "The current host tool inventory could not be verified."
    signature = current["schemas"].get("litetui_" + name)
    if signature is None:
        return "This remembered tool is disabled or no longer available."
    if signature != schemas["litetui_" + name]:
        return "This tool's definition changed after Codex registered it; stale-schema execution was refused."
    return None
