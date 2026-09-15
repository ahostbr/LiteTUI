# Paginated delivery recovery

Task: REL-20260914-CODEX-PARITY

recover_queue_head and reconcile_messages still requested inline thread history
directly. For native paginated threads this could omit the accepted client-message
identity and leave a recovered answer indefinitely unconfirmed.

Both paths now use codex_history.read, which validates thread identity and follows
the persisted history mode through all full-item pages. Recovery still requires
positive client-ID evidence; a malformed page leaves queued/materialized input
unconfirmed and does not resend it.

Four integration cases exercise both paths with acceptance on the second page and
with a malformed second page. They assert exactly the read-only requests, queue
retention versus one-time materialization, and delivery state. Existing legacy and
wrong-thread cases remain covered. Thirty-eight steering/history-page/async-question
tests pass; scoped Ruff passes after import formatting. This is synthetic protocol
coverage, not a live multi-page history capture or full client auto-dispatch proof.
