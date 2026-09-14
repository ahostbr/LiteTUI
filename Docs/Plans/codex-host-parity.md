# Plan: Close Codex host integration gaps

Status: implementation plan; no implementation or agent dispatch authorized by this document.
Date: 2026-09-14.
Source: [integration audit](../codex-host-integration-audit-2026-09-14.md).

## Outcome and scope

Make the official Codex app-server a fully integrated backend in LiteTUI and its
LiteGUI/LiteSuite hosts. Reuse shared tool cards, authorization interfaces, questions,
history, task views and settings wherever their contracts are compatible. Preserve
Codex ownership of inference, credentials, caching, reasoning, native orchestration
and compaction, including Astra Ultra. Do not introduce a second model/tool loop.

Every audit gap has a work item and acceptance test below. Unsupported native
capabilities must remain visible as blockers until implemented or explicitly accepted
by Ryan as a narrower product contract. Relabeling a control is necessary honesty,
but does not by itself satisfy a request for behavioral parity.

Changes already made for tool cards, output cleanup, images, silent-turn interruption,
stream cleanup, message separation and thinking timers are regression prerequisites.
The earlier 41-test result is a baseline, not acceptance evidence for this plan.

## Constraints already established

- Official app-server is required. No return to the custom Responses agent loop.
- Preserve existing per-conversation settings and global defaults.
- Host hooks use the accepted LiteTUI-native contract: existing authorization doors,
  observe and gate, global/project configuration, recoverable hook denials distinct
  from human-denial stops, bounded retries/timeouts, no compaction hook invention.
- Native requests must not be executed a second time by host tools for visibility.
- Display/replay records must never be resent as synthetic model context merely to
  recreate UI. Preserve native thread continuity and cache behavior.
- No tool arguments/results or conversation bodies in operational diagnostics.
  User-visible conversation storage has a separate, explicit persistence contract.
- Preserve dirty work. No broad staging, merges, or unsolicited agent dispatch.

## Phase 0 — Establish protocol and integration contracts

Dependencies: none. This phase gates choices about native controls.

1. Record CLI version, resolved executable, supported model metadata, generated
   experimental schema and source revision. Read current official app-server docs
   and matching CLI source for semantics not established by schema.
2. Build a capability matrix for native shell/file/MCP/agent execution, pre-execution
   hooks, tools-off, individual disables, targeted cancellation, process/session IDs,
   backgrounding, steering, question answers, dynamic inventory updates, history
   replay, compaction events and usage counters.
3. For each capability record: supported endpoint/configuration, lifecycle scope,
   minimum version, experimental status, authoritative evidence and a minimal probe.
   An accepted request is insufficient: verify the resulting behavior.
4. Audit shared TUI/RPC interfaces and both GUI consumers. Define a versioned event
   contract carrying provider/thread/turn/item/call IDs, tool identity, arguments,
   lifecycle status, progress, result and measured duration. Distinguish immutable
   identity from display name. Define duplicate, replay and out-of-order handling.
5. Inventory current dirty changes and capture approved prerequisite patches with
   hashes. Establish a reproducible branch/worktree containing those prerequisites;
   a worktree from clean HEAD alone will omit the current uncommitted adapter.

Acceptance: matrix and event contract reviewed against live probes; unsupported
assumptions are not used to dispatch dependent implementation. If exact controls
require an engine change, present the concrete upstream/minimum-version route and
scope impact to Ryan before choosing a fork or replacing native tools.

## Phase 1 — Execution controls and lifecycle

Dependencies: Phase 0 native capability proof.
Primary files: `codex_app_server.py`, `tool_policy.py`, `hook_host.py`, `app.py`,
`tasks.py`, `codex_tool_ui.py`, plus focused adapter modules as needed.

### C1: Native policy, hooks and tool enablement

- Map host profiles to supported native controls, including shell, files, MCP and
  delegated agents. Propagate restrictions to child execution where supported.
