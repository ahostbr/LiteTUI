"""Chrome automation — registration for chrome_tool.

Implementation stays in src/chrome_tool.py; this module registers it.
"""
from litetui import chrome_tool
from litetui.plugins import PluginManifest
from litetui.tool_policy import CHROME_POLICY


def _register(ctx) -> None:
    ctx.tool(
        chrome_tool.CHROME_TOOL_SPEC,
        chrome_tool.run,
        gate=lambda: chrome_tool.SCRIPT.exists(),
        policy=CHROME_POLICY,
    )


PLUGIN = PluginManifest(id="chrome", register=_register)
