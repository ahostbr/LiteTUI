# Delivery recovery scope across asynchronous history reads

Task: REL-20260914-CODEX-PARITY

Queue recovery previously checked conversation ownership before awaiting native
history, then allowed SteeringLedger.reconcile to mutate and save state before
accept_steered checked ownership again. A conversation switch during the read
could therefore reach a stale save callback against the new conversation.

recover_queue_head now captures the conversation object, conversation ID, backend
object and queue head before reading history and verifies all four before ledger
mutation. A stale result returns false with the input state retained.
reconcile_messages likewise verifies conversation/backend scope after each history
read before changing message metadata or marking it accepted.

Five new asynchronous boundary cases cover replacement of the conversation list,
conversation ID, backend and queue head, plus the materialized-message path. They
check that no stale save or acceptance mutation occurs. Forty-nine steering,
async-question and transport tests pass; scoped Ruff and diff checks pass.

This is a source-level recovery fix. It does not close live installed-client
switch/restart acceptance, native launch-policy blocking, or full-plan release gates.
