# WS1: Rust terminal frontend with retained Python runtime

Master: [master.md](master.md). Source inventory: [inventory.md](inventory.md).

## Handoff contract

- Task T0565; one workstream, sequential phases P0–P5. Status: P0 in progress; user-requested offline Rust preview implemented. Real-runtime P1 gate not passed.
- Created 2026-10-15; modified [2026-10-15]; agents [OpenBolt]; source [9ef5337]; conversation [83816ba3-d370-4c4f-b0f7-022cc4b4074d]. Append future metadata.
- Work only in `C:\Projects\.worktrees\OpenBolt-rust-tui`, branch `work/rust-tui-OpenBolt`. Public repository: local commits only, no secrets or private transcripts in fixtures.
- Goal: full baseline UI parity on Windows, verified in LiteSuite's embedded terminal and Windows Terminal. Preserve familiar layout/keys. Python remains installed temporarily. Preserve custom plugin behavior but allow its UI source to change. Use separate fresh data; never reset old state. All backend and authority behavior remains in scope.
- No timeline until inventory is reconciled. No “MVP complete” substituting for parity. Later phases may not proceed with missing contract details.

## Current implementation checkpoint - 2026-10-29

F8 now pages the owned source catalog and preflights selected history without mutation before guarded open, connected history, fresh state/policy and held-input readback. The draft survives, and oversized previews leave the original session usable. Independent review passed. Larger-history paging and remaining session actions are still open; see the newest master checkpoint for verification and limits.

Portable staging now verifies a supplied executable and tracked runtime/resources with exact hashes, preserves external Python and retained data, and keeps helper outputs outside the immutable package. Both alias directions and PowerShell screenshot defaults are covered; 33 staging and 47 helper tests passed with independent review signoff. Full installation/provenance/host/resource acceptance remains pending. See the newest master checkpoint and remaining-parity.md.

Claude prepared continuation now preserves original message/delivery identity, source authority and a held queue barrier through worker retirement. Pre-start cache refusal and started-turn failure have distinct native outcomes; independent readback retains unsent/uncertain evidence. Prompt operation IDs are fresh across launches. The newest master checkpoint records full verification and the remaining parity work; bounded conversation browsing and portable packaging are now executing in parallel under the user's explicit /ls-max-parallel request.

F7 X then fresh visible Enter now resolves one uncertain Claude delivery without replay or any claim about provider effects. Source persistence and independent native readback are mandatory; other holds remain. Claude continuation still needs existing-message reuse, a native pre-start refusal outcome for cache warnings, and settlement that preserves unsent/uncertain ledger entries. See the latest master checkpoint for evidence and full remaining scope.

Claude selected-segment entries now restore under the same launch-local hold policy. F7 exposes prepared and uncertain deliveries with native/segment/workspace identity. Only prepared input offers cancellation, with durable source verification and independent native readback. Ordinary Python policy is unchanged. Explicit Claude continuation and uncertainty resolution remain required; the latest master checkpoint records verification.

F7 now checks native provider history with H then a fresh visible Enter. Only exact positive thread/turn/client-input evidence can record an accepted user message and remove its hold; absent evidence remains held. Rust verifies an independent queue readback and renders an inert previously-accepted row, without executing or resending it. Current verification and remaining scope are in the latest master checkpoint. Claude recovery and session browsing remain required.

F7 now continues the source-offered oldest held input with C then a fresh visible Enter. Rust reserves a distinct live row/command while preserving the draft, labels a pre-start refusal separately, and requires a correlated saved queue read after the exact Python worker retires. Failed/contradictory outcomes stop without replay; uncertain delivery entries remain held. The latest master checkpoint records the native, owned-source and controlled-process evidence. Provider reconciliation, Claude recovery and full parity remain open.

Source-side explicit continuation now has a durable claim/admission/materialization contract and a distinct not-started refusal response. It preserves saved source/profile while capturing the fresh command ID. The Rust F7 Continue binding/reducer is the next implementation step; it must not treat refusal or a positive admission ACK as completed execution. Current inspect/cancel behavior remains unchanged. See the latest master checkpoint for evidence and remaining scope.

