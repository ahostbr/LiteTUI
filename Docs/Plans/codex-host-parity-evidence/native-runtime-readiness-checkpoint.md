# C2/C8 native activity observation and restart readiness

The app-server protocol reader now observes commandExecution lifecycle events and
server-initiated requests before placing them on the existing event queue. This
includes child-thread and late events that the active parent-turn renderer may not
consume. The queue remains intact for existing consumers. Outgoing responses clear
pending server requests only after the write drains. Connection loss marks runtime
state unknown; a new app-server process gets fresh runtime state.

RuntimeActivity stores only native thread/turn/item/process identities and state,
never command text, arguments, stdout, results or host PIDs. A turn completion is
not process-exit proof. Command item completion without an exit code stays pending;
an explicit exit can settle matching PTY identities within the same thread. A
different thread's identical opaque process string cannot settle it. Late replay
cannot resurrect a recorded completed command. Missing identities mark uncertainty.

The read-only restart_readiness helper refuses pending commands, server requests,
disconnection or unknown activity. It enumerates all loaded native threads with
pagination, verifies exact IDs and idle status, and refuses an activity-revision
change during the check. It does not interrupt, restart, kill or infer ownership
from a process ID. A future caller must also recheck its returned revision at the
actual replacement boundary and serialize replacement with other host operations.

Validation: 38 tests pass across native runtime, app-server and inventory. New tests
exercise the actual protocol reader, late child activity, request settlement,
pagination, active child, repeated cursor, mismatched thread, race and connection
loss. Scoped Ruff and diff checks pass. These are offline tests with no inference.

This observer/check is wired into the protocol reader but does not yet authorize
automatic engine replacement. Real native background-command event semantics and
child/background completeness must be proved before C8 refresh consumes its green
result. Native task UI, targeted controls and full C2 acceptance remain open.
