# Phase 0 execution checkpoint

Task: REL-20260914-CODEX-PARITY.
Branch: feat/codex-host-parity.
Candidate: C:/Projects/.worktrees/codex-host-parity.
Base: 8eeb5459918c19a09ee0f5afd87ce10634435d48, verified before branching.

Sentinel acknowledged the LiteTUI release and integration hold on 2026-09-14.
No LiteGUI changes, push, merge or release are authorized in this execution scope.
Other app releases may proceed independently. Root edits remain untouched by the
candidate reconciliation. `snapshot.json` records the 30 prerequisite files copied
and SHA-256 checked; these include mixed edits and do not imply sole authorship.

## Capability findings

| Capability | Evidence established | Remaining proof |
| --- | --- | --- |
| Installed engine | `codex-cli 0.154.0`; existing transport starts successfully | Record resolved executable hash alongside final probe runs |
| Native hooks | Matching official config schema defines PreToolUse/PostToolUse; matching core source exposes hook identities for shell, patches and agent spawning | Probe blocking semantics, configuration precedence and inherited child scope before wiring host policy |
| Hook enumeration | Live app-server `hooks/list` accepted; only response field names stored in protocol-smoke.json | Listing is not proof of execution enforcement |
| Native targeted cancel | CommandExecTerminateParams limits processId to client-supplied command/exec sessions | Do not use this endpoint with agent item IDs; discover a separate supported capability or retain explicit blocker |
| Steering | TurnSteerParams requires expectedTurnId and supports user input plus clientUserMessageId | Prove completion races, acceptance persistence and duplicate behavior |
| Native tool inventory | dynamicTools found on thread/start; absent from inspected turn/start and thread/resume schema | Inspect remaining update endpoints and source before selecting lifecycle strategy |
| Model catalog | Live `model/list` accepted | Consume actual capability metadata, not hardcoded feature assumptions |
| Questions | Current shared tool reports text; native API expects keyed structured answers | Add structured host seam and cancellation mapping |
| Display/history/usage | Existing audit identifies missing mappings | Define and test independent display records and snapshot accounting before connecting replay |