The latest master checkpoint adds F7 held-input inspection and explicit eligible cancellation. Full escaped content and delivery evidence are scrollable. Delete followed by a fresh visible Enter cancels only source-offered queued/next_turn entries, after current ownership/journal validation and durable denial. A separate correlated readback updates held count; ambiguous storage or readback never permits retry or silently releases the hold. Pending operations retain composer ownership. Continuation, uncertain-send reconciliation and Claude prepared-input recovery remain open.

Recovered Codex input is now held by an explicit Rust-owned launch policy across idle flush, native steering and recovery. Resume requires a matching policy/count readback; startup and persistent footer expose the held count. Pending entries remain unchanged across restart and new explicit turns in the controlled native process gate. Explicit queue actions and in-app conversation browsing remain incomplete; see the newest master checkpoint for 330 Rust tests, 34 process tests and the Python baseline split.

The asynchronous source conversation-open barrier now waits for connection and newly scheduled native history before returning a fresh connected transcript. It rejects lifecycle/identity uncertainty and blocks competing commands during the transition. This is a prerequisite for the native browser, which remains pending. Restored provider queues require their own no-replay/reconciliation gate for both in-app opening and saved startup; current resume evidence covers histories without pending provider input. See the latest master checkpoint for 95 passing Python tests and the controlled Rust restart regression.

Saved-conversation restart is now implemented for versioned retained Rust roots. The CLI accepts an exact --resume UUID; the PowerShell launcher forwards -DataRoot/-Resume and explicit backend/model. Python owns acquisition and resume. A correlated owned history read precedes Ready, saved thinking survives, and prior prompts are never resubmitted. Saved text/details render before new turns; richer native history, images, large-history paging and conversation browsing remain open. See the newest master checkpoint for process evidence and Python fixture baseline failures.

The latest checkpoint in [master.md](master.md) supersedes the historical implementation descriptions below. Live model and thinking selectors now use the owned management connection, source-derived choices, independent mutation/readback requests and actual effective settings. F3 opens models; Tab switches to thinking, and the thinking footer opens it directly. Only a fresh visible confirmation applies a choice. Pending verification keeps input owned; unknown readback outcomes stop the connection without replay.

The controlled process exercises actual source setters and owned persistence, then uses the selected values on the next controlled turn. Backend/engine switching, general settings and remaining management screens are not closed by this checkpoint. Automated rendering/contract evidence does not establish either-host acceptance or full parity. See the master checkpoint for exact test counts and baseline failures.

## Existing components to retain

Paths below are relative to this worktree.

| Source | Interface / ownership | Required migration treatment |
| --- | --- | --- |
| `src/litetui/cli.py` | `main()`; `--rpc`; launches `LiteTUI(...).run(headless=True)` | Spawn as the private runtime; retain its CLI validation and cleanup. |
| `src/litetui/rpc.py` | `_dispatch(app, cmd: dict) -> None`; `_respond(cmd_id, *, ok, result=None, error=None, needs=None)`; `rpc_emit(data)` | Reuse transport and command semantics; extend additively when inventory proves gaps. |
| `src/litetui/gui_rpc.py` | `dispatch(app, cmd)` and `async_dispatch(app, cmd)`; `PROTOCOL_VERSION = 1`; `OPERATIONS` | Authoritative management operations. Rust does not reproduce settings/tool/scheduler business rules. |
| `src/litetui/app.py` | `_submit_text`, `_execute_tool`, `_authorize_action`, `_stream`, `_rpc_emit`, `_shutdown`, `compose`, `BINDINGS` | Keep current authority/lifecycle methods behind headless host. Add semantic events where widget-only behavior is missing. |
| `src/litetui/turn_engine.py` | `TurnEngine` pure decisions including chat/compact request construction, tool accumulation, reasoning selection | Do not reimplement these rules in Rust. |
| `src/litetui/stream_sink.py` | `StreamSink` owns Textual timer/card, throttling, finish/cancel | Rust presents semantic text/reasoning/tool events, not Python widget mutations. Preserve final text and cancellation behavior. |
| `src/litetui/conversation.py` | `ConversationRepository` append-only records, replay, leases, staging/adoption | Python remains writer. Rust presents returned session/message data; never writes transcript JSONL directly. |
| `src/litetui/plugins/__init__.py` | `PLUGIN_LOAD_ORDER`, `PluginManifest`, `PluginRegistry`, `PluginContext`, `register_plugins`, `activate_plugins` | Keep per-runtime registration order, ownership, duplicate rejection and unload/reload semantics. |
| `src/litetui/settings_ui_model.py`, `settings_ui_adapter.py`, `settings_service.py`, `settings_scope.py` | Settings model/service/ownership rules | Reuse Python validation and requested/effective distinctions; inventory custom controls before assigning Rust widgets. |
| `src/litetui/paths.py` | `data_root()` reads `LITETUI_DATA_ROOT` | Set before imports; isolate all writable app state and audit anchors. |
| `src/litetui/input_controls.py`, `widgets.py`, `now_card.py`, `themes.py`, `side_panel.py` | Current terminal interaction/rendering references | Source for ported behavior, not libraries to call from Rust. |
| `src/litetui/tool_approval.py`, `ask_user_question.py`, `seat_authority.py` | Approval/question resolution and seat authority | UI responds to identified pending requests; Python alone grants/denies authority. |

