# C3/C4 proposed plans and reader state

The locally generated app-server schema at artifacts/history-schema defines
PlanThreadItem as id/text/type and explicitly makes completed text authoritative
over concatenated PlanDeltaNotification deltas. These are proposed plans, distinct
from turn/plan/updated checklist events. The adapter previously skipped plan items
and did not route item/plan/delta.

Proposed plans now use separate shared FoldBlock cards and native item identities.
Streaming text updates the card and versioned plan_update RPC event; completion
replaces draft text. Duplicate completions, late deltas and replayed starts cannot
erase the completed/current text. Turn termination saves interrupted state and
publishes it. Text uses shared sanitization, including after delta concatenation.
Recovered plans retain their native order and partial/completed state and reconcile
idempotently. Checklist records remain distinct.

Replayed plan cards now carry native trace identity. Reader-state capture/restore
uses FoldBlock (including ToolMessage) so mixed expanded/collapsed plan cards and
tool cards retain their states while history refresh preserves scroll position.

Validation: 51 tests pass across proposed plans, history, app-server, tool UI and
compaction UI. This includes actual Textual cards, mixed fold state and scroll,
fake-server transport notification routing, saved authoritative text, interrupted
state, sanitization and recovery ordering. Scoped Ruff passes.

One initial transport-test invocation omitted stream=True and consequently took
the isolated title/evaluator route, issuing one real synthetic model call. It
finished through the normal finally-close path and provided no plan acceptance
evidence. The corrected fixture explicitly uses streaming and forbids a real
AppServer.start; the passing tests above are offline. No delegation probe was run.

This checkpoint does not prove native live plan production, GUI/Suite rendering,
full legacy migration or packaged-client acceptance. Those gates remain open.
