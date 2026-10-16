# T0565 — P0 Rust Runtime Contract and Isolation Gates

**Disposition: P0 remains open; contract work can proceed offline. Real-runtime integration, full parity, isolation and performance acceptance are not established.**

This synthesis reconciles all ten source audits with the supplied plan and `tests/test_rust_wire_contract.py`. It grants **no new launch, merge, push or cutover authority**. Python remains the runtime, policy, persistence, scheduling and execution authority; Rust is its presentation/protocol client.

## 1. Executive verdict and evidence boundary

The reports establish a usable command seam, GUI negotiation, additive event envelopes and important negative-path semantics. They do **not** establish complete turn correlation, recoverable event ordering, safe owned-child startup or lifetime containment.

Parent verification after synthesis: **18 wire tests passed**, and **42 tests passed** in the focused set including packaging tests and existing terminal-output, approval-type, stream-sink, model-switch and unsupported-version checks. Nine synthetic case bundles were exported from passing executions. The synthesis originally reviewed the first seven tests; separate source review found and prompted the evidence-packaging fixes and malformed-command regression below. These tests retain real reader/dispatcher/serializer functions, an unbound app emitter and real futures, while substituting the host loop, workers and streams. They establish **in-process behavior only**, never actual child isolation.

No audit report’s proposed tests are claimed to have run. Historical Python passes and Rust preview observations remain separately attributed planning evidence.

**Citation discipline:** most audits reference HEAD `15850ed…`; lane9 explicitly guarantees that revision only for its app excerpts. Non-app line numbers were counted independently and sometimes differ between lanes. References below therefore prefer **file/function names**. Inherited app anchors are navigation aids, not newly verified citations. Before freezing fixtures, record actual revisions, source hashes and checked anchors. A child’s missing packet is not a missing repository file.

## 2. Established command, event, identity and authority contract

| Area | Source-established behavior | Required Rust contract / unresolved gate |
|---|---|---|
| Transport | `rpc.py:rpc_emit/_reader_loop`: JSONL; emitter uses `json.dumps(default=str)`, newline and flush. Blank input is ignored; reader errors are direct records. EOF schedules exit; output errors can be swallowed. | Incremental UTF-8 decoding across reads; separate stderr drain; bounded frames/queues. Input encoding, concurrent-write integrity, limits and real pipe behavior need proof. No delivery guarantee follows from successful emission. |
| Negotiation | `gui_rpc.py:dispatch`: `gui.hello` requires integer protocol version `1`, excluding booleans; enables GUI behavior and returns protocol/data versions, capabilities and operations. Prior hello is not server-enforced authorization. | Require successful compatibility negotiation before management controls. Inventory names are not argument/event/completion schemas. Establish the activation boundary under concurrent events, not merely sequential test execution. |
| Responses | `rpc.py:_respond`: `type`, `ok`, optional correlation `id`, and `result` or string `error`; null/absent IDs are omitted. Commands use top-level fields, not universal `params`. | Separate transport success from nested operation success, persistence success and effective state. Preserve optional fields such as `needs:"terminal"`. |
| Event representation | `LiteTUI._rpc_emit` emits raw first, then `gui.event` with session, operation and nested event when enabled (`app.py:5730–5748`, inherited). Direct responses/errors remain unwrapped. | Render one authoritative representation after a verified boundary; consume direct responses/errors. Suppress only verified mirrors—not equal deltas generally. Preserve original records diagnostically. |
| Identity | Command ID, operation ID, session/conversation ID, ask/approval/request IDs, queue/native-entry IDs, task/process/tool-call IDs have different meanings. Management context can override the app’s operation fallback. | Use distinct types and process-generation namespaces. Command IDs are not idempotency keys or proven turn IDs. Never assign an ambiguous event to “latest prompt.” Complete turn/message identity remains blocking. |
| Admission/completion | Legacy prompt can emit `submit_refused` before returning `turn:"accepted"`. GUI submission returns acceptance plus behavior. Queue input can enter an existing turn. | Model request, admission, queue ownership, execution, turn completion and effective resource state separately. Acceptance, worker success, presentation settlement and abort ACK are not completion. |
| Cancellation | RPC abort releases waiters before requesting stop. First active stop is soft; another can force cancellation. Tool cancellation and queued-input cancellation are separate paths. | Preserve Python ordering and eligibility checks. Disconnect means interrupted/unknown, not safely cancelled. Old completion must not mutate newer work. |
| Approval | Dedicated target fields win over legacy `id`; `allow` and optional `remember` require booleans. Python owns frozen authority, expiry, revocation, audit and settlement. | Send independent command and target IDs. Denial-first UI; explicit once/always choices. ACK means decision accepted—not execution or standing-rule persistence. Never replay decisions. |
| Unknown/failure | No global event sequence, resume cursor or deduplication contract is established. The baseline allowed non-string command types to escape uniform error handling; this checkpoint now rejects those before routing. Other malformed fields remain unaudited; mutations can partially apply before failure. | Preserve unknown payloads bounded and sanitized. Under the retained plan, unknown non-allowlisted events block further mutation pending explicit recovery. Reconcile uncertain outcomes through reads; **never automatically replay side effects**. |

