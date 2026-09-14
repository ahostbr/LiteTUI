# Codex tool event contract v1

Producer: `codex_tool_ui.py`, official native item lifecycle. Applies to native
tools and LiteTUI dynamic tools called by Codex. The shared host authorization
door still executes dynamic tools; its legacy start emission is suppressed only
in that task-local native scope. Questions keep their separate question UI events.

Every native tool start/progress/result carries `eventVersion: 1`, `provider:
codex`, `threadId`, `turnId` and `id`. Match by all four identity fields. `id` is
the native item ID, not the JSON-RPC request envelope ID or the display tool name.

| Type | Payload |
| --- | --- |
| `tool_call` | `name`, `args`, `status: running` |
| `tool_progress` | `name`, `text` (bounded replacement snapshot, not append delta) |
| `tool_result` | `name`, `result`, equal `text` compatibility alias, `ok`, `status`, `durationMs` |

Result status is completed, failed or interrupted. `durationMs: null` means no
measured start or provider duration was available. A completion received first
creates one start/result pair; later starts and duplicate completion notifications
do not create another card. Stream cleanup settles every outstanding tool once.
Progress is bounded and coalesced, and cannot replace a completed result.

Compaction uses `compaction_start` / `compaction_end` with stable identity,
`automatic`, terminal `outcome: success|cancelled|error`, `will_resume`, and known
duration. Manual compaction has a client operation item ID `compact:<native turn>`;
its identity is stable across both events. Its trace is persisted separately from
model input. Automatic compaction uses its native item ID.

Consumer acceptance must prove same-name out-of-order calls, duplicate terminal
events, late starts, completion-first, interruptions, progress replacement and
history/reconnect. Preserve arguments, text, status and measured duration through
the downstream schema and renderer, not just the adapter's emitted object.

Legacy producers may omit IDs and version. Their fallback must be separate from
v1 matching and documented; never match a v1 result solely by tool name. The
producer retains both `result` (LiteGUI) and `text` (existing LiteSuite adapter)
until consumers support the canonical payload consistently.

Evidence: `Docs/Plans/codex-host-parity-evidence/tool-events-probe.json` and
`compaction-events-probe.json` are live synthetic checks with numeric/boolean
results only. They establish producer behavior, not downstream client acceptance.
