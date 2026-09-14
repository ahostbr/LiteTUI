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