Terminal rates may be null and caller extras can override base `turn_end` fields. Do not invent stop-reason enums, completion identities or exactly-once guarantees.

## 3. Startup and isolation matrix

A temporary data root, headless mode and disabled tool advertising are **not** containment.

| Boundary | Established exposure/control | Required evidence before real-child reliance |
|---|---|---|
| Before import | `paths.data_root()` honors `LITETUI_DATA_ROOT`; some constants freeze at import. Home integrations, explicit roots, checkout resources and workspace paths escape that root. | Absolute synthetic data/home/work/temp roots and clean environment before import; deny protected roots; trace migration, locking and explicit-root bypasses. |
| Owned startup | CLI/session APIs are mandatory; bare mutable app construction refuses. RPC otherwise defaults to autonomous profile. Constructor/mount already perform discovery and service setup. | Explicit valid authority selection **before spawn**; cancellation/missing selection starts nothing. Real create/acquire, collision, ownership, resume and failure-path evidence. Never forge ownership/ancestry markers. |
| Plugins/hooks | Plugins import before disable checks; registration is not enforced pure; activation occurs on mount. Hook “observe” can execute code; queued snapshots survive later disabling. | Audited import/register/activate seams; hooks off before snapshots; process sentinels through teardown; residue and failure tests. Visibility gates are not authorization. |
| Fleet/scheduler | Harness disable covers important doors, not every direct registry/presence access. Cron construction loads jobs; activation starts monitoring. | Synthetic fleet/job stores; no live registration/mailbox access; audited constructors, timers and shutdown. Do not fake successful registration to bypass legitimate startup requirements. |
| MCP/backend/monitor | MCP startup can spawn executables or make HTTP requests; lazy HTTP remains eager. Browser demo still writes. Backend construction alone does not prove a request. | OS-enforced network/process policy plus explicit fixture allowances; record forbidden **attempts**. Audit provider-native configuration and monitor sinks/threads. |
| Descriptors/terminal | CLI duplicates protocol stdout, redirects normal stdout and runs headless; image initialization occurs earlier. Existing follow-up identifies non-TTY probe guards. | Real no-console test: first stdout bytes parse as JSON, no escape preamble, stderr flood handled; verify cell-size behavior. Corruption is a risk to test, **not established fact**. |
| Lifetime/Windows | Visible shutdown closes admission and releases repository before session under a registration lock. Some MCP uncertainty is quarantined; descendant cleanup is not universally proven. | Job Object/ownership design, nested jobs/breakaway policy, detached descendants, bounded shutdown and recovery; shared services excluded. EOF/ACK must not imply cleanup. |

Environment switches (`LITETUI_NO_HARNESS`, hooks off, update-check opt-out) are defense-in-depth—not a sandbox. Controls must remain effective through shutdown.

