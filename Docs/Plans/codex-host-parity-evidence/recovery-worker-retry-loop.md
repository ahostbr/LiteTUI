# Avoid self-triggered native delivery recovery

Task: REL-20260914-CODEX-PARITY

The pending-input recovery worker used the chat group. Completion of any chat
worker schedules another queue flush, so a retained unconfirmed delivery could
immediately create another recovery worker indefinitely.

Recovery now uses a separate codex-recovery group and an in-flight guard. Only
successful recovery explicitly schedules the next queue flush. Expected provider,
I/O or timeout failures retain the input and produce a fixed status message without
private error details. Conversation/backend scope is checked before recovery starts
and before publishing the result; a switched conversation receives no stale notice.

Five worker-boundary cases verify one attempt for duplicate flush calls, no automatic
rescheduling for unconfirmed/failed recovery, one continuation on success, and no
stale notices before/during a switch. Thirty-eight recovery/steering/shared-queue
tests pass. The new test passes Ruff; app.py retains its 67 baseline findings with
no additions. This does not replace live installed-client recovery acceptance.
