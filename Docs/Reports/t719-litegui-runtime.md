# T719 — LiteGUI runtime interfaces and shared ownership

Owner: RustAxis, worker, `01a096ad-0296-7c82-94a9-4f596dfb57b2`. Branch: `feat/t719-litegui-runtime`. Worktree: `C:/Projects/LiteTUI/.worktrees/feat-t719-litegui-runtime`. Base: `9735a96`. Sentinel owns review and merge; this report does not claim a release or Ryan's real-backend acceptance.

## Ryan's implementation intent

The approved LiteGUI v1 plan says: “Extend the existing JSONL RPC implementation”, “Preserve existing clients and commands; add explicit capabilities and structured management operations”, and “The renderer never writes conversation, settings, hook or scheduler files directly.” It also requires “Cross-process exclusive ownership for an active conversation”, “A scheduler execution lease so shared jobs cannot fire twice”, “Atomic, coordinated updates to shared configuration”, and “An explicit invalid selection produces an actionable error rather than silently choosing another binary.” The backend retains the plan's existing `litesuite | litetui` router owner values and model-load confirmation requirements.

This work adds a structured adapter over LiteTUI's current runtime. The GUI application, its visual design, Electron packaging, public update feed and website registration belong to the other workstreams. It does not import Frontier stores or replace LiteTUI's agent loop.

## Public contract

`gui_rpc.OPERATIONS` is the executable operation inventory. `gui.hello` requires integer protocol version 1 and returns the data version, supported operation names and capabilities. Requests use the existing `{type: 'response', id, ok, result, error}` envelope. `id` is a correlation ID; `ask_id`, `approval_id` and management `request_id` remain target IDs. Invalid versions, settings and expired targets fail visibly.

`gui.state` returns authoritative persisted session navigation, current conversation, effective runtime mode/model/profile, settings metadata with requested/effective/deferred values, jobs/tasks, pending approval/question/model decisions, usage and owned active work. Host shutdown waits for `active_work`; `busy` covers chat and blocking management, allowing independent background work. `gui.work.cancel` pauses scheduling and requests cancellation only for this instance's work. It reports work that cannot yet settle instead of claiming it stopped.

Conversation operations use `ConversationRepository` and existing submission/compaction paths. Model operations use existing backend discovery, load, unload, setters, installed flag discovery and the T690 backend VRAM gate. Skills, MCP, hooks, tools, jobs, goals, calendar, memory, system prompt, images, mark and Glassbox operations return structured data. Runtime prerequisites remain visible backend errors; no model weights or model service are installed here.

Trusted host tools register as plugin-owned tools with the conservative unknown-MCP policy. Only the existing `_execute_tool` authorization door can reach their runner. The runner emits `host_tool_requested` with request, session and operation IDs; `gui.host_tools.result` completes that exact call. Unregister refuses active turns/management and pending calls, including a tool awaiting approval before its runner starts. Existing legacy events, including `turn_end`, retain their original shape. Negotiated clients additionally receive `gui.event` wrappers carrying session and operation identity.

## Shared data and process lifecycle

The selected `LITETUI_DATA_ROOT` holds settings, conversation history/memory, hooks, scheduler state, skills cache, MCP configuration, llama presets/logs, suspended-seat breadcrumbs, screenshots, audio working data and Glassbox fallback artifacts. Bundled immutable helper scripts remain under the package's `tools` tree.

Session and scheduler leases hold a Windows byte-range lock (POSIX flock elsewhere) for their lifetime. The OS releases ownership when the owning process exits; no elapsed-time heuristic steals a live process's lock. Persistent lock files are never unlinked to create a second lock namespace. Configuration and row stores combine the existing baseline delta merge with cross-process coordination and atomic replacement. Unknown data versions refuse startup/writes. These guarantees require cooperating compatible runtimes; a legacy external writer that ignores leases cannot be made safe by this process alone.

The shared router record remains `owner=litetui`. A live sibling process's record is not treated as this process's orphan just because both belong to the same family. Explicit executable selection is validated before attachment or spawn and does not fall back silently. The existing backend confirmation gate remains the common load/reload boundary.

## Verification and review

Initial behavioral tests failed before the interface/ownership implementation. `tests/test_gui_management.py` covers protocol/typed settings, actual child-process lease exclusion and exit recovery, incompatible data versions, explicit invalid executable, persisted management state, authority denial without host invocation, host-tool correlation, model confirmation, scheduler exclusion, deferred handshake values, owned active work and writable screenshot paths.

Two existing router tests change their expected behavior deliberately: a live same-family server is a sibling, and its different-port live claim is preserved. No family value was added. Other existing tests retain their behavioral assertions.

Root independently exercised a real RPC child against a deterministic HTTP model fixture: durable streaming/restart, invalid settings/version and stale IDs, denied shell sentinel absence, question completion/cancellation recovery, real MCP child reconnect, an approved configured hook, and two-child automatic scheduler execution once. Root reported five passing contract scenarios. This worker did not load or query a live model.

Final named-file regression, lint comparison, exact disk-derived file list and clean-tree evidence are appended after the correction/review pass. Passing deterministic tests does not replace Ryan's packaged application and real-backend review.

### Correction pass

The initial 156-file slice reported **14 failed, 1921 passed, 7 skipped, 6 xfailed in 688.31s (0:11:28)**. Nine torn-transcript fixtures opened their second writer without closing their first; they now release the first repository before the second adopts it. Replay, damaged-byte preservation and append assertions remain. A fake RPC submitter now supplies a real staged repository; the correlation-ID assertions remain. A minimal scheduled-job host omitted the optional prior slot, which now defaults to `None` for rollback. The missing-executable test checks the new standalone path/server recovery actions. The task-loader census now verifies the sole call lives inside `app.py`'s constructor, rather than hardcoding its line number.

The initial slice also exposed a pre-existing kill-on-close census mismatch: both base `9735a96` and this branch contain calls in `lifecycle_hooks.py` and `plugins/core_tools.py`, while the test expects only the latter. Its resolution and final green evidence are recorded below.

New red-first correction arms cover a fresh current conversation before its first disk write, queue/interrupt under both Enter settings, a sibling scheduler preserving a foreign conversation's loop, external scheduler add/edit/delete refresh, resumed inbox delivery after an abandoned Quit, inner usage snapshot shape with unchanged legacy events, unknown writer protocol refusal, and invalid typed model configuration before persistence. Settings persistence failure reports a visible failure even when the existing setter applies values for this session.

Event correlation now separates asynchronous management request context from the active execution's operation ID. Queued prompt items carry their own request ID but round-boundary injection remains part of the existing execution and retains its ID on the sole `turn_end`. An interrupt's next execution adopts the queued request's ID. Root's real-child question → attachment change → queued prompt → answer fixture first reproduced the wrong final ID, then passed with both input messages persisted and the original operation ID retained.

Minimum supported cooperating terminal runtime: a build containing T719's protocol-1 leases and data compatibility checks. Older LiteTUI processes ignore these locks. Existing harness presence contains CLI/pid/name but does not identify a data root or writer protocol, so it cannot prove that an older live process writes the selected folder. Close older writers before selecting a shared folder; use a T719-capable terminal build for concurrent access. The runtime refuses unknown marked data and writer protocols instead of accepting them silently.