## 4. Prioritized blockers and exact next evidence

### P0-A — Complete correlation and lifecycle contract

**Inspect:** all event producers and consumers of `_gui_next_operation_id`, `_gui_operation_id`, `current_operation`/`OPERATION_CONTEXT`; `_submit_text`, `_stream`, turn-start/end helpers, queue delivery, native ledgers and conversation switching.

**Produce:** versioned event inventory with required fields/nullability; operation-to-turn/message mapping; snapshot/event ordering policy; sanitized traces for refusal-before-ACK, queued mid-turn delivery, interrupt, session switch, late completion and missing terminal events.

**Tests:** reducer cases must reject “latest prompt” attribution, preserve repeated identical deltas and close only identified work. Add identity fields compatibly where repository evidence proves they are missing.

### P0-B — Owned startup and lifetime containment

**Inspect:** `cli.py`, paths/settings precedence, `AgentSession` and launch-context implementation, `ConversationRepository`, migration/coordinated-write helpers, registration polling/writers, prepared-child launch/process/environment code, image/backend initialization and teardown.

**Produce:** complete filesystem/network/process effect inventory from first import through release; approved resolved-root policy; Windows ownership and shared-service exclusion design.

**Tests:** synthetic failure injection at acquisition, constructor, mount, connection and registration; wrong-root negative control; no child without authority selection; collision refusal; graceful shutdown, EOF, forced termination and descendant recovery. In-process mocks cannot discharge this blocker.

### P0-C — Authority and cancellation edge cases

**Inspect:** all approval-waiter creators, standing-rule persistence/matching, permission-judge bypass, parent/child approval bridge and capability/argument binding.

**Produce:** evidence that normal production approvals possess authority records; explicit timeout/abort/revocation outcomes and child identity mapping.

**Tests:** human/agent races, deadlines, store replacement, persistence failure, abort races and restart replay. The current real-future test deliberately has **no authority record**: it proves resolver transport behavior, not policy enforcement. Verify denied side-effect absence with a successful inert control later.

### P0-D — Wire hardening and parity inventory

**Inspect/test:** non-string `type` and command fields, malformed event payloads, concurrent emission, broken stdout, escaped/Unicode data, serialization fallback, partial settings effects and model reconnect completion.

Extend `tests/test_rust_wire_contract.py` only within its declared boundary. Fragmented byte reads, fd ownership and scheduling require separate transport/process tests.

Reconcile `parity.json` against every frontend contribution, including custom plugin specimens, settings timing, images/voice and both hosts. Missing access leaves rows blocked—not waived.

### P0-E — Observable host/performance protocol

Freeze Windows Terminal and LiteSuite launch/control/capture procedures, trace schema, packaging topology, workload manifests and instrumentation boundaries. Proposed runner names are not implemented capabilities.

## 5. Dependency sequence: smallest safe implementation

1. **Pin evidence:** revisions, checked citations, source hashes, fake boundaries and execution records.
2. **Extend in-process wire evidence:** existing production path, negative tests and optional synthetic serialized captures; validate test sensitivity.
3. **Freeze schemas and parity rows:** resolve identity/terminal gaps before queue/interrupt reducers.
4. **Implement offline Rust transport/reducer:** reviewed fixtures, unknown-event blocking, bounded buffering, generation isolation and no replay.
5. **Prove owned-child containment and host procedures:** only within separately applicable authorization.
6. **Establish Python baseline**, then obtain approved numerical thresholds.
7. **Implement real-process vertical path:** hello and narrow read-only management first; then owned mutations, prompts and consequential controls.
8. Expand to full inventory parity; perform equivalent both-host comparisons and fault acceptance.

A read-only first slice reduces risk; it does not redefine the full-parity goal.

## 6. Performance evidence protocol

Measure actual Python Textual frontend/runtime first—not the Rust demo. Compare later against Rust **plus retained Python runtime and owned descendants**, with equivalent behavior and identical isolation.

