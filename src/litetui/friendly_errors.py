"""User-facing diagnostic copy. Keep classification here, not at each UI surface.

Only rewrite recognized failures: an unknown message may contain an important
remedy, so preserve it rather than offering a misleading generic instruction.
"""

from __future__ import annotations

import re

_MCP = re.compile(r"^(?:\[!\] mcp |Could not (?:re)?connect |Declared )(?P<name>[^:]+): (?P<reason>.+)$", re.IGNORECASE | re.DOTALL)


def display_error(raw: str, mode: str = "plain") -> str:
    """Return digestible copy or the exact original; never discard unknown detail."""
    if mode == "detail":
        return raw
    match = _MCP.match(raw)
    if match:
        name = match.group("name").strip(" '\".")
        reason = match.group("reason")
        if "cannot reach" in reason or "connection refused" in reason.lower() or "WinError 10061" in reason:
            if name == "litesuite-tools" or "localhost:7423" in reason or "127.0.0.1:7423" in reason:
                return "LiteSuite tools are offline. Start LiteSuite to use them; chat and other tools still work."
            return f"{name} tools are offline. Start their server, then use /mcp reconnect {name}; chat still works."
        if "no `command` in config" in reason or "no `url` in config" in reason:
            return f"{name} has an incomplete MCP setup. Check its entry in .mcp.json; chat still works."
    return raw
