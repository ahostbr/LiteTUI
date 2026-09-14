# C3 history and compaction identity checks

Manual compaction now validates the thread ID returned by thread/resume before
adopting the native process/thread or requesting thread/compact/start. A mismatched
response raises the same explicit provider error as normal turn resume. The test
proves no compaction request, transcript mutation, RPC event or transport identity
adoption occurs on mismatch.

Read-only history refresh now skips later Codex metadata records that omit native
thread identity, and tolerates null provider_metadata. Previously such a record
could hide an earlier valid thread ID and silently suppress history recovery.
Existing switch/append/backend/active-chat race cases now include both forms of
legacy tail metadata and still discard stale read results without mutation.

Validation: 43 tests pass across compaction UI, history, paginated history, GUI
history RPC and app-server transport. Scoped Ruff passes. All tests use synthetic
servers or local rendering; no native model call was made for this checkpoint.

Full fork/delete journeys, legacy turn migration and packaged all-client history
acceptance remain open. This is an identity-check checkpoint, not full C3 closure.
