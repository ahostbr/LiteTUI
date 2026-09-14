# C8 installed MCP catalog refresh observations

Installed binary reports codex-cli 0.154.0. The new
scripts/codex_mcp_catalog_probe.py starts a temporary synthetic stdio MCP server
under an isolated CODEX_HOME, without copying credentials or starting model turns.
The fixture changes alpha.value from string to integer, adds beta, and emits a
tools/list_changed notification. The script queries catalog status before/after,
then reloads unchanged config, then reloads a changed environment revision.

Two runs are retained separately:

- mcp-catalog-live.json: threadless status initially has alpha/string; subsequent
  status has alpha/integer and beta. tools/list observed versions 1,2,2,2.
- mcp-thread-catalog-live.json: adds one native thread/start and queries both
  threadless and thread-scoped status at each stage. The same native thread's
  status reports alpha/integer and beta after the fixture changes. Subsequent
  unchanged/config-revision reload observations retain that catalog. No turn/start
  is issued; all explicit probe requests are restricted to thread/start,
  mcpServerStatus/list and config/mcpServer/reload.

Both app-server processes closed and temporary homes were removed. Ruff passes.
The recorded accepted flag means the catalog observation completed, not C8 parity.

These observations do not isolate notification handling from status-driven
tools/list refresh: the fixture records tools/list calls at multiple stages.
They do not prove a model request consumes the new catalog, deferred search sees
new tools, execution uses the changed schema, disabled tools remain gated, or
history/context cache continuity under a real turn. No host bridge architecture
has been adopted on this evidence alone. The next behavioral gate is a bounded
same-thread synthetic model turn using the changed MCP inventory.

Source cross-check: the current upstream connection manager can reuse unchanged
connections, so a successful config reload is not itself schema-refresh proof:
https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/connection_manager.rs
This source may differ from the installed version; the JSON files capture the
installed catalog behavior. App-server protocol reference:
https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