- Enforce blocking hooks before execution using an actual engine enforcement seam.
  An `item/started` notification or after-the-fact interruption is not a gate.
- Route supported native approvals through shared approval UI without duplicate
  prompts. Preserve denial/cancellation distinctions and standing-rule scope.
- Retain `_execute_tool` as the single host-tool authorization door.
- Implement tools-off and individual disable behavior for native tools only where
  enforcement is proven. Do not equate read-only sandboxing with tools disabled.
- Where enforcement is unavailable, show the actual scope and keep exact parity
  blocked. Do not silently weaken hooks or substitute an unofficial agent loop.

Acceptance: attempted host/native shell, file, MCP and delegated actions under each
profile; tools-off; mid-conversation disable; denied hook; failed blocking hook;
human denial; autonomous and scheduled execution. A denied operation has no side
effect, including through child agents. No duplicate dispatch or hook execution.

### C2: Native task visibility, cancellation and shutdown

- Represent native activity in shared task views with provider-owned IDs and real
  status. Distinguish host-owned processes from Codex-owned processes.
- Implement targeted cancel/background operations only through supported lifecycle
  APIs. Never put unrelated or unverified PIDs into `ttyguard.CANCELLABLE`.
- If only turn interruption is available, expose an explicitly labeled Stop Turn
  action. Do not make a per-tool Cancel button silently kill sibling calls.
- Handle quiet turns, waiting approvals/questions, running host tools, connection
  loss, forced cancellation and application exit. Ensure all pending UI/RPC requests
  settle and owned subprocesses are cleaned up.

Acceptance: two simultaneous tools, cancel one where supported; stop whole turn;
stop while no text arrives; stop while a host question is open; disconnect/restart;
no orphan tools or indefinite pending cards. Shared host task behavior stays intact.

## Phase 2 — One event path for live UI, history and RPC

Dependencies: Phase 0 event contract; Phase 1 lifecycle semantics.
Primary files: `codex_app_server.py`, `codex_tool_ui.py`, `widgets.py`, `rpc.py`,
conversation storage/replay code identified in Phase 0; LiteGUI `src/renderer/state.ts`;
LiteSuite's active LiteTUI adapter and native Codex consumers, resolved in Phase 0.

### C3: Persist and replay native activity

- Store display records separately from model-input messages, keyed by native IDs.
  Use a versioned persistence format and explicit migration behavior.
- Reconcile persisted records with resumed thread items; deduplicate by identity.
  Preserve message phase/order so final answers and commentary remain distinguishable.
- Restore cards, results, error states, durations and native plan/compaction history.
  Define unknown duration for incomplete/recovered calls; never invent timing.
- Handle new, resumed, switched, compacted, forked and deleted conversations without
  cross-thread leakage. Exports include the intended visible trace.
- Apply shared sanitization to displayed/stored tool content. Store image references
  or attachments deliberately; do not dump image bytes into card text or diagnostics.

Acceptance: close/reopen mid-turn and after completion, reconnect, replay twice,
compact/resume, conversation switching and export. Same logical trace appears once;
replay executes nothing and sends no extra provider input.

### C4: Progress, plans and compaction display

- Feed command output deltas, MCP progress, plan updates and compaction lifecycle into
  existing widgets through the event contract. Bound output buffers and render rate;
  define truncation visibly without discarding the stored result silently.
- Reconcile streamed output with authoritative completed output without duplication.
- Keep elapsed timers, thinking transitions, scrolling and fold behavior consistent.
  Shared cancellation controls must reflect the capabilities established in C2.

Acceptance: long multiline/ANSI output, progress with no text, failed command, automatic
compaction, wide/narrow/resized terminals, user scroll position and folded cards.

### C5: RPC identity and client parity

- Send stable call IDs for host dynamic tools as well as native tools. Eliminate
  duplicate start/result emissions and settle interrupted calls over RPC.
- Update LiteGUI and affected LiteSuite consumers to pair by identity; retain a
  documented compatibility fallback for older producers without IDs.
