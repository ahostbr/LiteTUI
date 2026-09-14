# C3 GUI open response timing

The existing gui.conversations.read response already carries persisted messages
and provider metadata. No second history format is needed for those clients.
The asynchronous open path previously returned its disk snapshot before the native
refresh worker completed. Resume now retains that worker handle; GUI open awaits
it, validates conversation/backend/session identity, then rereads the persisted
conversation through the existing response path. Non-native opens are unchanged.

Validation: 22 GUI-history/history tests pass. The new asynchronous tests prove
that open remains pending until the worker settles, returns the refreshed read,
rejects a switched conversation, and preserves non-native behavior. gui_rpc.py and
new tests pass Ruff; app.py retains 67 existing findings with no additions.

The frontend must still consume/render these native records. LiteGUI source remains
report-only pending scope approval, and Suite consumer work remains within the
existing ownership fence. This is not complete client recovery acceptance.

## C2 next probe authorization

Installed 0.154.0 schema describes collabAgentToolCall.status as operation status,
agentsStates as last-known target-agent status, and receiverThreadIds as child
identities for spawn. Child agent status values include pendingInit, running,
interrupted, completed, errored, shutdown and notFound. Thread status notifications
separately expose active/idle/notLoaded/systemError and approval/input wait flags.
The current parent reader filters notifications from different thread IDs.

Sentinel explicitly authorized one isolated native runtime probe: one Astra parent,
at most one synthetic child, no recursive delegation, text-only work, no project
edits or external communications, exact returned IDs, bounded timeout and finally
cleanup of probe-owned resources only. Check notification/subscription semantics
and targeted child stop with parent continuity. One child cannot prove sibling
isolation. Failed/inconclusive evidence must be reported before another live run.
No implementation-agent spawning was authorized. Other apps may proceed toward
release; LiteTUI-dependent integration/build/publication is last, not a block on
their independent work. Suite and LiteGUI ownership restrictions remain.
