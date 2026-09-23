"""Plain Talk presentation of known diagnostics; Full Detail keeps exact raw text."""
from __future__ import annotations

import re

_MCP = re.compile(r"^(?:\[!\] mcp |Could not (?:re)?connect )(?P<name>[^:]+): (?P<reason>.+)$", re.IGNORECASE | re.DOTALL)
_DECLARED = re.compile(r"^Declared (?P<name>.+?) in \.mcp\.json, but it did not start: (?P<reason>.+)$", re.DOTALL)


def _mcp_reason(name: str, reason: str) -> str | None:
    name = name.strip(" '\".")
    low = reason.lower()
    if "cannot reach" in low or "connection refused" in low or "winerror 10061" in low:
        if name == "litesuite-tools" or "localhost:7423" in low or "127.0.0.1:7423" in low:
            return "LiteSuite tools are offline. Start LiteSuite to use them; chat and other tools still work."
        return f"{name} tools are offline. Start their server, then use /mcp reconnect {name}; chat still works."
    if "no `command` in config" in low or "no `url` in config" in low:
        return f"{name} has an incomplete MCP setup. Check its entry in .mcp.json; chat still works."
    return None


def display_error(raw: str, mode: str = "plain", *, surface: str = "chat") -> str:
    """Rewrite recognized user-facing errors only; unknown diagnostics remain exact."""
    if mode == "detail":
        return raw
    match = _MCP.match(raw)
    if match:
        return _mcp_reason(match["name"], match["reason"]) or raw
    match = _DECLARED.match(raw)
    if match:
        reason = _mcp_reason(match["name"], match["reason"])
        return f"Declared {match['name']} in .mcp.json, but it did not start. {reason}" if reason else raw
    if surface.startswith("mcp") and raw.startswith("MCPError:"):
        return _mcp_reason(surface.partition(":")[2] or "this server", raw) or raw
    if surface == "mcp":
        match = re.match(r"(?:could not (?:re)?connect |declared )(?P<name>[^:,]+)(?:, but it did not start)?: (?P<reason>.+)", raw, re.IGNORECASE | re.DOTALL)
        if match:
            reason = _mcp_reason(match["name"], match["reason"])
            if reason:
                return (f"Declared {match['name']}, but it did not start. {reason}"
                        if raw.lower().startswith("declared ") else reason)
    if surface == "settings":
        if raw.startswith(("Cannot save", "Save failed")):
            return "Settings could not be saved. Check that the settings file is writable, then retry."
        if raw.startswith("Cannot retry"):
            return "Settings could not be retried. Reopen /settings and try again."
        if raw.startswith("Runtime apply failed"):
            return "Settings were saved, but a running service could not apply them. Reconnect to apply them."
    if surface == "plugin" and ("failed:" in raw or "failed at activate:" in raw):
        return "A plugin could not start or finish its work. Check /plugins and the runtime error log."
    if surface == "tool" and raw.startswith("[error] invalid tool arguments:"):
        return "The tool arguments were not valid JSON. Check the argument shape and retry."
    if surface == "paste" and raw.startswith("Paste failed:"):
        return "Paste failed — the clipboard did not hold a readable image. Copy an image and try again."
    if surface == "theme" and raw.startswith("custom theme ") and " skipped:" in raw:
        return f"{raw.split(' skipped:', 1)[0]} could not load. Check its colors in /settings."
    return raw


def present(raw: str, mode: str, *, surface: str = "chat") -> str:
    """Present mapped copy, retaining the raw diagnostic in the runtime log."""
    from litetui import runtime_log

    shown = display_error(raw, mode, surface=surface)
    if shown != raw:
        runtime_log.record_error("user_message_simplified", detail=raw, surface=surface)
    return shown