- Preserve status, timing, progress, history replay and failure details end-to-end.

Acceptance: same-name concurrent calls finishing out of order; duplicate notifications;
completion before a delayed start; reconnect/replay; old producer compatibility.
Compare TUI and each affected GUI's rendering of the same fixture trace.

## Phase 3 — Interactive turn continuity

Dependencies: Phase 0 protocol proof and Phase 2 identities.
Primary files: `codex_app_server.py`, `hook_host.py`, `ask_user_question.py`,
`app.py`, question RPC contracts and consumers.

### C6: Queued input and steering

- Run queued input through existing admission hooks exactly once before native steering.
- Use expected turn identity when steering. Distinguish admitted, sent and accepted
  state; do not remove an item from durable delivery state until acceptance is known.
- Handle completion racing with steering, reconnect, rejection, images and multiple
  queued messages. Fall back to the next turn without dropping or duplicating input.
- Do not let provider metadata mark unsent queued input as already consumed.

Acceptance: steering during text, native execution and approval wait; turn ends at
send time; server rejects; restart after acceptance; hook denial. Each admitted user
message is delivered once to the intended conversation.

### C7: Structured questions

- Add a structured result seam beneath the existing human-readable host-tool report.
  Preserve legacy `ask_user_question` output for existing callers.
- Map question IDs, selected labels, free text and supported secret-answer handling
  to native response schema. Do not parse presentation text to recover selections.
- Preserve unanswered, partial/chat, submitted and cancelled states distinctly.
  Where native response types cannot express a host state, implement a tested
  cancellation/continuation path rather than presenting it as a final answer.
- Close outstanding questions cleanly on stop, disconnect and shutdown.

Acceptance: multiple questions; selected option plus note; free text; cancel; chat
instead of submit; tools disabled; RPC reply; duplicate/late reply. Silence and
partial selections never become consent or a submitted answer.

## Phase 4 — Inventory, settings and accounting

Dependencies: Phase 0 capabilities; Phase 2 event model.
Primary files: `codex_app_server.py`, `thinking_capabilities.py`, `settings_screen.py`,
`settings.py`, `turn_engine.py`, `turnstats.py`, shared model/usage RPC consumers.

### C8: Dynamic inventory lifecycle

- Track a deterministic digest of active/deferred tools and their schemas.
- Apply supported native inventory updates at their allowed lifecycle boundary.
- If registration is immutable, evaluate supported full-inventory/deferred exposure
  with execution-time authorization, or an explicit thread migration preserving
  history. Prove behavior before choosing; never reset the thread silently.
- Handle tools installed, enabled, disabled or schema-changed mid-conversation and
  after resume. Preserve runtime disable checks regardless of schema exposure.

Acceptance: enable a previously unavailable tool, disable a remembered tool, change
schema, reconnect, deferred search and overlapping names. No stale schema execution
or unintended context/cache reset.

### C9: Backend-aware settings

- Inventory every exposed inference/loop setting: reasoning, sampling, token limits,
  tool iterations, tool profiles, result rewriting, auto-background and compaction.
- Classify each as supported native control, host-only control, engine-owned value
  or unsupported. Wire real controls; disable or explain inapplicable controls.
- Preserve all six Astra modes from actual installed metadata and distinguish global
  defaults from per-conversation values. Do not map Ultra to a raw reasoning value.
- Remove misleading startup/status claims, such as a local iteration cap presented
  as governing a native Codex turn when it does not.

Acceptance: settings changes affect the promised next request/lifecycle boundary;
save/restart/resume; backend switch preserves local settings; no silent no-op control.

### C10: Usage, timing and cache accounting

- Verify native `last` and cumulative usage semantics across multiple model requests,
  turns, resume and compaction using source and controlled probes.
- Separate latest context occupancy, turn aggregate usage, cumulative thread usage,
  cached input and output/reasoning counts. Preserve unknown values as unknown.
