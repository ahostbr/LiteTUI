# Native restart hold and local boundary (T0558-B)

## Scope and invariant

A restart hold closes **new work admission**, not the running turn. Preparation
never cancels a turn, answers an approval, launches a provider, claims inbox mail,
or replays queued input. Already-running work may settle through its native
persistence paths. A checkpoint remains blocked until that work and unsupported
transient state are resolved.

This is **not a complete save/restore engine**. The boundary covers only the
explicitly listed LiteTUI-owned local files. Claude/Codex external provider
transcript durability is not proved here: native-provider backends return a
`provider-durability-unsupported:<backend>` blocker. Provider evidence and
coordinator reconciliation remain work for cards C/D. Placement, resource
recovery and coordinator ACK are excluded from the runtime readiness field.
No UI build, new listener, provider replay store, MCP/auth change, host restart,
OS reboot or fleet-wide stop is implemented by this card.

## Existing control path

The existing authenticated owner-local `LocalRpc` endpoint calls the existing
`gui_rpc` dispatcher on the app loop. Four operations are added to its existing
allowlist (the caller still uses the existing authentication and frame envelope):

| Operation | Fields beyond `type`/request `id` | Effect |
| --- | --- | --- |
| `gui.checkpoint.prepare` | `checkpoint_id` UUID; explicit `disposition`: `idle`, `waiting`, or `held` | Close admission before publishing the durable hold. An active turn forces `held`. |
| `gui.checkpoint.status` | none | Read inventory and evidence; no checkpoint file creation or replay. |
| `gui.checkpoint.flush` | matching `checkpoint_id` | Fsync/hash covered files and publish immutable local boundary evidence, only if blockers are absent. |
| `gui.checkpoint.release` | matching `checkpoint_id`; `reconciled: true` | Persist release. Never itself flushes a queue, wakes work, cancels, or recreates services. |

The active `AgentSession` capability supplies seat ownership. A checkpoint cannot
steal another active hold; conversation switching is blocked while held.
Ordinary unowned/archive apps gain no writable checkpoint authority. Releasing a
hold is explicit reconciliation, not permission to bypass the existing native
admission policies. Normal scheduling is eligible again only after release.

GUI reads and results for already-issued callbacks remain available. Unsafe GUI
mutations (including edit/retry before snapshot mutation and async management
operations) are refused at the dispatcher entrance. The direct UI, stream,
queue/hook, native steering/input, scheduler, parent-wake and goal paths also
check the hold. The stream checks again after awaited MCP maintenance. Native
approval response paths can settle already-running work; approval *mail* remains
unread while held and therefore may keep readiness blocked.

### Explicit same-seat compaction

The existing `gui.conversations.compact` operation additionally requires the
matching `checkpoint_id` and `handoff_sha256` when held. The owner verifies and
fsyncs its current native `handoff.md`, then grants one in-memory permission to
the existing compactor. A changed digest, released/corrupt hold, or consumed
permission refuses it. Permissions do not survive process restart. Automatic
compaction and its continuation/wake routes stay gated. This operation does not
invent a compact-success receipt; the coordinator must observe native completion
and request a fresh boundary afterwards.

## Inventory and persistence

| State | Handling |
| --- | --- |
| Current conversation JSONL and existing companions | Fsync/hash through the owned directory; compare authoritative transcript replay with in-memory conversation. |
| Agent settings, `handoff.md`, `memory.md`, `soul.md`, recursive `memories/` | Fsync/hash existing native files. Missing/unreadable required files refuse evidence. |
| Existing task logs associated with this conversation | Fsync/hash after outcomes are settled. No separate task replay ledger. |
| Native incoming Maildir | Do not poll/claim while held. A poll already in flight blocks ACK; any already-claimed input is retained in the existing queue. |
| Draft, images, queued/rejected input | Retain existing state; named blocker, not silently dropped or serialized into a competing queue. |
| Approval/question/callback waiters | Named blocker until existing response and persistence paths settle them. |
| Running turn, workers, tools, child operations, monitor threads, unresolved background outcomes | Block. Reuse native activity inventory, excluding only exact known idle infrastructure worker identities. |
| Hook lifecycle queue, compaction continuation, goal evaluation | Block; do not start new continuation/evaluation while held. |
| Uncertain/pending native input delivery | Block, even without a live worker. No speculative resend. |
| Native Claude/Codex provider durability | Explicit unsupported blocker, separate from local-file coverage. |
| Store/settings/background persistence errors, changed or unreadable evidence | Refuse ACK; preserve admission hold. |