## Proposed source layout

Presentation exception authorized 2026-10-15: `rust/litetui-terminal` now contains an offline preview (`demo.rs`, `ui.rs`, `main.rs`) plus tests and launcher. This does not implement the transport/runtime modules below. Ryan accepted its corrected structural layout, not an Ash-only palette; preserve full theming and use that layout for subsequent UI work. The semantic palette catalog and F4 picker now cover 36 public dark fixtures; custom editing/persistence/runtime catalog delivery remain open. See the master visual and offline-theme checkpoints.

Create one Rust crate, not a multi-crate framework:

- `rust/litetui-terminal/Cargo.toml`, `Cargo.lock` — binary `litetui-rs`, pinned dependency resolution; Ratatui, Crossterm, Serde/serde_json. Resolve compatible exact versions in P0/P1.
- `src/main.rs` — CLI, initial terminal state and runtime construction.
- `src/runtime.rs` — child process, stdout/stderr readers, command writes, shutdown/exit observation. Never interpret stderr as a protocol message.
- `src/protocol.rs` — typed command/event envelopes plus versioned capability dispatch; preserves unknown diagnostic payloads without executing them.
- `src/state.rs` — pure UI state reducer and correlation map, transcript/message/tool IDs, pending dialogs, disconnected/busy status.
- `src/terminal.rs` — Crossterm input, resize and terminal guard.
- `src/ui/mod.rs`, then cohesive feature files as needed — chat, composer, footer, palettes/dialogs/settings; no backend policy implementation.
- `tests/` — reducer, input, layout-buffer, lifecycle and actual-process integration tests.
- `tests/fixtures/rust_frontend/` at repository root — sanitized shared JSONL contracts used by Python and Rust tests.
- `tests/test_rust_frontend_contract.py`, `tests/test_rust_frontend_isolation.py` — proposed Python behavioral tests, not existing files.
- `e2e/rust_frontend_acceptance.py` — proposed acceptance runner; opt-in live paths and strictly isolated fixture roots.

Do not create dummy implementations for later screens. A missing inventory contract blocks that slice.

## Process, authority and data contract

Launch the configured Python executable directly with argument arrays, never a shell-expanded command string. Runtime module invocation is `-m litetui.cli --rpc`. Preserve existing CLI options for backend/model, named agent and launch profile. Use the worktree's installed package or explicit `PYTHONPATH=<worktree>\src` in development, not an editable install resolving to shared develop.

`--rpc` currently defaults to autonomous policy when no `--tool-profile` is passed. **The Rust launcher must not silently inherit that default:** pass the user's explicitly selected profile, subject to seat constraints; display the effective profile returned by the runtime. With no selection, prompt BEFORE spawning Python; no runtime may start until an explicit valid profile is selected. Add a negative test asserting no child, hook, job or plugin activation occurs when selection is absent or cancelled. Rust never infers a broader profile from a failed request.

Set `LITETUI_DATA_ROOT` before process start. Proposed installed default: `%LOCALAPPDATA%\LiteTUI-Rust\data`; development/e2e always use a unique temporary root. Refuse the default legacy root and the known running application's root in the new-launch path; test equivalent resolved paths, not only string inequality. Credentials may be deliberately configured through supported provider mechanisms but are not copied into fixtures/logs. “Fresh” is not a factory reset.

