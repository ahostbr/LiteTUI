"""grep + edit — registration for the surgical file tools.

Implementation lives in src/litetui/file_tools.py (specs + handlers); this
module only binds them into the plugin registry with their policies: grep is
read-only inspection, edit writes and so carries WRITE_POLICY's path-based
classification (self-store vs workspace vs external).
"""
from litetui import file_tools
from litetui.plugins import PluginManifest
from litetui.tool_policy import READ_POLICY, WRITE_POLICY


def _register(ctx) -> None:
    ctx.tool(file_tools.GREP_TOOL_SPEC, file_tools.tool_grep, policy=READ_POLICY)
    ctx.tool(file_tools.EDIT_TOOL_SPEC, file_tools.tool_edit, policy=WRITE_POLICY)


PLUGIN = PluginManifest(id="file-tools", register=_register)
