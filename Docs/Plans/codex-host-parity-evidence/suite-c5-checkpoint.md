# C5 Suite consumer checkpoint

Task: REL-20260914-C5-SUITE (companion to REL-20260914-CODEX-PARITY).
Candidate: `C:/Projects/.worktrees/release-20260914-suite`, branch
`codex/release-20260914-suite`, verified base `6885d57c3c73bba1d629d7e65425255cc805feff`.
No Suite commit, merge, push, release, real provider launch or deployed-app action.

## Changed ownership fence

- `apps/server/src/provider/Layers/LiteTuiAdapter.ts`
- New adjacent `LiteTuiToolEvents.ts`, `LiteTuiToolEvents.test.ts`, `LiteTuiToolEvents.fixture.json`
- `apps/server/src/orchestration/Layers/ProviderRuntimeIngestion.ts` (one status passthrough)
- `apps/web/src/session-logic.ts`
- `apps/web/src/litesuite/components/panels/frontier-chat/toolRows.ts`, `toolRows.test.ts`
- Same directory `ToolCallRow.tsx`, `ToolCallRow.browser.tsx`

Sentinel approved the adapter/helper fence first, then explicitly approved ingestion,
worklog, actual Frontier row projection and unknown-duration renderer extensions.
All unrelated candidate dirty files were preserved.

## Behavior

Native v1 events retain provider/thread/turn/item identity, arguments, replacement
progress, terminal status and authoritative duration. Host turn ownership is remembered
for late native items; tool events do not invent Date.now-based turn IDs. Old ID-less
calls match only when unambiguous. Failed/interrupted statuses reach ingestion and
cards. Explicit unknown duration is not replaced with receipt timing. Historical
producers with no duration field retain existing paired timing.

Frontier rows and worklog grouping use identity and source/turn boundaries rather
than identical tool names. Replayed events do not create new cards or reopen finished
ones. Progress appears as output, preserving arguments. Start payloads reach interactive
widget eligibility before completion. Abrupt child exit/error settles active cards once,
with last output, unknown duration and explicit host interruption provenance.

The existing status-dot/collapse design is retained. Terminal unknown timing renders
an accessible neutral em dash; running rows keep their pulse and no duration cell.
Native interrupted remains a terminal failure-colored dot with exact status retained in
the data. Native late starts suppressed by the producer cannot reconstruct unavailable
arguments; replayed activity starts can enrich card arguments without reopening them.

## Validation

- 19 server tests passed: `LiteTuiToolEvents.test.ts` + `adapterToolCompletions.test.ts`.
- 61 web tests passed: `toolRows.test.ts` + `session-logic.test.ts` (including legacy tests).
- 8 isolated Chromium tests passed: `ToolCallRow.browser.tsx`, running/known success,
  terminal unknown/interrupted, collapse/expand and established visual assertions.
- `bun run typecheck` passed from both apps/server and apps/web. Server emitted three
  existing advisory Effect messages in CheckpointStore/wsServer tests, not errors.
- Scoped formatting and diff checks pass after preserving LF in the one-line ingestion edit.

The shared synthetic fixture identifies LiteTUI tool-event v1 / installed Codex 0.154.0.
Server tests compare actual mapper -> schema decode -> ingestion output against it.
Web tests feed the same fixture to real Frontier/worklog/rich-card extractors. This
preserves the server/web TypeScript project boundary while checking the joining contract.

No existing LiteTuiAdapter.test.ts execution: it can launch real child sessions at import.
No packaging acceptance, real Suite-hosted provider smoke or LiteGUI acceptance is
claimed. Full plan and release hold remain open. Remaining broader gaps include native
history reconciliation, task lifecycle, inventory updates, async questions, complete
steering recovery, six-mode runtime coverage and packaged helper validation.

## Review repair: late start metadata

Sentinel reproduced completion-first followed by a real late start carrying args.
The initial mapper discarded that data; this was a defect, not a producer limitation.
The mapper now emits an idempotent metadata-only item update for newly available,
nonconflicting top-level argument keys and missing tool name. It retains terminal
status and identity and never emits late output/progress as a result. It stores key
provenance rather than duplicating large completed outputs in memory.

Frontier and worklog projections merge that metadata into the terminal card while
preserving final output, status and measured timing. The shared fixture now includes
nameless completion -> late Bash start with command -> replay. Server tests verify
actual schema/ingestion projection; web tests verify both grouping paths; an isolated
Chromium test consumes the same fixture as raw JSON and checks one completed card,
late command text, original 10ms duration, no running pulse and original expanded output.

Revised focused results: 20 server tests, 62 web logic tests, 9 Chromium tests pass.
Server typecheck passed after the repair; final web typecheck status is recorded in
harness followup once its process exits. Scope and no-commit/release gates unchanged.