The private child owns runtime persistence and leases. Rust holds only presentation state and the command correlation map. Do not register a second fleet seat from Rust; the Python process is the runtime seat. Do not duplicate scheduler ownership. A second writer must receive the existing ownership refusal, not silently select another home.

Start with the headless Textual runtime. Do not undertake `_stream` extraction or remove Textual in this port without a separate reviewed scope change. Any terminal probes made by Python, including image setup, must not compete with Rust's input reader; prove behavior against redirected pipes and patch only the RPC path if required.

## Current wire mappings (observed in source, live fixtures pending)

Source evidence: gui_rpc.py:dispatch defines gui.hello, gui.state and gui.prompt.submit (accepted:true); rpc.py:_dispatch defines legacy prompt (turn:accepted), approve, abort and list_commands. These are distinct operations, not interchangeable response shapes. One UTF-8 JSON object per line on child stdin/stdout; drain stderr separately. Request IDs are correlation IDs, not approval/question target IDs.

| User action | Request / observed contract | Rust outcome |
| --- | --- | --- |
| Negotiate | `{"type":"gui.hello","id":"c1","protocol_version":1}` | Require successful response with `protocol_version`, `data_version`, `capabilities`, `operations`; incompatible runtime is an actionable stop. |
| Refresh state | `{"type":"gui.state","id":"c2"}` | Read `session_id`, `busy`, `mode`, `tool_profile`, `model`, `settings`, `conversations`, `jobs`, `tasks`, `ownership`, `active_work`, `usage`, pending dialogs. Do not invent missing data. |
| Submit text | `{"type":"gui.prompt.submit","id":"c3","message":"hello","behavior":"normal"}` | `accepted:true` is acceptance only. `behavior` is normal/queue/interrupt; keep submitted text associated with request/turn until actual completion. |
| Allow/deny | `{"type":"approve","id":"c4","approval_id":"<pending-id>","allow":false,"remember":false}` | `allow` and `remember` are booleans. Preserve exact target ID; stale/unknown ID is an error. Never resolve another pending card. |
| Stop turn | `{"type":"abort","id":"c5"}` | Show cancellation pending until state/event settles; response contains measured `stopped` and `cancelled_asks`. A transport ACK is not process termination. |
| Discover slash commands | `{"type":"list_commands","id":"c6"}` | Use actual registry metadata and existing `run_command` schema after P0 records it. Do not send slash text through `gui.prompt.submit`, which explicitly rejects it. |
| Settings view | `gui.settings.get` | `metadata`, `requested`, `effective`, `deferred`; render source/read-only/apply semantics. Successful save is not proof a reconnect-dependent value is active. |

Existing response envelope is captured by `rpc.py:_respond`; P0 must freeze its exact serialized shape in fixtures before writing Rust deserializers. Current semantic events include `turn_start`, `text_delta` with `text`, `turn_end` with `stopReason`, `tool_call` with `name`/`args`, `usage`, errors, approval/question requests and backend-specific events. This list is not exhaustive: inspect all emitting modules and record field shapes and operation/session correlation. Add missing fields additively and preserve current consumers.

Map `session_id` to `UiState.session_id`; `ownership.writable` gates editing; `busy` plus `active_work` drive activity indicators; pending request IDs key modal state. Map `text_delta.text` into the identified assistant block, not directly to terminal output. Escape/control-sanitize model text and tool output before rendering. `turn_end` settles only its matching turn, and final persisted transcript is the outcome oracle.

### Transport constraints to implement and test

- Concurrent stdout and stderr drains, byte-safe UTF-8 handling across arbitrary read boundaries; lines may be fragmented or several may arrive at once.
- Bounded memory. P0 measures representative largest frames (including images/history) and records a finite frame/queue limit with headroom. No inherited 1 MiB sidecar limit without evidence; no unlimited `read_line` accumulation. Oversize frames fail visibly and cannot corrupt subsequent session state.
- Coalesce adjacent text deltas only if content/order remain exact. Never drop tool results, approvals, completions or errors to relieve backpressure.
- Only an explicitly negotiated allowlist of diagnostic event types may be ignored/displayed without state changes. Any other unknown event, malformed required fields, wrong version or lost synchronization blocks further mutation/submission until explicit recovery. Never infer that an unknown event is informational.
- Use command IDs unique within a runtime generation. A timeout is an uncertain result, not permission to retry a side effect. Clear correlation state on child replacement; never replay pending prompts/tools/approval decisions automatically.
- On orderly exit, consult active work, use existing cancellation/shutdown paths, wait with a documented bounded grace period, then terminate only the owned child tree if necessary. Never kill shared model servers or unrelated agents. Test stderr flooding, broken stdin, child exit and refusal during teardown.
- Terminal guard restores raw mode, alternate screen, cursor and enabled input modes on normal exit and recoverable errors. Hard-kill recovery limits must be documented; do not promise a process can run cleanup after force termination.