For **each host separately**, freeze revisions/build hashes, optimized Rust profile, Python dependencies, hardware/power state, terminal/PTY versions, fonts/DPI, dimensions, theme, fixture hashes and deterministic provider cadence. Do not pool hosts or compare a mock against live inference.

Retain five launches and three steady-state repetitions per frontend/host, with explicit warm/cold definitions. Fresh state is not necessarily cold cache. Interleave later reference runs to detect drift.

Workloads: startup/idle; streaming with typing/paste; 10,000-message history; approval/tool/cancel; streaming resize across compact boundaries; equivalent management/plugins; faults separately. Validate final rendered content and persisted outcomes, not ACK counts.

Record monotonic timestamps and clock alignment for:

- launch, child creation, protocol readiness, first frame and first genuinely editable input;
- input injection → handling → host-visible update;
- event arrival → visible update, backlog, streaming gaps and cancellation settlement;
- resize request → correct visible layout;
- cumulative CPU and synchronized process-tree memory, including exited descendants.

Report Windows working set/private bytes explicitly; do not sum unrelated peak times. Account for host overhead and shared services separately. A draw/write return is not visible-frame proof.

Retain raw traces, failures and individual runs. **Baseline precedes thresholds.** Measurement mode needs no thresholds; acceptance must fail if approved thresholds or either host’s evidence are missing. Inject render delay to prove gate sensitivity.

## 7. Ten-lane reconciliation and final status

| Lane | Synthesis resolution |
|---|---|
| 1 — Events | Its missing `gui_rpc` packet is resolved by lane2; full turn attribution remains unresolved. |
| 2 — RPC | Establishes negotiation/commands, not complete lifecycle or isolation. |
| 3 — Approval | Preserves Python authority; universal authority-record presence and child binding remain open. |
| 4 — Queue/cancel | Establishes distinct ownership/state transitions; omitted cleanup is unknown, not absent. |
| 5 — Startup | Mandatory owned CLI remains valid; lane6 supplies root details and lane8 some harness details. |
| 6 — Data | Confirms root routing, not containment; repository code presence is not complete write-set proof. |
| 7 — Plugins/hooks | Supplies hook behavior missing elsewhere; registration/“active” status is not completed activation. |
| 8 — Services | Supplies concrete external doors; missing hooks/RPC packets are resolved elsewhere, not all shutdown gaps. |
| 9 — Harness | Parent implementation follows its production-function boundary; captures remain in-process. |
| 10 — Performance | Supplies protocol design, not measurements or numerical gates. |

**Implemented:** 18-test in-process wire harness, five packaging tests, nine optional evidence exports, and non-string command-type rejection; offline presentation work as previously recorded.
**Tested:** parent executed the 18 wire tests and 42-test focused combined set. New-file Ruff and capture-runner mypy passed. `rpc.py` has six Ruff and three mypy findings, all reproduced on its unchanged HEAD blob; no lint/type regression was introduced by the guard. The ten audits and synthesis themselves performed no execution.
**Live-verified:** no new isolated child, runtime parity, both-host integration or performance evidence established here.

**Retained gate:** P0 open; full parity, Python authority and no side-effect replay remain mandatory.

## 8. Reproduce the implemented evidence

From the owned worktree, using the project Python environment and `PYTHONPATH=src`:

```powershell
python -m pytest tests/test_rust_wire_contract.py tests/test_rust_wire_capture.py tests/test_rpc_terminal_output.py tests/test_rpc_approval_types.py tests/test_stream_sink.py tests/test_rpc_model_switch.py tests/test_gui_management.py::test_protocol_refuses_unsupported_version -q
python scripts/capture_rust_wire_contract.py
python -m ruff check tests/test_rust_wire_contract.py
```

