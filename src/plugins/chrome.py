"""Chrome automation — registration for chrome_tool.

Implementation stays in src/chrome_tool.py; this module registers it.
"""
import chrome_tool
from plugins import PluginManifest


def _register(ctx) -> None:
    ctx.tool(
        chrome_tool.CHROME_TOOL_SPEC,
        chrome_tool.run,
        gate=lambda: chrome_tool.SCRIPT.exists(),
    )


PLUGIN = PluginManifest(id="chrome", register=_register)
