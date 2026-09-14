# C3 native resume reconciliation checkpoint

Installed app-server schema was regenerated locally with
`codex app-server generate-json-schema --out artifacts/history-schema`.
ThreadResumeResponse defines Thread.turns as populated on resume, rollback, fork
and includeTurns reads. The transport now consumes those returned native items
before starting the next turn and rejects a mismatched resumed thread identity.

New accepted turns persist app_server_turn_id before their first display item.
Reconciliation uses that identity or existing trace turn IDs, scoped to the exact
native thread. It repairs known terminal items and inserts missing native items
in their native order. The existing tool mapper writes into a non-executing sink:
no tool invocation, widget mounting, live RPC event, or model input is generated.
Assistant phases are retained. Missing native timing remains unknown; matching
previous terminal timing can be retained. In-progress items are not fabricated
as completed, even if a surrounding turn says completed. Missing items in partial
history are preserved. Unknown trace versions are left untouched.

Changes persist through the existing conversation edit seam. Save failure restores
the in-memory trace and propagates instead of starting the next turn. Repeating
the same reconciliation does not create another save or duplicate items.

Validation: 30 tests pass across test_codex_history.py, test_codex_app_server.py,
test_codex_tool_ui.py and test_codex_compaction_ui.py. This includes actual
AppServerTransport resume with a fake server: recovered history remains display
metadata and turn/start contains only the new user input. Scoped Ruff and
git diff checks pass. No inference or live app-server sessions were started.

Remaining C3 gates: immediate visible refresh on reopening (current repair runs
on transport resume), authoritative RPC history delivery, legacy turns lacking
both saved turn IDs and display records, interrupted-turn partial text recovery,
cross-turn local plan ordering, fork/delete/export and live disk restart journeys.
This checkpoint is not full C3 or all-client release acceptance.