## Visual and input specification

The committed Textual implementation, not a new mockup, is the design reference. No independently verified mockup is used.

Observed base layout, top to bottom: Header; NowCard; expanding ChatLog; conditional image-attached indicator; conditional skill autocomplete; PromptBox; ContextFooter. ChatLog takes full width, remaining height, cell padding 1/1 and single-cell scrollbars. Skill autocomplete sits above input, maximum total height 12 rows and list height 10, with two-cell horizontal margin and rounded primary border.

Observed user message layout: automatic height, top/left/bottom/right margins 1/8/0/2 (Textual source `margin: 1 2 0 8`), vertical/horizontal padding 1/2, rounded primary border, left-aligned title, primary-darkened background. Use theme-resolved colors, not arbitrary new hex values. P0 exports resolved palettes for supported themes before buffer snapshots are approved.

Observed compact behavior in `app.py:_apply_compact_profile`: manual override wins; entering initially uses width <100, noncompact subsequent entry uses <=93; remaining compact uses width <101. Port this hysteresis rather than introduce resize flicker. Test boundary widths 93,94,99,100,101 as well as 45,80,120,160 columns and small terminal heights. Remaining feature geometry and clipping rules must be filled from source and captured baseline screens in P0.

Preserve these observed bindings and their focus contexts: Escape stop-turn; Ctrl+Q quit; Ctrl+O paste-image; Ctrl+V paste; Ctrl+Shift+C copy selection; Ctrl+X clear image; Ctrl+L clear chat; Ctrl+T tools toggle; Shift+Tab authority cycle; Ctrl+P plan; Ctrl+G compact; Ctrl+B previous tool; Ctrl+E expand tool. Ctrl+J and Ctrl+Shift+Enter are alternate-send aliases. Do not assume Enter behavior: read `PromptBox`, input history and `_submit_text`, and test normal/newline/queue/interrupt behavior with the current settings.

P0 captures every additional binding, mouse action, modal focus trap, selection, Unicode width rule, bracketed paste behavior, autocomplete and history interaction. OS-terminal key interception is a measured host-specific constraint, not permission to silently change shortcuts.

## Plugin compatibility contract

Retain Python `PluginRegistry` per runtime and `PLUGIN_LOAD_ORDER`. `PluginContext` exposes tools, dynamic tools, commands/aliases, palette rows, prompt sections, observers, turn finalizers and activation. `unload(owner)` must remove all contributed capabilities, not only visible palette entries.

The current `extensions.list` surface is insufficient to describe arbitrary Textual screens or Python palette closures. P0 selects representative plugins and documents every frontend contribution. For each UI-dependent plugin, define the exact structured operation and screen model before adaptation; extend GUI RPC explicitly rather than serializing Python callables or exposing arbitrary `getattr`/evaluation.

Preserve existing command handlers and authority checks. UI source edits are permitted by Ryan. Unported UI contributions must be visibly unsupported and block release parity; never silently run a hidden Textual modal or call it equivalent. User-installed plugins absent from this checkout remain an explicit inventory gate requiring Ryan's specimens.

## Delivery phases

### [] P0 — Auditable baseline and contract freeze