The runner provisions a fresh `artifacts/rust-tui-port/wire-captures/run-<uuid>/` BEFORE pytest snapshots the checkout root. Cases bind synthetic inputs and actual serialized outputs by SHA256, selected source/isolation files, runtime/dependency identity and explicit fake boundaries. Only a zero-exit pytest process plus hash validation receives atomic `completed.json`. This marker binds listed bytes; it does not independently establish test collection completeness. The retained pytest log explicitly reports 18 passed for this run. A directory without that index is incomplete/diagnostic, not a green run. The published run is `run-499a6635da0a404dbb3c7e46a6495da1`; its index binds all nine case bundles and the pytest log. This is selected-source provenance, not a complete environment lock. Nine cases cover reader errors/EOF, negotiation/mirrors, unsupported protocol, usage/approval cache, approval denial, operation context, refusal-before-ACK in raw/GUI modes, and terminal null rates. Additional tests cover non-RPC silence, serializer fallback, broken output and five malformed type values. Packaging tests reject failed runs, no captures, changed bytes and wrong run identity.

A concrete captured warning: the legacy whitespace-prompt path emits `submit_refused` and can then ACK `turn: accepted`; its GUI wrapper can have a null operation ID. A client cannot safely infer a started turn or correlate every event to the latest command. Repeated identical event payloads are also preserved; content equality is not an event identity.

The harness binds real app methods to an inert host. It never constructs LiteTUI. GUI negotiation does run the real data-version check inside pytest's disposable data root. Thread scheduling, Textual workers, lease acquisition in the refusal probe, and the paint callback are explicitly substituted; pending approval futures are real but have no production authority record. Network/process tripwires are partial and are **not an OS sandbox**. Windows asyncio uses local socket pairs internally, so the harness does not globally forbid every socket `connect` call.

All ten Wave1 agents, synthesis and final test review were explicitly requested as `gpt-6.1-sol` at High. Original lane reports/task IDs are retained locally under `artifacts/rust-tui-port/parallel-wave1/`; they remain source-audit evidence, not verified parity rows. The inventory ledger was not overwritten or promoted to passed.


### Review-driven hardening and remaining limits

Five new reader/dispatcher tests initially failed with `AttributeError` on `null`, numeric, boolean, list and object `type` values. A narrow guard in `rpc._dispatch` now emits a correlated failure and returns before any routing; a following valid command still runs. No broader malformed-field safety claim follows.

Evidence export initially depended on a pre-existing artifacts directory and used independent fixed filenames. Review caught both limitations. The explicit runner now pre-creates a unique directory, clears pytest selection addopts, bounds the run, verifies case bytes and publishes the completion index only after teardown succeeds. Original partial captures and failed runs were preserved, not silently cleaned or counted green.

Two Windows harness pitfalls were observed and corrected: globally blocking socket.connect interfered with asyncio's own socket pair; `platform.system()` attempted to spawn `ver` under the process tripwire. Runtime metadata now uses `sys.platform` and `sys.version` without process probes. The runner safely prints Unicode diagnostics under Windows legacy console encodings.

Follow-up review found no blocker in the fixes, but correctly noted that pytest exit zero alone does not prove an unmodified collection inventory. The completion index now states this limitation; collection/plugin-driven skips must be read from the retained pytest log. The five malformed-type cases are covered by that 18-pass log, not separate replay bundles.

## 9. Subsequent offline Rust wire checkpoint

The earlier sections record the Python capture/audit checkpoint. The Rust library now implements bounded incremental JSONL framing plus a connection-local `WireClient`; the preview is still offline and is not connected to this library path. See `rust/litetui-terminal/README.md` for the public boundaries.

Implemented and tested: strict UTF-8/object framing, valid-prefix delivery and permanent decoder stop on failure/EOF; typed command correlation; internally generated generation-scoped non-reused IDs; bounded pending requests; matching versioned hello; sticky activation uncertainty; explicit per-type mirrored/direct selection without content deduplication; nullable envelope identity; direct-response draining after faults; and uncertain pending requests retained after disconnect. Compatibility is not an authority grant or snapshot barrier. The decoder has an explicit 128-value nesting bound in addition to its caller-chosen byte bound.

