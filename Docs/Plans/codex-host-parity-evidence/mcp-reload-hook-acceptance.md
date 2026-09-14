# C8 reload mechanism and C1 MCP hook invocation evidence

The definitive current probe is mcp-config-revision-validated-live.json. One native
Astra/medium thread executes alpha under schema version 1, then, after changing the
synthetic MCP server's config environment revision and requesting
config/mcpServer/reload, executes newly added beta and alpha under its integer
schema version 2. All three fixture calls are valid. Both turns complete normally,
with one unchanged thread ID and no model-history reconstruction or thread reset.
The fixture sees tools/list versions [1,2]. Native usage/cached-input counters are
recorded; this is not a guarantee of a particular cache hit rate after tool changes.

The probe verifies its own hook is discovered, trusted and enabled before model
work. All three PreToolUse helpers run and native hook statuses are completed.
The helper allows synthetic tools with an empty JSON object and denies all other
tools. This proves invocation and accepted allow responses for these MCP calls;
native denial effects, runtime registry drift, full profiles, child inheritance and
the production host bridge remain separate acceptance gates.

Earlier evidence is preserved rather than overwritten:

- mcp-config-revision-live.json proved inventory refresh, but hook logging was empty.
- mcp-config-revision-hooked-live.json proved helper invocation after correcting
  Windows command construction, but hook statuses were failed.
- mcp-config-revision-validated-live.json proves both refresh and successful hooks.

Corrections to prior suspected hook coverage gap:

1. The original MCP probe configured a hook without registering its trusted hash.
   The production NativeHookBridge already performs discovery/trust/reverification.
2. The probe quoted the Python executable path as a PowerShell string expression.
   It now uses the executable basename with its directory prepended to PATH, matching
   production's command strategy. A real PowerShell test covers a spaced helper path.
3. The probe emitted permissionDecision:allow without updatedInput. This engine
   rejects that combination and fails open. Normal allow is an empty JSON object,
   already used by production NativePolicy. The local upstream source test
   permission_decision_allow_without_updated_input_fails_open confirms the contract.
   The child-delegation probe had the same output-format defect and is corrected
   offline; no additional delegation run occurred or is authorized by this change.

Thus empty/failed hook observations are not evidence of an intrinsic MCP hook
bypass. Seven offline tests pass for generated helpers, negative gate cases,
changed schema validation, trust failure, and actual PowerShell command execution.
Scoped Ruff and diff checks pass. All three live processes closed and temporary
homes were removed. The accepted flags in the earlier two files cover inventory
only; the final file explicitly separates inventory_accepted and hook_accepted and
requires both plus cleanup for accepted=true.

Next implementation must manage an MCP-backed host inventory at the supported
reload boundary, maintain execution-time authorization and schema checks, preserve
user Codex configuration, and prove reconnect/disable/install/overlap behavior.
This fixture's temporary config-file update does not yet establish the production
configuration ownership/lifecycle. C8 and full release parity remain open.

Version-pinned source references:
https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/src/tools/handlers/mcp.rs
https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/hooks/src/events/pre_tool_use.rs