1. Generate static inventory from tracked source: all GUI operations, RPC commands, plugin load order, Textual app/screen/widget classes and bindings, backend names and relevant tests. Reconcile with live navigation; static discovery alone cannot see dynamic plugins/settings.
2. For every user-facing row create `artifacts/rust-tui-port/parity.json` with fields: `id`, `source_revision`, `source_paths`, `entry_action`, `preconditions`, `request`, `response`, `events`, `state_owner`, `rust_surface`, `positive_check`, `negative_probe`, `terminals`, `status`, `evidence`. Empty unknown fields are allowed only while status is blocked; none at release.
3. Capture actual sanitized runtime fixtures for golden paths and negative cases using a deterministic backend boundary and real `LiteTUI`/RPC collaborators. Include text, reasoning, tool begin/end, approvals, questions, queueing, abort, errors and state refresh; do not use production transcripts.
4. Record current terminal appearance and interactions for both hosts; resolved theme palettes and all selected plugin screens. Store terminal dimensions, fonts/DPI, versions and screenshot references.
5. Benchmark Python baseline: time to first editable input (not first window), input-to-render latency while streaming, scrolling with 10,000 fixture messages, CPU idle/streaming, process-tree memory. Five launches plus three repeated steady-state runs per host; separate warm/cold results. Record hardware, fixture hash, provider boundary, terminal and background load. Shared model services reported separately to avoid counting unrelated server work.
6. Ask Ryan to approve numeric gates from those measurements. Pin crate versions and frame/queue limits with justification. T0565 process selection is resolved: Ryan chose **Tier 2 / High** and authorized execution after compaction.

Exit: reviewed parity inventory and fixtures, executable measurement method and complete specification for the next slice. If private plugin specimens or live access are missing, leave affected rows blocked. Do not assert that this initial planning pass completed P0.

### [] P1 — Terminal shell and real-runtime vertical path

Before pinning Ratatui, check `ratatui-rgp` version/API compatibility for the recorded later Ratty/RGP target; this does not authorize inline 3D implementation in P0/P1. Create crate and terminal guard; spawn actual headless runtime with isolated state and explicit authority. Implement hello/state, text submission, stream rendering and graceful exit. Offline fixture mode exists only for tests, clearly labeled; it cannot be the sole integration proof. Version mismatch and child-start failure leave a visible diagnostic and restore the terminal. First implementation test must fail without the binary/runtime integration.

### [] P2 — Complete conversation/input/safety experience

Port composer and shortcuts, transcript blocks, reasoning/tools, selection/scroll/compact behavior, queue/interrupt/cancel, questions/approvals, save/resume and error reporting. Retain Python persistence/authority and test denied execution by absence of the fixture side effect plus proof that its success control does create it. Prevent duplicate turn submissions at the UI boundary, and prove the reducer handles late events without editing the wrong conversation.

### [] P3 — Remaining full-parity features

Work from inventory, not a generic list. Required groups include all backend/model and engine controls; settings and themes; skills/slash commands; tools/MCP/hooks; conversations/memory; agent/fleet visibility; tasks/subagents/goals; jobs/calendar; diagnostics/telemetry; image staging/viewing; STT/TTS; custom plugins and reload. Each group requires concrete commands/events, source controls, state ownership and tests before dispatch. Wire all submissions to real runtime operations and assert their receiving state, not only invocation.

Images require verifying actual terminal capabilities and fallback behavior in both hosts. Voice requires actual selected provider/device tests, not only list population. A missing external service leaves the coverage pending rather than being replaced by an unconditional success stub.

### [] P4 — Parity, fault and performance gate

Run the inventory-driven acceptance matrix, process failure tests, existing affected Python suites and new Rust tests. Compare repeated measurements against Ryan's approved targets. Prove negative controls fail. Obtain visual signoff in both terminal hosts. Audit runtime startup/teardown and state-root writes with actual processes.

### [] P5 — Packaging and review

Package the frontend plus an explicit Python-runtime requirement or bundled runtime strategy selected after baseline. Test a clean install, missing Python, missing package, wrong protocol, upgrade of the new app's own fresh state and uninstall without touching the old app. Keep current Python entrypoint usable. Independent review then local branch completion; separate human authorization for merge/push/cutover. Do not remove worktrees automatically.

## Verification instruments

All proposed filenames below are **to be implemented**, not commands already available. Run from worktree root with its own package on `PYTHONPATH`. For script-style legacy files with module-level `sys.exit`, run `python tests/<file>.py`; do not blindly collect those with pytest.

