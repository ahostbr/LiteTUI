"""tool_search — deferred tool loading, Claude Code's shape.

the user 2026-09-10 12:5x: "lazy load ... same way claude does it i think is best". Measured with
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


def _run(reg, args: dict, mcp=None) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "[error] tool_search: a query is required (keywords, or select:<name>)"
    before = set(reg.activated)
    hits = reg.search_tools(query)
    if mcp is not None:
        try:
            error = mcp.load_tools(query, hits)
        except Exception as exc:
            error = f"[error] tool_search MCP startup: {type(exc).__name__}: {exc}"
        reg.activated = before
        if error:
            return error
        # Read live schemas after starting: a cached tool can have disappeared.
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
    mcp = getattr(app, "mcp", None)
    ctx.tool(tool_schemas.load("tool_search"), lambda args: _run(reg, args, mcp), policy=READ_POLICY)

    def lazy_block():
        names = mcp.lazy_names() if mcp is not None else []
        return ("\nLazy MCP servers (not running): " + ", ".join(names)
                + ". Query tool_search with an exact server name to discover its tools.\n") if names else ""
    # Names only; the schemas load through the tool above. Gated on tools_enabled
    # for the same reason the skills index is: without the tool the list would
    # advertise things the model has no way to open.
    ctx.prompt_section(
        PROMPT_ORDER["DEFERRED_TOOLS"],
        lambda: deferred_tools_block(reg.deferred_specs()) + lazy_block(),
        enabled=lambda: app.tools_enabled and bool(reg.deferred_specs() or lazy_block()),
    )


PLUGIN = PluginManifest(id="tool_search", register=_register)
