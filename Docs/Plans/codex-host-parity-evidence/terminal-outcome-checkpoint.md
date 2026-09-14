# Native terminal outcome preservation

The C3 recovery audit found that CodexToolUI flattened explicit interrupted,
cancelled and declined item results to failed in both stored trace records and
RPC tool_result events. Those outcomes now retain their native spelling. Actual
failure signals (nonzero exit, success=false, error) still produce failed when
the item does not specify a more precise terminal outcome.

Regression tests first reproduced all three flattened outcomes, then passed
after the fix. They exercise the production item mapper, duplicate completion,
finish cleanup, saved trace state and RPC payload, retaining reported zero
duration. The focused tool UI, compaction UI and app-server suite passes 24 tests;
scoped Ruff and git diff checks pass. No live provider calls were made.

C3 remains open: transport resume currently loads a native thread but does not
reconcile its authoritative items with saved display_trace. Local replay alone
does not establish reconnect/mid-turn recovery or complete export parity. The
next implementation must reconcile by thread/turn/item identity and preserve
message ordering without resubmitting display records as model input.