| Check | Instrument / PASS outcome | Conditions | Negative proof |
| --- | --- | --- | --- |
| Existing boundary regression | `python -m pytest tests/test_gui_management.py tests/test_rpc_approval_types.py tests/test_rpc_terminal_output.py tests/test_turn_engine.py tests/test_stream_sink.py -q` exits 0 | Windows baseline; measured 95 pass once during planning | These existing passes are baseline evidence only, not newly demonstrated mutation sensitivity. New protocol tests below supply negative controls. |
| Wire/reducer contract | `cargo test --manifest-path rust/litetui-terminal/Cargo.toml --locked` and `python -m pytest tests/test_rust_frontend_contract.py -q` exit 0 | Sanitized identical fixtures, deterministic backend boundary, actual Python dispatch | Wrong protocol version, malformed required field, stale target ID, split UTF-8 and late-turn event each assert a specific rejection/outcome. Temporarily mutate expected outcome and require nonzero before trusting gate. |
| Root isolation | `python -m pytest tests/test_rust_frontend_isolation.py -q` exits 0 | Unique empty data root, real child, controlled legacy sentinel root, before/after tree hashes | Deliberately configure the forbidden legacy root: launch must refuse. Inject attempted write to sentinel and prove monitor fails. |
| Terminal safety | `python e2e/rust_frontend_acceptance.py --scenario lifecycle --terminal <host> --repeats 3` exits 0 | Windows Terminal and LiteSuite hosts separately; real executable; test-owned children only | Kill test runtime during stream, fill stderr, close stdin; require error state, no resend, restored terminal after frontend exit and no surviving owned child. |
| Functional parity | `python e2e/rust_frontend_acceptance.py --matrix artifacts/rust-tui-port/parity.json --require-complete` exits 0 | Both hosts, baseline revision recorded; all required capabilities available | Delete evidence or mark any required row skipped; runner must exit nonzero. Each feature row includes its own behavioral failure case. |
| Performance | `python e2e/rust_frontend_acceptance.py --scenario performance --baseline artifacts/rust-tui-port/python-baseline.json --thresholds artifacts/rust-tui-port/thresholds.json` exits 0 | Identical fixture/hardware/terminal/load; 5 launches, 3 steady-state runs; combined runtime tree | Inject artificial render delay and verify latency gate fails; missing thresholds/results must fail, not silently skip. |
| Quality | `cargo fmt --manifest-path rust/litetui-terminal/Cargo.toml --check`; `cargo clippy --manifest-path rust/litetui-terminal/Cargo.toml --all-targets --locked -- -D warnings`; affected Python `ruff`/`mypy` under project configuration | Locked dependencies and recorded toolchain; changed paths plus affected callers | Invalid format/warning fixture during gate validation; compare any Python type failure to untouched baseline before classifying pre-existing. |
| Human visual outcome | **MANUAL: Ryan** signs off recorded viewport interactions for each host | Same dimensions/fonts/theme, actual runtime; images/voice where required | Present deliberate missing approval/focus/overflow state to test capture visibility; do not accept screenshot without observed interactions. |

Existing neighboring suites to expand into by affected behavior: `test_rpc.py` (live cases require `LITETUI_E2E=1`), `test_rpc_isolation.py`, `test_rpc_jobs.py`, `test_rpc_tasks.py`, `test_gui_owned_storage.py`, `test_plugin_substrate.py`, `test_plugin_reload*.py`, `test_input.py`, `test_input_history.py`, `test_footer*.py`, `test_settings_ui_model.py`, `test_settings_ui_adapter.py`, `test_tool_approval.py`, `test_agent_ownership.py`, `test_owned_write_boundaries.py`, `test_stt_backend.py`, `test_voice_backend.py`, plus their feature-specific existing contracts. Inspect each file's runner and side effects before invoking. Live/provider tests need explicit resource/credential coordination and cannot be replaced with all-skipped results.

## Three acceptance scenarios

1. Launch Rust on a fresh owned root; choose a backend/model and policy; type multiline text, attach an image, receive incremental text/reasoning and a tool approval. Deny the first request and prove no fixture file appears; allow a second request and verify exact fixture file content and persisted tool result. Cancel a later turn and resume the saved conversation after restart without repeating either tool.
2. Open settings and change one immediate value, one reconnect value and one restart value. Observe actual effective state after required transitions. Invoke the selected custom plugin from both its command alias and palette row. Create a safe scheduled test job, observe its once-only execution result in task/calendar views, cancel future activity and verify runtime-owned process cleanup.
3. In each terminal host, stream a long response while navigating history, selecting/copying, scrolling and resizing across compact thresholds. Terminate the test Python child. Rust must display disconnection, retain recoverable input, refuse automatic side-effect replay and restore terminal modes on exit. Relaunch explicitly and show persisted state.