- Deduplicate snapshots; handle counter reset/rebase on compaction and reconnect.
- Define elapsed/TPS denominators honestly: tool wait is not model generation time.
  Suppress unsupported precision instead of treating text chunks as tokens.
- Keep native cache ownership. No history reconstruction, per-token instruction
  churn, or arbitrary cache-retention fields added by the host.

Acceptance: multi-tool/multi-request turn, repeated usage event, no usage, reconnect,
compaction and model/effort change. Totals reconcile to native evidence; cache hits
do not reduce context occupancy; cache writes are not fabricated. Live cache probes
demonstrate reuse without claiming a guaranteed hit percentage.

## Phase 5 — Integration, packaging and acceptance

Dependencies: C1–C10 complete or explicitly accepted as blocked/narrowed by Ryan.

1. Run targeted regressions followed by affected host/client suites. Compare ordinary
   local backends to ensure shared changes did not regress them.
2. Run controlled live Codex cases for Low, Medium, High, Extra high, Max and Ultra;
   include native and host tools, an image, steering, approval denial, question,
   compaction, stop and resume. Ultra must retain engine-owned delegation behavior.
3. Build LiteGUI with the verified LiteTUI runtime and record source SHA/patch provenance
   and artifact hashes. Validate the installed application, not only a dev renderer.
4. Validate affected LiteSuite surfaces separately; shared engine usage is not proof
   of shared host UI behavior. Scope unrelated LiteSuite defects separately.
5. Save screenshots/recordings of narrow/wide TUI, GUI concurrent cards, question and
   resumed history, using synthetic non-sensitive content.
6. Produce final coverage matrix with implementation, unit/integration, live and
   packaged evidence columns. Leave any failing or unproven gate visibly open.

## Verification commands

Run from the named repository using its verified environment. Proposed additional
test filenames are created during the relevant work item, not assumed present now.

LiteTUI baseline:

```powershell
python -m pytest tests/test_codex_app_server.py tests/test_codex_tool_ui.py tests/test_oauth_transport.py tests/test_oauth_ui.py tests/test_collapsible_tool_cards.py -q
.venv/Scripts/ruff.exe check src/litetui/codex_app_server.py src/litetui/codex_tool_ui.py tests/test_codex_app_server.py tests/test_codex_tool_ui.py
```

Add focused tests for each C1–C10 contract and run the project's affected checks.
Record existing unrelated lint failures separately; changed code must pass its checks.

LiteGUI:

```powershell
pnpm typecheck
pnpm test
pnpm verify:contracts
pnpm test:integration
pnpm test:desktop
```

Before packaging, inspect current packaging/release scripts and runtime provenance.
Use the existing `package:runtime`, `package:win` and `verify:release` workflow as
applicable; building an artifact does not authorize publishing it.

LiteSuite: `bun run typecheck`, `bun run lint`, affected contract/adapter tests, then
the applicable desktop smoke checks. Record the known full-suite baseline separately
and reverify it before attributing a failure to this work.

Live probes use synthetic tools and inputs, no destructive operations, no real
messages to other people, and no raw payload logging. Verify a native output against
its displayed/exported form; mock tests alone cannot establish native semantics.

## Execution and completion rules

Use a reproducible isolated workspace → write failing contract tests → implement →
debug failures systematically → verify → review → prepare branch completion.
Honor applicable repository instructions and required commit trailers. No merge or
agent delegation is implied by this plan. If additional workflow skills are used,
verify their availability and applicability instead of assuming they are installed.

Suggested sequence: Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5.
C6/C7 and C8/C9/C10 can be independently implemented after their stated dependencies,
but this plan does not launch parallel workers.

Completion means all eleven rows in the audit map to tested outcomes: policy/hooks
and tools-off map to C1; tasks/cancel C2; history C3; progress C4; RPC C5; steering C6;
questions C7; inventory C8; settings C9; accounting C10. Unsupported capabilities
remain blockers unless Ryan explicitly accepts a revised scope. A passing test count
or a truthful disabled-control label alone is not evidence that all gaps are closed.
