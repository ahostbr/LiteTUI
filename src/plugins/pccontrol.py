"""Desktop control (mouse/keyboard/window) — registration for pccontrol_tool.

The implementation stays in src/pccontrol_tool.py, already the cleanest
tool shape in the tree (SPEC + run(args) + SCRIPT gate, zero app state);
this module is its registration into the plugin registry.
"""
import pccontrol_tool
from plugins import PluginManifest


def _register(ctx) -> None:
    # The gate is a LIVE check per spec assembly — install the script
    # mid-session and the tool appears next turn, exactly as before.
    ctx.tool(
        pccontrol_tool.PCCONTROL_TOOL_SPEC,
        pccontrol_tool.run,
        gate=lambda: pccontrol_tool.SCRIPT.exists(),
    )


PLUGIN = PluginManifest(id="pccontrol", register=_register)
