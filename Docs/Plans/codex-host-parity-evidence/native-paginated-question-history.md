# Live paginated native history

Task: REL-20260914-CODEX-PARITY

Ran the bounded async-question/answer probe with --history-pages. It creates the
native thread with historyMode=paginated, captures an explicit synthetic answer
through the shared host RPC handler, reloads it once from ConversationRepository,
and submits it directly to the same native thread as the second turn.

The production codex_history.read function then reads native history through a
probe wrapper that changes only the requested page limit to one. The app-server
returned two pages, each containing one turn. Both expected native turn IDs were
present in the reconstructed history and the original async question/options were
recovered. This establishes live pagination behavior in addition to the synthetic
malformed-page and delivery-recovery tests.

Evidence: native-paginated-question-history.json, accepted=true. Both model turns
completed, the native reply matched, process close succeeded and the temporary
home was removed. No native shell/file action was requested. The pre-existing
command-launch policy blocker was not bypassed.

After the accumulated production fixes, all test_codex*.py modules passed together:
203 tests in 13.16 seconds. Scoped Ruff for the probe and diff checks passed.

This still does not validate installed client automatic answer dispatch, full
restart/fork/delete journeys, GUI/Suite behavior or all release gates. The previously
tested wheel predates later usage/question/recovery fixes and remains separately
identified; source-level results do not update that artifact's provenance.
