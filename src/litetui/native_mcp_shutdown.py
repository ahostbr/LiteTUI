"""Join the constructor-owned MCP manager before native acknowledgement or lease release."""

from __future__ import annotations

from litetui.agent_preparation import await_preparation
from litetui.mcp_client import MCPManager


async def settle_before_shutdown(app):
    manager = getattr(app, "_owned_mcp_manager", None)
    if manager is None:
        if getattr(app, "mcp", None) is not None:
            raise RuntimeError("MCP cleanup lacks its captured manager owner")
        return
    if not isinstance(manager, MCPManager):
        raise TypeError("MCP cleanup owner is not a source manager")

    # Close admission before yielding; a previously admitted connect is joined
    # by stop_all's operation lock. Cancellation cannot abandon that thread.
    manager._closing = True
    await await_preparation(manager.stop_all)
    with manager._servers_lock:
        retained = bool(manager.servers or manager._stop_failed)
    if retained:
        raise RuntimeError(
            "MCP cleanup retains unconfirmed server handles; retry shutdown"
        )
    if getattr(app, "mcp", None) is not manager:
        raise RuntimeError(
            "MCP manager changed; cleanup of the current manager is unproven"
        )
