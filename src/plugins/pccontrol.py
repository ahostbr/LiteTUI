"""Desktop control (mouse/keyboard/window) — registration for pccontrol_tool.

The implementation stays in src/pccontrol_tool.py, already the cleanest
tool shape in the tree (SPEC + run(args) + SCRIPT gate, zero app state);
this module is its registration into the plugin registry.
"""
import pccontrol_tool
from plugins import PluginManifest
from tool_policy import PCCONTROL_POLICY


def _register(ctx) -> None:
    # The gate is a LIVE check per spec assembly — install the script
    # mid-session and the tool appears next turn, exactly as before.
    ctx.tool(
        pccontrol_tool.PCCONTROL_TOOL_SPEC,
        pccontrol_tool.run,
        gate=lambda: pccontrol_tool.SCRIPT.exists(),
        policy=PCCONTROL_POLICY,
    )


PLUGIN = PluginManifest(id="pccontrol", register=_register)
