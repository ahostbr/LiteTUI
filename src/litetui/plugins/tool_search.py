"""tool_search — deferred tool loading, Claude Code's shape.

Ryan 2026-09-10 12:5x: "lazy load ... same way claude does it i think is best". Measured with
qwen3.5-9b's own tokenizer the same hour (scratchpad litetui_context_cost.py): a first request
carried 21,727 prompt tokens, of which the 41 MCP schemas were 9,125 and the four heaviest
built-ins (studio, subagent, listen, pccontrol) about 2,600. Every one of them rode every turn
whether or not the turn could use them.

The shape: those tools are DEFERRED — still dispatchable, but their schemas are withheld from
the request until the model loads them through this tool, or calls one by name (which counts
as knowing it and activates it). The system prompt carries a names-only index
(`deferred_tools_block`) so the model knows what exists. The `skill` tool got the same
treatment for the skills index the same day.

Deferral lives on the registry (`deferred_static`, `defer_dynamic`, `activated`) but is OFF on
a bare registry; this plugin is the one place that turns it on, so every existing test and
caller of `tool_specs()` keeps the old offer.
"""
import json

from litetui import tool_schemas
from litetui.plugins import PROMPT_ORDER, PluginManifest, deferred_tools_block
from litetui.tool_policy import READ_POLICY

#: Built-ins withheld until loaded. Every MCP tool is deferred (registry.defer_dynamic).
DEFAULT_DEFERRED = frozenset({"studio", "subagent", "listen", "pccontrol"})


def _run(reg, args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "[error] tool_search: a query is required (keywords, or select:<name>)"
    hits = reg.search_tools(query)
    if not hits:
        names = ", ".join(sorted(s["function"]["name"] for s in reg.deferred_specs())) or "(none)"
        return f"no deferred tool matches {query!r}. Deferred names: {names}"
    loaded = ", ".join(s["function"]["name"] for s in hits)
    return (
        f"Loaded {len(hits)} tool(s): {loaded}. Callable from your next turn on. Schemas:\n"
        + json.dumps(hits, ensure_ascii=False)
    )


def _register(ctx) -> None:
    app = ctx.app
    reg = app.plugins
    reg.deferred_static = DEFAULT_DEFERRED
    reg.defer_dynamic = True
    ctx.tool(tool_schemas.load("tool_search"), lambda args: _run(reg, args), policy=READ_POLICY)
    # Names only; the schemas load through the tool above. Gated on tools_enabled
    # for the same reason the skills index is: without the tool the list would
    # advertise things the model has no way to open.
    ctx.prompt_section(
        PROMPT_ORDER["DEFERRED_TOOLS"],
        lambda: deferred_tools_block(reg.deferred_specs()),
        enabled=lambda: app.tools_enabled and bool(reg.deferred_specs()),
    )


PLUGIN = PluginManifest(id="tool_search", register=_register)
