# Native background diagnostic checkpoint

Task: REL-20260914-CODEX-PARITY

The background-command acceptance gate remains open. Neither
`native-background-history-diagnostic-live.json` nor
`native-background-unified-live.json` established a running native command.
Both runs verified hook trust and permitted the exact synthetic command once.
The hook completed, but no commandExecution item or pending native process was
observed. Native history contained only user and assistant messages. The latter
run explicitly enabled unified_exec, matching the installed user's feature
setting; this did not resolve the missing lifecycle. Its temporary rollout scan
found no function_call_output records. That absence does not establish that the
tool succeeded or identify why execution failed.

Diagnostic output contains fixed categories only, never raw tool results,
arguments, messages or error text. Both app-server processes closed and temporary
homes were removed. The real Codex configuration was not changed.

The generated installed protocol also exposes experimentalRawEvents, explicitly
described as internal use only. This checkpoint does not enable that facility or
make production behavior depend on it.

The observed-idle response in these failed probes is not proof that restart is
safe while native background work exists. No automatic restart or MCP inventory
replacement has been enabled. Establish an actual long-lived command and observe
its exit before accepting this route; child-lifecycle coverage remains a separate
open gate and its live rerun remains on hold.

Validation: 39 tests passed across test_codex_runtime.py,
test_codex_app_server.py and test_codex_inventory.py. Scoped Ruff checks passed
for the diagnostic script and runtime tests. This is a diagnostic checkpoint,
not C2/C8 closure or release approval.
