"""Bounded runtime and cached MCP metadata for native status inspection."""
from __future__ import annotations

import re

from litetui import background_input as work
from litetui.native_context import read_binding

OPERATION = "gui.inspection.native_status"


def _persistence(owner, key):
    if owner is None or not hasattr(owner, key):
        return "unavailable"
    return "error" if getattr(owner, key) else "ok"


def status(app, cmd):
    from litetui.gui_rpc import PROTOCOL_VERSION
    from litetui.mcp_client import MCPManager
    from litetui.version import __version__

    bridge, binding = read_binding(app, cmd, OPERATION)
    if not re.fullmatch(r"[A-Za-z0-9._+\-]{1,64}", __version__) or PROTOCOL_VERSION != 1:
        raise ValueError("Unsupported runtime status version")
    manager = getattr(app, "mcp", None)
    enabled = getattr(app.settings, "mcp_enabled", None)
    mcp = {"enabled": enabled if type(enabled) is bool else None,
           "available": False, "complete": False, "remaining": None, "servers": []}
    if isinstance(manager, MCPManager):
        mcp.update(manager.inspect_cached())
    bridge.check()
    result = {"version": 1, "kind": "runtime_inspection", **binding,
            "diagnostics": {"runtime_version": __version__, "protocol_version": PROTOCOL_VERSION,
                            "conversation_persistence": _persistence(app.store, "persist_error"),
                            "settings_persistence": _persistence(app, "_settings_persist_error")},
            "mcp": mcp}
    if len(work.encoded(result)) > 32768:
        raise ValueError("Runtime status exceeds its complete response budget")
    return result