Review exposed three parsing/identity defects that were repaired: opaque large numbers initially rounded through machine floats; public signed-positive and unsigned command IDs could serialize identically but compare unequal; and enabling exact numbers exposed serde's private number-map coercion of literal wire objects. Raw-aware container parsing now separates actual containers from primitive number parsing. A subsequent duplicate-key regression proved overwritten subtrees could bypass depth validation; the object visitor now validates **every** occurrence before last-key-wins insertion. Numeric rounding, object coercion, and overwritten-depth tests were observed failing before their respective fixes. All wire payloads still require their eventual event-specific schemas before state application.

Verification command: `cargo test --manifest-path rust/litetui-terminal/Cargo.toml --locked --offline`, with Cargo fmt and all-targets Clippy (`-D warnings`). The new wire-specific tests comprise ten decoder cases and seventeen client cases; existing preview, input and theme tests remain included. No real-runtime, performance or full-parity claim follows.

### Separate construction work still in progress

A new, uncommitted Python construction probe ran four cases against real `AgentSession`, `LiteTUI(rpc=True)`, backend/client construction and `ConversationRepository`, with extension activation and external calls controlled. Together with the existing launch-context suite, 43 tests passed. This is more than the earlier synthetic host, but **not a review-complete containment gate**: Sol review identified partial-constructor and exception-path fixture cleanup gaps, a missing denied-attempt ledger, inherited silent MCP masking, and insufficiently strong storage/concurrency assertions. The attempted test-source hardening was refused by the active tool policy; it was not retried through another form. No Python product code changed in this checkpoint.

The capture runner also completed another 18-test synthetic capture invocation. Rust consumption of those bytes remains unverified: a read-only artifact inspection was refused by the active tool policy as a launcher operation. Both policy diagnostics were reported to Sentinel; neither refusal authorized a workaround. Capture replay, production delivery inventory, immutable producer/turn provenance, payload schemas, downstream queues, state reducer, child containment and live integration remain open. Full 1:1 port parity is **not complete**.

### Subsequent checkpoints: replay, construction and envelope IDs

The master plan records the later reviewed construction/storage gate (3678b88) and hash-verified output-only replay (bc6856c), superseding the in-progress status immediately above. Neither establishes a mounted lifecycle or real-child containment.

The envelope-identity checkpoint adds a canonical UUID4 `event_id` at the top level of each `gui.event`. It creates that ID and captures existing session/operation metadata before the first sink write; it then writes the unchanged legacy payload followed by the envelope. Payload-owned IDs are separate and are never overwritten. That checkpoint establishes emission identity only, not execution identity, retry/delivery guarantees, deduplication or replay storage.

The subsequent execution-origin increment explicitly attributes only generic `turn_start`, `text_delta`, `reasoning_delta` and `turn_end` producers. These envelopes additionally carry `execution_id`, `hook_id` and `source`, and use captured `session_id` and `operation_id`. `execution_id` is a fresh UUID4 allocated synchronously per scheduling/inline invocation, before its asynchronous body runs. It is not an admission, command, event or logical-turn ID. Queued admission B does not create execution B or change execution A's originating metadata. Direct invocations with no admitted origin capture their session but leave operation/hook/source null. Malformed mutable metadata is normalized to null without changing command acceptance. Native, tool, usage, refresh and retry callback producers remain outside this explicit attribution scope; their existing ambient operation fields are not execution provenance. Raw legacy payloads remain unchanged, and no event deduplication or delivery guarantee is added.

Old ID-less envelopes remain accepted by the Rust transport. New envelope IDs survive decoding in `Notice::Event.frame`; the nested payload remains separate. Distinct equal emissions and repeated replay IDs are all retained. The historical Python capture streams remain unchanged and continue to replay against their original hashes. Current verification and limitations are recorded in the master's checkpoint sections.

### Bounded generic-stream projection boundary