Recursive enumeration rejects links/junctions and unsettled hidden companions.
The existing conversation lease and prior restart-boundary files are not copied
into the boundary. Provider caches outside the owned tree, other conversations,
Maildir contents, global settings/jobs/resources and window placement are not
claimed as saved by this file list.

## Evidence and failure semantics

Agent-home `restart-checkpoint.json` binds checkpoint, agent and conversation
UUIDs, disposition, phase, runtime UUID and optional receipt. The memory gate
closes before its first durable publication. Atomic replacement uses an fsynced
temporary file; directory fsync is additionally used where supported. This is
process-crash evidence, **not a Windows sudden-power-loss guarantee**.

A conversation's immutable
`restart-boundary-<checkpoint>-<runtime>-<revision>.json` names the covered files,
sizes and SHA-256 hashes, with `coverage: litetui-owned-files-only`. The revision
includes the owned launch authority and conversation identity. Existing boundary
files must match the source inventory exactly before retry reuse. The receipt is
the card-A `Receipt(kind='boundary', assurance='native', durable=True)` and is
returned only after both boundary and hold-state publication succeed.

`liteharness.checkpoint_contract` is imported lazily when flushing. A compatible
card-A installation is required for a receipt. Absence is an explicit failure;
ordinary LiteTUI startup, holds, status and release do not require it. There is no
runtime cross-checkout import-path injection or substitute contract definition.

Changes to any covered source invalidate the boundary. Missing, corrupt or
changed hold/boundary records cannot produce `boundary_ready`. Failed release
keeps the memory gate closed. A crash after boundary publication but before hold
ACK leaves no reusable acknowledged boundary; retry must validate and publish
again. After process restart the persisted hold remains, but the former runtime's
ACK is not automatically reused. Idle does not automatically become work, and
interrupted work remains held pending explicit reconciliation.

`readiness` is separately `blocked`, `ready-idle`, or `ready-held`, scoped to owned
runtime only: owner identity, configured backend/model/thinking level, connection,
inbox monitor readiness and local blockers. It is **not** a complete-save ACK.
In particular it does not assert external provider durability or reuse a prior
runtime's boundary. Callers must check `boundary_ready` and its receipt for local
checkpoint evidence, plus the other coordinator-owned readiness predicates.

The four unconfirmed plan recommendations remain card-A manifest decision data.
This implementation assigns no executable defaults to them.

## Validation

Focused tests are `tests/test_restart_checkpoint.py` and
`tests/test_restart_checkpoint_integration.py`. They use synthetic real owned
stores, native entry methods and the authenticated LocalRpc frame handler; no
live seat registry, provider or restart is exercised. Contract-dependent cases
skip if the optional card-A package is absent; admission/failure tests remain
available, and a missing-contract test verifies refusal rather than fake ACK.
Run the focused files together with existing conversation storage, resume, inbox,
parent-wake, native persistence/steering, goal, queue and local-RPC regressions.

Known baseline: `test_codex_steering.py::test_uncertain_delivery_survives_actual_conversation_store_reload`
constructs an unowned `ConversationRepository` then writes it. It fails at the
unchanged `conversation.py` ownership guard, including when the original
70bdc54 steering module is used. Do not weaken ownership to make it pass.