Sources: installed generated v2 schemas at C:/Projects/codex-appserver-0154-schema;
[official app-server documentation](https://developers.openai.com/codex/app-server);
[matching configuration schema](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/config.schema.json);
[matching hook tests](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/tests/suite/hooks.rs).
Downloaded matching source is retained beside this checkpoint for reproducibility.
No model prompt, credentials, hook definitions or tool results are stored by the
read-only protocol smoke result.

## Reconciliation started

Reviewed Sentinel's 039faf4/de8f0d3/4b0cfd7 stack instead of cherry-picking over the
adapter. Reused subagent plugin/schema and associated test behavior from the final
stack; integrated explicit child effort validation in the candidate's existing
complete_sidecall while preserving its official app-server route and cleanup.
Updated the old OAuth test double for the newer prompt_cache_key instance field.
The standard-tier tests apply to the legacy wire path; native app-server service-tier
inheritance still needs separate verification and is not claimed fixed by them.

Combined validation: 50 tests passed across child effort, standard-tier legacy wire,
app-server transport, Codex cards, OAuth transport/UI and shared collapsible cards.
Phase 0 remains in progress; native policy enforcement and release acceptance are
not cleared by this result.

## Proposed event contract (implementation pending)

- Identity: provider + threadId + turnId + itemId; dynamic callId retained separately
  if protocol identity differs. Never match completion solely by display name.
- Lifecycle: started, progress, completed, failed, interrupted; carry authoritative
  native status and distinguish unknown/recovered state.
- Content: tool name/namespace, structured arguments, text/image result references,
  progress kind and bounded display text. Operational logs receive only enumerated
  event metadata, IDs and numeric measurements.
- Timing: observed monotonic start for live cards; provider duration when supplied;
  unknown after replay when neither exists. No invented zero-duration success.
- Replay: persist provider display events outside model-input messages. Deduplicate
  by identity and event/snapshot semantics, reconcile authoritative completion, never
  dispatch a tool or submit a synthetic provider turn while restoring display.
- RPC: version the additive fields, carry IDs on host and native calls, define a
  legacy fallback only when IDs are absent. Consumers must handle duplicate and
  out-of-order events, including terminal events after reconnect.

Next: prove native pre-tool denial on a synthetic tool/action, then finalize C1's
host-hook bridge contract; validate steering/inventory boundaries independently.

## Continuation checkpoint

- Native shell enforcement is now live-proven in `native-hook-startup-probe.json`:
  `blocked_before_command=true`, hook tool identity `Bash`, completed hook status
  `blocked`, and no native command item. Enabling hooks alone was insufficient.
- Correction after further probing: session trust works with a whole-table
  `hooks.state={...}` override. Dotted override syntax misparsed the hook identity
  containing `config.toml` and Windows backslashes. The successful probe now uses
  session-only trust; no normal user configuration writes are needed. Its private
  temporary CODEX_HOME and copied login state are removed on exit.
- Quoted executable-first hook commands failed under the session shell; the
  successful synthetic probe invoked `python` with the quoted script path. Product
  helper launch must explicitly control runtime discovery and test paths with spaces.
- C7 implementation started: task-local structured capture preserves the shared
  authorization/tool door and legacy question text; native replies now carry actual
  selected labels/free text keyed by question ID. Chat/cancel does not submit partial
  selections. Free-text-only and secret note fields are supported; single-select is
  enforced by the TUI and RPC answer adapter. Native empty/cancelled answers stop
  the turn. Desktop/client cancellation and secret rendering still require full
  end-to-end acceptance; C7 is not marked fully complete.
- Combined current checks: 56 passed; scoped new adapter/probe/question-result lint
  passes. No active probe process remains after the successful live run.
- Next implementation: a scoped native hook-to-host policy bridge, including trusted
  registration lifecycle, failure behavior, exact tool identity and child scope.
  C1 remains incomplete; other C2–C10 gates remain open as previously recorded.

## Native policy and display checkpoint

- Session hook discovery preserves source-layer user hooks and trusts only the two
  host helper definitions. The helper uses an authenticated loopback connection.
  Missing or failed policy replies fail closed. Live allow/disabled/tools-off
  checks are recorded in `native-policy-probe.json`.
- Native pre-tool requests pass through shared authorization and lifecycle hooks.
  PostToolUse alone has no reliable success status; tool_after waits for native
  item completion and uses its exit/status fields, with the original host context
  and policy profile. Outstanding items settle as cancelled, never success.
- Bridge approval waits run in Textual workers. Tests cover a real modal response,
  stop, peer disconnect, bridge close and policy exceptions. Delegated child hook
  scope, full native cancellation and packaged helper discovery remain open.
- Native command/MCP progress, plans and compactions use shared widgets. Display
  traces persist separately in provider metadata and replay without tool dispatch;
  duplicate IDs are suppressed and missing durations remain unknown. Authoritative
  native history reconciliation, output throttling and RPC consumer parity remain
  open. This is a checkpoint, not C1–C10 or release acceptance.

## Settings and usage checkpoint

- `codex_settings.py` classifies the exposed generation/loop controls. Engine-owned
  or unsupported local controls are disabled with an explanation; their saved
  values survive. Host-tool output/subagent/background controls explain their
  limited scope. The Codex startup banner no longer claims a local iteration cap.
  Mounted settings tests cover Codex and LM Studio, including save preservation.
- `codex_usage.py` separates latest request, native cumulative and per-turn delta
  snapshots. Duplicate snapshots are ignored. Context uses the latest request's
  total, including cached input. Missing fields, missing resume baselines and
  detected counter rebases retain unknown aggregates. Manual compaction replaces
  or invalidates the next-turn baseline; automatic compaction invalidates the
  current turn's aggregate pending stronger response-level accounting.
- Native stream TPS and local prompt-evaluation ETA are suppressed because no
  measured native generation-only denominator is exposed by this adapter.
- `usage-snapshots.json` captures only numeric counters from two live synthetic
  host-tool turns, low then medium. Each turn produced two native request snapshots.
  First-turn context ended at 28,801 while aggregate usage was 57,571; second-turn
  context ended at 35,390 while aggregate usage was 70,749. Cumulative totals
  reconcile across both turns. Cached input was 28,544 then 35,200 for those turns.
  These observations demonstrate reuse, not a promised hit rate.
- Source: [matching native TokenUsageInfo implementation](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/protocol/src/protocol.rs)
  accumulates completed responses into total and replaces last; its context/error
  paths can rebase counters. Live compaction/resume accounting, full six-mode
  acceptance, all client settings surfaces and packaging remain open.
- Validation: 66 focused tests pass; another 22 usage/cache/ETA tests pass. The
  broader OAuth/footer selection has 32 passes and six `test_footer_fields.py`
  failures. Loading committed `ce5ed3c` app.py reproduced those same six failures:
  the tests expect all footer fields at the default narrow test width, while the
  current responsive footer omits them. This baseline remains to reconcile with
  explicit wide/narrow acceptance; it is not a passing full-suite claim.

## Steering capability and delivery-state checkpoint

- `steering-probe.json` verifies live `turn/steer` during a synthetic host-tool
  wait: the response identifies the expected active turn, the resulting answer
  reflects the steering, and `thread/read(includeTurns=true)` contains exactly
  one `userMessage.clientId` matching `clientUserMessageId`. Steering after the
  turn completes is explicitly rejected. No prompt, output or credentials appear
  in the saved evidence.
- Matching [official steering tests](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/app-server/tests/suite/v2/turn_steer.rs)
  confirm the expected-turn precondition and invalid-request rejection code
  `-32600`. The host now preserves JSON-RPC rejection codes in a dedicated error
  type; connection loss and timeouts remain distinguishable from rejection.
- `codex_steering.py` adds a delivery ledger with save-before-effect transitions:
  queued, admitting, admitted, sending, accepted, denied, next_turn and uncertain.
  Admission and send are not automatically repeated after interruption. Explicit
  validation rejection permits next-turn fallback; uncertain submission requires
  positive native-history reconciliation. Missing history is not treated as proof
  of non-delivery, including partial or rolled-back histories.
- Tests cover image input, stable identity, one admission/send, explicit rejection,
  internal errors, lost replies, interrupted admission/send and restored-ledger
  reconciliation. Host queue integration, durable resume UI, and automatic safe
  fallback are the next work; C6 remains open.

## Host queue integration checkpoint

- Active Codex turns now start a separate host steering worker. It can run while
  the stream consumer awaits a host tool or question. It invokes the shared prompt
  admission hook before native steering, saves each delivery transition, and
  removes queued input only on confirmed acceptance.
- Explicit validation rejection retains the admitted message for the next turn.
  The fallback carries the same client identity and skips duplicate admission.
  Lost replies and cancelled sends remain held. Resume reconstructs queued entries
  from private conversation metadata; native history can positively confirm them
  without resending. Uncertain fallback turn/start delivery also requires matching
  native thread and client identity before it is treated as accepted.
- Local user records carry an internal delivery marker. It is stripped by the
  local OpenAI transport. Conversation guards prevent native queued input from
  being silently sent into a different conversation or local backend. The queued
  bubble changes to delivered only after native acceptance, including fallback.
- The repository's persistence-error state now gates queued sends. A failed or
  unavailable journal write cannot be followed by native steering. A disk-backed
  ConversationRepository test reloads an uncertain delivery and verifies its
  content, identity, admission and state survive.
- `host-queue-steering-probe.json` verifies the integrated queue path against the
  installed app-server while a synthetic host tool waits. Steering affected the
  answer and produced one matching persisted native user-message ID.
- Validation: 75 queue/hook/adapter/question/usage checks passed, followed by the
  added disk-reload test in a 13-test steering run. Full interactive approval-wait,
  abrupt process restart, multiple queued images and rendered resume acceptance
  still require validation; C6 and the overall release gate remain open.

## Tool event ownership and compaction checkpoint

- Codex's item stream now owns RPC lifecycles for both native and host dynamic
  tools. Task-local suppression prevents the host dispatcher emitting a duplicate
  start. Starts/results/progress carry version, provider, thread, turn and item
  identity. Interrupted tools settle over RPC. Completion-first and duplicate/late
  notifications do not create extra cards or fabricated durations.
- Tool results carry equal `result` and `text` fields for current LiteGUI/LiteSuite
  compatibility. Native question callbacks no longer emit orphan legacy tool starts;
  their dedicated shared question events remain active.
- Manual compaction now uses the shared card, elapsed clock, saved display record,
  native context snapshot and stop handling. Automatic/manual RPC ends carry the
  outcome/will_resume fields existing consumers expect. Interrupted compactions
  settle in RPC mode even when no Textual card exists.
- Live `tool-events-probe.json`: two starts and two results, unique complete scoped
  identities, matching aliases and measured durations. Live
  `compaction-events-probe.json`: one matched manual compaction pair, success, no
  automatic continuation, displayed conversation preserved. 51 focused tests pass.
- Contract: `Docs/codex-tool-event-contract.md`. Consumer parity remains open.
  Sentinel authorized REL-20260914-C5-SUITE in the existing Suite release worktree,
  limited to LiteTuiAdapter and focused tests/helpers, with no commits or runtime
  launches there. LiteGUI stays excluded; a separate proposed diff/report against
  its dirty baseline is required for Sentinel's consolidated user scope decision.

## Suite C5 consumer checkpoint (2026-09-14)

Authorized companion work implemented without commits in release-20260914-suite.
See `suite-c5-checkpoint.md` for exact fence and validation: 19 server + 61 web logic
+ 8 isolated browser tests, server/web typechecks. Producer/schema/projection/card
identity, arguments, progress, status and timing are connected by a shared synthetic
fixture. Full release/client/packaged acceptance stays open. LiteGUI report/draft
patch sent to Sentinel; proposal remains unapplied pending consolidated user scope.

## C5 review repair and C7 question lifetime checkpoint

C5 Suite review found late start args were dropped after completion-first. Repaired
with idempotent metadata-only updates and terminal-safe merges in both projections.
Revised evidence: 20 server, 62 web logic, 9 browser tests; both typechecks pass.
Independent Sentinel re-review requested; no Suite commits or release action.

C7 server-request questions now run independently of the native reader; turn-owned
cancellation releases RPC threads and real modal/sidebar widgets. 43 focused tests
pass, including actual-reader stop/disconnect. See `question-lifecycle-checkpoint.md`.
Async agentMessage questions and full all-client/live acceptance remain open.
