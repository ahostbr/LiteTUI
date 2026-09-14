# C8 loaded-thread configuration does not establish live inventory refresh

mcp-thread-config-live.json records the bounded in-place alternative test. Initial
thread/start configuration registers the synthetic MCP server; thread-scoped
status confirms alpha exists, and turn one executes it successfully with a completed
PreToolUse hook. Between turns, the fixture changes to version 2 and thread/resume
receives a new MCP configuration revision for the same already-loaded thread.

The response preserves thread identity, the app-server process is unchanged and the
configuration file is unchanged. Nevertheless turn two executes neither beta nor
updated alpha. tools/list observes [1,1], never version 2. Both turns finish normally,
but inventory acceptance is false. The process closes and temporary home is removed.
This is not evidence that thread/resume applies MCP updates to a loaded thread.

The session-restart route remains the verified configuration-preserving mechanism.
Its production adoption needs an active-work boundary: thread idle status alone
does not yet prove absence of background PTYs. The generated commandExecution item
schema has a processId, but process/exited is explicitly for client-owned
process/spawn handles, so those two identity domains must not be conflated.
No host PID kill or speculative native restart logic was added.

The probe's new thread-config mode remains available to reproduce this negative
result. Existing fixture tests pass and scoped Ruff passes. No delegation or
cross-client launches were performed. C8 full host integration remains open.
