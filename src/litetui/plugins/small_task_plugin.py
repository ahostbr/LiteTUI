"""Reachable opt-in dispatch; ordinary host approval semantics remain intact."""
from litetui import small_task_dispatch, tool_schemas
from litetui.plugins import PluginManifest
from litetui.tool_policy import CAPABILITIES, ToolPolicy

# Delegating a full worker can exercise every capability, not merely network/read.
POLICY = ToolPolicy(frozenset(CAPABILITIES), 'Dispatch a visible full worker and persist its receipt')


def _register(ctx):
    ctx.tool(tool_schemas.load('small_task'), lambda args: small_task_dispatch.run(ctx.app, args),
             policy=POLICY)


PLUGIN = PluginManifest(id='small_task', register=_register)