The offline Rust projection consumes selected hello/event notices, never provisional legacy events or raw mirrors. It requires a nonempty originating session and a canonical UUID4 execution ID; operation/hook/source may be null but must remain equal for that execution. It validates the selected envelope/payload pairing, start model/thinking values, delta strings, and terminal reason/rate/source/error fields before changing row content. Unsupported producer kinds fail explicitly. Connection generations are caller-owned and stale input is inert. This is an identity-keyed generic assistant projection, not a production delivery inventory, snapshot reducer or replay reconciliation policy.

An observed terminal ends only that identified stream; it cannot establish successful command/admission completion or durable storage. On fault/disconnect, active rows retain their text and become incomplete. Caller-supplied positive row/text/metadata budgets are enforced before append/insertion/terminal mutation. Terminal-control and bidi escaping is presentation-only, and source text stays intact. The shared assistant renderer is exercised through TestBackend; it is not wired to the preview or a runtime process. General transcript, native/tool event schemas, bounded transport queues and lifecycle/host parity remain open.

### Generic executor observations (producer only)

The next producer increment adds two event kinds in the generic `_stream_worker` tool loop, using that invocation's captured `EventOrigin` for their GUI envelopes:

- `generic_tool_requested`: `observation_id` (fresh UUID4), `round` (zero-based loop iteration), `slot` (tool accumulator key), `provider_call_id` (the provider ID or existing `call_{slot}` fallback), and `name`. Emitted after successful object-argument parsing immediately before calling `_execute_tool`. It means the loop reached this host invocation boundary, **not** that authorization or dispatch occurred. Round includes completion-retry iterations; slot is not necessarily a dense execution ordinal. Arguments are deliberately omitted: this increment does not define a safe argument-preview serialization contract.
- `generic_tool_returned`: the same `observation_id`, `result` (after the existing shared escape stripping and secret-name redaction), and `ok` (the executor's unchanged return flag). Emitted before presentation and model-context transforms. It does **not** assert tool success, persistence, transcript insertion, background completion, or successful delivery. A refusal is a normal return; a background receipt with `ok=true` is still only a receipt. Existing redaction recognizes named secret representations; it is not a guarantee to detect every secret-shaped value.

Each attempted host invocation gets a distinct observation ID even when provider IDs repeat within/across rounds. That ID is distinct from execution, GUI-event, operation and background-task IDs. The raw observation payload does not contain execution attribution; its GUI envelope does. Existing raw `tool_call` and all other producers remain unchanged. There is no new inferred ownership for native tools, approvals, usage, background completions or callbacks.

Invalid/non-object arguments and calls skipped after stop produce neither observation. Every loop slot resets its optional observation ID, preventing an invalid/skipped call from reusing a preceding call's ID. An exception/cancellation escaping the executor, progress bookkeeping, or shared hygiene can leave only a request. There is no synthetic pair, `finally` emission, retry, or recovery claim. New emission failures propagate through the existing emitter contract: failure before execution prevents that invocation; failure after execution can prevent presentation/context insertion. Consumers must retain uncertainty rather than retrying side effects. Partial raw/envelope delivery remains possible.

The producer checkpoint defined/tests this boundary without Rust consumption. Mounted tests use scripted providers/transports, not a real child or provider. Negative inline tests explicitly cancel their test-owned elapsed painter after injected failure because the existing exceptional path can leave it waiting on an inflight tool; those tests do not establish runtime exception cleanup.

### Rust tool-observation consumer

The next Rust increment consumes these two kinds under the existing selected-notice, immutable-origin and connection-generation boundary. Each execution row retains request-ordered `ToolObservation` values. Canonical UUID4 observation IDs are unique across that projection generation, including observations already returned; a return must resolve within the same execution. Provider IDs and round/slot coordinates are retained but never used as alternative join keys. Requests require string name/provider fields and nonnegative u64-representable integer coordinates; returns require string result and boolean `ok`. Empty strings are accepted. Missing/wrong-type/overflowing identity or fields fault before mutation.

`Requested` means only a request record was observed. `Returned` preserves result/host flag, never inferred operation/tool/background success. `Incomplete` means no retained return before stream end or loss of certainty, including a received return rejected for malformed fields or capacity. A valid `turn_end` still ends the stream row while marking only its pending observations incomplete. Global faults/disconnect affect all pending observations and leave returned observations intact. Late events never restore certainty or fabricate pairing.

The explicit positive `tool_calls` limit is aggregate across rows and is not released on return. Observation ID, provider ID and tool name bytes plus sixteen bytes for coordinates charge the shared metadata budget. Results charge the shared retained UTF-8 text budget. These are logical content/count limits, not exact heap or render-time bounds. Unknown producers remain blocking; this six-kind policy is only a fixture policy, not a production traffic inventory.

Tool rendering shares the demo's unbordered body geometry and semantic theme colors, escaping controls/bidi at display time. Tools are grouped after each aggregate assistant row with a visible nonchronological label. No arguments, timings, approval or task-completion state is invented. There is no interactive projected-tool controller, runtime connection or live visual acceptance in this increment.

### Native live reasoning reconciliation (subsequent live increment)

The live serialized Claude path is separate from the generic identified-stream projection described above. Claude `reasoning_delta` now carries additive `reconcile: "append" | "replace"`, reflecting the same `thinking` snapshot distinction used by Python's ThinkingBlock. The raw event type/text remain available to existing consumers; clients must interpret the new field to repair divergent prefixes. This is not a new execution ID, pairing receipt or authority grant. Rust requires explicit reconciliation on native Claude traces; old ambiguous Claude emitters fail visibly. Generic non-Claude deltas without the field retain append semantics. Present malformed modes are refused.

Accepted reasoning is confined to an active started turn. It never makes an answer visible for the completion gate. Reasoning and answers share emitted-content limits; snapshots do not refund emitted work. Retained snapshots replace only the trace, preserving the answer and refunding only the prior trace's retained bytes/display units. Bounds are checked before mutation. Empty snapshots clear the trace; late/closed observations cannot revive it. Rendering uses existing thinking-card geometry and terminal-control escaping without invented timing/token metadata. Tools, approvals and GUI reconciliation are not enabled by this addition.

### Bounded raw ingress foundation

`transport::Inbound` is a single-generation, synchronous borrowed-byte FIFO before JSON decoding. Caller-supplied positive limits cover per-frame bytes excluding LF (including CR), queued frame count, and aggregate queued original bytes including LF. Partial-frame content has its own per-frame bound. Complete frames are moved, not reserialized; popping transfers ownership and releases the queue charge. Limits are logical retained bytes/counts, not exact heap/capacity or consumer-retained-frame bounds. No production defaults are selected.

Capacity failures are fail-stop overload rejection, **not backpressure**. Each whole frame is checked before insertion. The offending frame and unread input suffix are explicitly discarded, accepted earlier frames remain drainable, and the first fault remains latched after draining/EOF/later input. Frame overflow precedes queue checks; at LF a full frame-count queue precedes byte-budget failure. Feed/drain scheduling affects whether a burst fits. A fresh connection needs a fresh caller-owned generation; stale generation feed/EOF calls are inert even while a current partial frame exists or after closure.

Blank-line filtering matches the actual decoder's ASCII convention (form feed included, vertical tab excluded). Oversized blank lines still fault. Nonblank invalid UTF-8/JSON is retained unchanged for the decoder, not silently repaired or classified as blank. EOF with any unterminated fragment, including whitespace, faults; EOF at a frame boundary records closure, not successful work or child cleanup.

The future session owner must stop admitting new work as soon as ingress is terminal, deliver retained FIFO frames before terminal uncertainty, and separately disconnect/fault the decoder/client/projection. Decoder failure stops protocol delivery immediately; later raw frames may be drained/discarded only for cleanup, never replayed through a new decoder. An accepted terminal record remains observed when later ingress fails; this primitive is not a transactional completion protocol. No async reader/writer, stderr drain, real pipes, cancellation owner, process/descendant cleanup or live runtime integration is implemented here.
