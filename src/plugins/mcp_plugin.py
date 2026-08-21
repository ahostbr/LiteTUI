"""MCP servers — the out-of-process plugin tier, joined to the one registry.

Registered as a DYNAMIC provider because MCP's tool set is variable-count
and resolved at call time (servers start and stop after boot); a static
per-spec registration would silently freeze the set. The provider callables
take NO app handle — arguments in, strings out — which is exactly the
privilege MCP has always had. Unifying the registries must not widen it.

MCPManager construction and load() stay host-owned lines in __init__
(a broken mcp.json should fail loud there, not vanish into per-plugin
isolation).
"""
from plugins import PluginManifest


def _register(ctx) -> None:
    app = ctx.app
    ctx.dynamic_tools(
        lambda: app.mcp.tool_specs(),
        lambda name: app._mcp_dispatch.get(name),
    )


PLUGIN = PluginManifest(id="mcp", register=_register)