## Buildability and verification verdict at planning time

P0 is actionable from this document and named sources. The existing process seam and key request shapes are verified in code. Exact remaining UI schemas, plugin specimens, all events, theme palettes, terminal baselines, performance targets, and dependency versions are intentionally gated P0 outputs. **The entire parity implementation is not yet build-ready.** Do not substitute broad feature names for missing specifications or dispatch P3 before enrichment.

No application code, Rust build, live frontend test, failure-injection run or performance comparison was completed during planning. The recorded 95 existing Python test passes cover only the named baseline files. Documentation generation/link checks establish document integrity and nothing more.

## Review resolutions and additional P0 gates

Independent review received 2026-10-15. Authority selection is now explicitly before child spawn; unknown-event handling now fails closed except a negotiated diagnostic allowlist.

- **RPC startup:** reviewer inferred image probing necessarily corrupts anonymous-pipe RPC from cli.py ordering. Follow-up source read found `image_viewer.py:_probe_sixel_da1` checks both `isatty()` values and `init_image_backend` documents non-TTY fallback. Therefore corruption is NOT established. Make a real no-console subprocess startup test a P1 entry gate: first stdout bytes must parse as a protocol object, with no escape preamble, during stderr flood. Test dependency cell-size behavior too; fix only a reproduced RPC-path defect without regressing normal TTY image initialization.
- **Correlation:** P0 must trace every emitter through operation/session/turn/message identifiers. Missing stable identity blocks P2; add fields compatibly before implementing queue/interrupt/conversation-switch reducers. Test interleaved turns and late completions.
- **Plugin capability:** `rpc.py` heuristically detects terminal handlers via string inspection. Never treat `headless_ok` as a safety/parity guarantee. Require explicit structured UI contracts, default unknown UI handlers to unsupported, and test indirect/wrapped widget calls.
- **Filesystem scope:** `LITETUI_DATA_ROOT` is not a sandbox. P0 enumerates protected legacy roots and permitted external locations (provider credential/cache paths separately). Use a test-owned cwd and fixture tools; instrument attempted writes outside fresh state plus allowed fixture roots. Hashing only the new data directory is insufficient. Never exercise unconstrained production plugin tools merely to test isolation.
- **Windows process ownership:** before P1 freeze a Job Object/owned-process mechanism, nested-job compatibility, breakaway policy, timeout, and exclusions for shared model services. Verify test descendants including detached workers, and that shared servers are never killed. PID ancestry alone is not authority.
- **Terminal measurement:** before P1 establish a real LiteSuite PTY launch/control route and Windows Terminal capture route. The proposed `--terminal` runner is a desired instrument, not a discovered capability. Where automation is unavailable, require Ryan's named manual/semi-automated host procedure, explicit evidence records and separate process-resource instrumentation; the matrix checker must fail without those records. No screen-render timing claims from command-send timing alone.
- **External support matrix:** P0 records image capabilities, voice providers/devices and backend access required for each parity row. Missing access blocks the row. Any exclusion requires Ryan's explicit scope change; no implicit parity waiver.
- **Packaging:** choose configured-system-Python versus bundled runtime in P0 before P1 process assumptions are frozen. P5 validates that selected topology and its licensing, dependency/plugin discovery and update implications; it does not introduce a new topology after testing.
- **Dark presentation:** Ryan requested dark mode after the first render was bright. All generated plan pages now use actual Oscura CSS with explicit near-black background and `color-scheme: dark`; the browser computed body background is rgb(9,9,11), text rgb(250,250,250). Preserve this presentation preference in future plan renders.

## Implemented P0 wire evidence (2026-10-15)

The planned full frontend/reducer suite remains open. Its implemented in-process subset is `tests/test_rust_wire_contract.py` (18 tests), plus `tests/test_rust_wire_capture.py` (5 packaging tests). `scripts/capture_rust_wire_contract.py` captures production serialization through explicit test doubles, not a CLI child. See [runtime-contract.md](runtime-contract.md) for the merged ten-lane audit, actual evidence boundaries, current malformed-type repair and unresolved identity/isolation gates. No raw/live fixture is copied from user sessions.
