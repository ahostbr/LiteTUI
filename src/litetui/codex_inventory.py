"""Deterministic native tool registration and schema fingerprints."""

import hashlib
import json

from litetui.model_transport import ProviderError


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
