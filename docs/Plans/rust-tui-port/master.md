# LiteTUI Rust terminal port

**T0565 · Tier 2 / High · P0 in progress · offline Rust visual preview available**

## Provenance

- Created: 2026-10-15.
- Modified: [2026-10-15 initial plan; 2026-10-15 P0 discovery; 2026-10-15 approved visual-preview direction; 2026-10-15 offline theme integration; 2026-10-15 parallel P0 contract audit].
- Agents: [OpenBolt].
- Conversations: [83816ba3-d370-4c4f-b0f7-022cc4b4074d].
- Source revisions: [LiteTUI develop 9ef5337].
- Worktree: `C:\Projects\.worktrees\OpenBolt-rust-tui`; branch `work/rust-tui-OpenBolt`.
- Back references: Ryan's request, “port litetui to rust”; v9 questionnaire in the conversation above; T0565.
- Forward references: [sub-terminal.md](sub-terminal.md), [inventory.md](inventory.md).
- Append future metadata entries; do not replace the provenance lists.

## Outcome and confirmed scope

Replace the visible Python/Textual terminal UI with Rust while retaining the Python runtime temporarily. Improve responsiveness, reliability, terminal interaction and distribution, with a later path to removing Python. These are goals, not measured results.

Ryan chose:

1. Full current UI feature parity before switching over—not a chat-only MVP labeled complete.
2. Familiar layout and shortcuts, without a simultaneous redesign.
3. Windows first; both LiteSuite's embedded terminal and Windows Terminal are acceptance environments.
4. Fresh separate application data. No legacy import requirement; no deletion or mutation of the running installation's state.
5. Preserve custom plugin behavior; allow adapting or porting Textual-dependent plugin UI source.
6. All current backends, approvals, permission boundaries, cancellation and agent ownership remain in scope.
7. Baseline the Python version first, then agree numerical performance targets. Include both processes and runtime-owned child processes in reported resource measurements.
8. One workstream with sequential delivery gates. No completion estimate before the feature inventory is reconciled.
9. No silent replay of commands/tool side effects after a crash or ambiguous result.
10. Python remains a required dependency during this phase. A Python-free executable is a later project milestone.

Final assumption check: Ryan selected **All correct**. Initial next-step authorization: **Plan it**. Subsequently Ryan selected Tier 2 / High and explicitly authorized: **“i want you to start this plan after compact”**. Begin P0 after compaction, without repeating the planning quiz. This document does not claim authorization to publish, push, replace the live installation, or run paid/live backend tests without coordination.

## Architecture decision

**Rust owns terminal input, layout, rendering, navigation and presentation state. Python remains authoritative for providers, tools/policy, approvals, storage, scheduling, agent lifecycle and plugin logic.**

Use the existing private child-process `python -m litetui.cli --rpc` JSONL/stdin/stdout interface and `gui.*` management operations as the migration seam. Do not use the settings-only native sidecar as the main chat bridge. Do not introduce a network listener merely for local frontend/runtime communication.

The existing `--rpc` process runs `LiteTUI.run(headless=True)` and still creates Textual objects. Retaining that internal headless host is deliberate during the UI-first migration; it is not a finished runtime extraction and may limit early memory improvements. No legacy Textual screen is to replace a required Rust screen in the parity release.

Proposed Rust implementation: one crate at `rust/litetui-terminal/`, using Ratatui, Crossterm and Serde/serde_json. The implementer must resolve and lock versions compatible with the measured Rust 1.92 toolchain before coding. Use a bounded transport queue, dedicated child-I/O readers, an explicit event reducer and a terminal cleanup guard. Keep layout and provider logic separate without introducing a speculative plugin ABI or rewriting providers.

## Measured source facts and their limits

- `cli.py:main` supports `--rpc`, and calls `app.run(headless=True)` after capturing protocol stdout and redirecting other stdout to stderr.
- `rpc.py:_dispatch` routes `gui.*` to `gui_rpc.async_dispatch`; older command names include prompt, answer, approve, abort and command discovery.
- `gui_rpc.py:OPERATIONS` and `PROTOCOL_VERSION = 1` provide a substantial management surface, not a full typed UI protocol.
- `app.py:compose` lays out Header → NowCard → ChatLog → image indicator → SkillAutocomplete → PromptBox → ContextFooter.
- `sidecar_dispatch.py:SettingsPatchDispatcher` is a settings/jobs bridge using the Textual owner thread, not an independent chat service.
- Plugin command/palette handlers may call arbitrary `ctx.app`/Textual APIs. Existing RPC discovery is not sufficient evidence of custom-plugin UI parity.
- `paths.py:data_root()` honors `LITETUI_DATA_ROOT`; it must be set before Python imports. Isolation of every feature still needs behavioral proof.
- Rust/cargo 1.92.0 are installed. No Rust crate was implemented or built during planning.
- Focused existing Python checks: **95 passed in 15.35 seconds**, one run on Windows, on baseline 9ef5337. Files: `test_gui_management.py`, `test_rpc_approval_types.py`, `test_rpc_terminal_output.py`, `test_turn_engine.py`, `test_stream_sink.py`. This is not live frontend or full-suite verification.

## Workstream and dependency gates

[] **WS1 — Terminal replacement:** [builder specification](sub-terminal.md).

`P0 inventory + baseline + wire fixtures → P1 process/transport + first vertical path → P2 chat/input/approvals → P3 all feature screens + plugins → P4 Windows parity, fault and performance evidence → P5 package/review/cutover`

These are internal milestones, not independently shippable parity releases. P0 is executable now. Later phases have explicit entry gates; do not dispatch a screen migration until its inventory row contains the exact source, request/response/event schemas, UI behavior, tests and evidence requirements. This is a gated implementation plan, not a claim that every feature contract is already fully specified.

## Release acceptance

[] Every audited baseline feature and required custom plugin has a passing parity row, no unaccounted operation, and evidence in both required terminal hosts where UI behavior differs.

[] Approval rejection, stale approval IDs, denied actions, malformed protocol, child death, cancel-while-waiting, repeated submissions and shutdown each have negative-path evidence. A response ACK alone never establishes a completed operation.

[] Fresh-data isolation includes settings, ownership, conversations, schedules, hooks, plugin state and generated files. The old installation remains unchanged.

[] Rust formatting, Clippy and tests pass; affected Python behavioral tests, lint and applicable type checks pass or have a documented baseline-only failure. No unrequested `run_all.py` run.

[] Python/Rust resource baselines are measured under identical workloads, with numerical gates agreed by Ryan after the baseline. No “faster” claim without comparable evidence.

[] Packaged Rust frontend launches with the documented Python runtime and missing-runtime errors are actionable; no claim of a standalone Python-free binary.

[] Ryan visually accepts the familiar layout, shortcuts, image behavior, scrolling, clipboard and terminal restoration in both terminal hosts. Machine checks accompany but do not replace this signoff.

The sub-plan names instruments, conditions, failure demonstrations and expected outcomes. Planned instruments do not yet exist and must not be reported as passing.

## Three golden paths

1. **Conversation and authority:** launch with fresh owned state and explicit policy, select a backend/model, send multiline text, see incremental answer/tool state, deny one tool and allow a separate safe fixture tool, cancel a later turn, restart and resume the new conversation with the same persisted outcome.
2. **Management and plugins:** edit a setting with immediate/reconnect/restart semantics, see requested versus effective state, invoke a custom plugin command and palette action, execute a scheduled safe fixture task once, see its actual result in tasks/calendar, then stop it without leaving an owned child running.
3. **Failure and terminal recovery:** stream while resizing and scrolling; terminate only the test runtime, see a disconnected/error state and no automatic resubmission, exit Rust and observe normal input/cursor/mouse modes restored in each terminal. Reopen explicitly and recover persisted data without re-running tools.

## Open gates and ownership

- OpenBolt/implementer: exhaustive live UI inventory, plugin specimen selection, wire schemas and resource baseline.
- Ryan: approve numerical performance targets after measurement; identify any private custom plugins not present in the committed source; visual acceptance.
- Resolved: Ryan chose **Tier 2 / High**; recorded on T0565. Execution authorized after compaction.
- Orchestrator: execution staffing/priority and review; SOTS remains the stated fleet priority.
- Live verification requires available backend credentials/services and terminal access. Missing coverage blocks full parity; skipped tests are not passes.
- Full buildability gate: **not yet passed for all screens**. Missing dynamic UI/event/plugin specifications are P0 outputs, not license for placeholders.

## Execution workflow

1. Use the already provisioned isolated worktree. No edits to shared develop, links/junctions, force operations or worktree deletion. Local commits only; repository is public.
2. Write contract/behavior tests and demonstrate the intended failure first.
3. Implement one coherent slice in the single workstream, using the actual Python runtime at integration boundaries.
4. Diagnose failures before adding more changes; preserve cancellation/error details.
5. Verify commands and visible outcomes under recorded conditions; retain failure-probe results.
6. Obtain independent review of protocol, safety, process lifecycle and whole diff.
7. Finish the local branch only after acceptance. Publishing/merging, live cutover and worktree removal require separate authorization.

## Planning artifact checks

`python Docs/Plans/rust-tui-port/render.py --check` verifies generated HTML and artifact copies match their Markdown sources, and local plan links resolve. This checks documentation integrity only, not product behavior.

Planning outputs are maintained under `Docs/Plans/rust-tui-port/` with synchronized finished copies under `artifacts/rust-tui-port/`. Open `master.html` for the human-readable view.

## Execution echo

After implementation, record which contracts were missing, which failure probes caught real faults, whether plugin adaptation preserved behavior, and whether the combined runtime actually improved the measured experience. Do not turn an unmeasured prediction into retrospective fact.

## Presentation incident and standing rule

Sentinel confirmed on 2026-10-15 that dark-only was already a standing user requirement, not a new preference. The first light plan render violated it. Root cause: the generator matched a prose `<style>` mention rather than the CSS block. Correction: repository-owned `plan.css`, explicit dark color scheme/background, and computed-style verification. Future pages must be checked offscreen before being shown; a source stylesheet or successful HTML generation alone is not proof of a dark rendered page.

The committed generator uses `markdown_it` from the project Python environment. Run it from the worktree root; generated copies under `artifacts/` remain local.

## Execution authorization update

2026-10-15: Ryan selected Tier 2 and High, then said “i want you to start this plan after compact”. Subsequently “go ahead bud” authorized continuing. **P0 is in progress. An offline Rust presentation preview now exists; the real runtime is not connected.**

## P0 checkpoint — reproducible source discovery

Implemented `scripts/rust_frontend_inventory.py` and `tests/test_rust_frontend_inventory.py`. The generator reads exact regular-file blobs at the approved revision with Git `ls-tree` / `cat-file --batch`, never application imports. It fingerprints inputs and the generator, records Python version, source spans and qualified symbols, and reports unsupported discovery forms rather than executing them. Working/untracked files and export-ignore/export-subst cannot alter the selected committed blobs.

Measured discovery: **262 source modules; 84 GUI operations; 15 legacy RPC commands; 33 ordered plugins; 9 backends; 24 BINDINGS declarations; 325 class candidates; 38 direct emitter-call sites; 600 test-file candidates**. All classes are candidates, including non-UI classes: unresolved inheritance is not proof of a complete screen census. Test discovery is not test execution. Prefer this exact-blob JSON for reproducible P0 evidence; the original Markdown inventory remains the earlier heuristic planning view.

```powershell
python scripts/rust_frontend_inventory.py --output artifacts/rust-tui-port/static-inventory.json
python scripts/rust_frontend_inventory.py --output artifacts/rust-tui-port/static-inventory.json --check
# First creation only: refuses any existing parity file.
python scripts/rust_frontend_inventory.py --output artifacts/rust-tui-port/static-inventory.json --seed-parity artifacts/rust-tui-port/parity.json
```

Generated JSON remains local/ignored. The parity seed contains **528 blocked discovery rows**, unknown runtime-contract fields, no outcome evidence, and both terminal hosts not run. Reconcile/filter/group these into actual user-facing behaviors before treating them as feature rows. Regeneration never replaces the parity ledger. Static output replacement is atomic and refuses non-static evidence. If seed creation fails after static publication, refreshed static evidence remains and existing parity is untouched; the command reports failure.

Checks measured on Windows: **44 focused tests passed in 25.02 seconds** across `test_rust_frontend_inventory.py`, `test_test_inventory.py`, `test_run_all_classify.py`, `test_explicit_test_inventory.py`; focused Ruff and mypy (`--check-untyped-defs`) passed. The initial missing-module failure and six review-regression failures were observed before implementation/fixes. Independent lightweight code review found no remaining material defects after fixes; this is not the separate blind intent-ledger gate.

**P0 remains open:** no live startup, terminal interaction, performance baseline, Rust build or backend parity is established. A fresh data directory does not isolate hooks, fleet registration, plugin activation or provider traffic. Source `app.py:5730-5749` emits both raw events and correlated `gui.event` wrappers after GUI negotiation; the frontend must avoid double-rendering, and queue/interrupt correlation still needs a complete audit.

## Recorded later target — Ratty/RGP inline 3D

Sentinel relayed Ryan's 2026-10-15 ~20:0x EDT goal: “we had it running fine ! it was sick ... almost like our litesuite canvas now :) was my dream for it”, referring to `orhun/ratty`, a GPU terminal emulator with RGP inline 3D and the `ratatui-rgp` widget crate. This is a **later-phase target, not added P0 implementation or an existing-parity requirement**.

Keep Ratatui dependency choices compatible with `ratatui-rgp`: verify actual supported versions and Windows capability negotiation before locking the frontend dependencies. Do not promise compatibility from crate names alone. Later work should investigate an optional Ratty host path, inline 3D display and graceful unsupported-host fallback while preserving the mandatory LiteSuite embedded-terminal and Windows Terminal acceptance paths. No RGP implementation, clone edits, GPU workload or new UI redesign is authorized by this P0 checkpoint.

Prior-art pointers supplied by Sentinel, not independently reverified here: `E:\SAS\REPO_CLONES\ratty` (Windows ConPTY/RGP work); Ryan's PR #66 reportedly merged upstream and #68 OSC-envelope-for-Windows reportedly closed unmerged on 10-11; conversation search phrase **Ratty LiteCode 3D Terminal Buddies**. Recover that design and measure the current crate/protocol contract before scheduling the later slice.

## Visual-preview checkpoint — accepted direction, not runtime parity

Ryan explicitly requested an openable GUI even with a mock conversation to inspect tool calls. This authorized an isolated presentation-only slice before the real-runtime P1 gate. The crate now exists at `rust/litetui-terminal`, with a locked Ratatui/Crossterm build, deterministic in-memory fixtures, tool expansion/selection, scrolling, a local mock composer and a mock approval that defaults to Deny. No Python child, provider, tool execution, real authority change, application persistence or fleet registration is present.

Open `rust/litetui-terminal/open-preview.cmd`, or run `rust/litetui-terminal/target/debug/litetui-rs.exe --preview` in an interactive terminal. The launcher builds only if the executable is missing; rebuild with Cargo after edits. The window is explicitly dark and fixture-labeled. `artifacts/rust-tui-port/preview-ash.png` is a local crop of the observed Windows Terminal render.

**Visual reference is the corrected layout, with Ash merely the currently selected theme.** Ryan rejected the initial generic rounded/teal tool cards as unlike LiteTUI, supplied a screenshot, then said “yes way better” and “lets continue with that in mnid as the GUI” after the correction. Preserve this direction: borderless black tool rows, amber names, inline arguments/status/duration/character counts, gray rounded assistant frames, nested dashed thinking blocks, theme-driven colors and familiar composer. Ryan explicitly clarified that LiteTUI has many themes: preserve built-in themes, the picker, custom tokens and custom-theme behavior; do not hardcode Ash as the product palette. Do not redesign the structural layout during runtime wiring. Source anchors: `themes.py:184-229`, `app.py:747-891`, `widgets.py:744-780, 965-1033, 1070-1198`. The current saved theme was read as `ash`; only theme keys were inspected. No live settings were changed.

Measured: Rust 1.92.0; Ratatui 0.30.2; Crossterm 0.29.0; ratatui-core 0.1.2. Upstream Ratty's `widget/Cargo.toml` currently names its widget `ratatui-ratty` 0.4.0 (the earlier plan called it `ratatui-rgp`), depends on core 0.1.2, and uses Ratatui 0.30 / Crossterm 0.29 in examples. This aligns the dependency families; it is not an RGP integration test. No GPU/Ratty dependency is included.

Initial preview verification: **16 Rust tests pass**, `cargo fmt --check`, Clippy `--all-targets --locked -- -D warnings`, and build pass. Tests include compact modal visibility, hidden-modal decision suppression, completed-frame dark colors, wide glyphs, actual TestBackend resize sequences and the source-derived tool/thinking visual structure. A compact modal clipping regression was reproduced before repair. The running preview was seen in Windows Terminal; Ryan accepted the revised visual direction. This is not full interaction signoff, embedded-terminal evidence, performance measurement or backend parity. Speech labels are explicitly mock, and real voice behavior remains unimplemented.

Next: retain this presentation while completing event correlation, sanitized wire fixtures and controlled startup isolation. In particular, choose the negotiated GUI event stream without double-consuming its raw-event mirrors. No real child may launch without explicit authority and isolated test state. The existing P0/P1 gates remain in force for real runtime integration.


## Offline theme checkpoint — semantic palettes, not live theme parity

The preview now consumes 36 public dark palettes exported reproducibly from `src/litetui/themes.py` and Textual 8.1.0. `scripts/export_rust_preview_themes.py --check` checks the committed fixture. The exporter never imports LiteTUI/App or reads saved/custom settings. `theme.rs` defines semantic presentation tokens; `theme_catalog.rs` is generated public data. Ash remains the default, not the only supported palette.

F4 opens a searchable picker. Up/Down selects, Enter applies in memory, Escape cancels. Replay preserves the selected palette; picker input cannot submit a mock turn or resolve an approval. Theme selection pauses below the minimum viewport. Narrow layouts retain essential controls/state, and long searches show their display-width-aware tail. Synthetic thinking/tool/footer overrides reach the renderer; actual custom-theme creation/loading/editing and persistence remain runtime parity gates.

Verification: **26 Rust tests passed** (including all 16 existing preview checks), formatting, Clippy with warnings denied, and build. **5 exporter tests passed**, focused Ruff and mypy passed (`MYPYPATH=src`, `--follow-imports=silent`). The existing Python theme suites reported **23 passed, 4 failed** when run alone, unchanged against HEAD: `test_watch_theme_persists_a_change` supplies a non-dataclass settings stub; three tests in `test_theme_extra_tokens.py` construct LiteTUI without an owned agent session. Adding the exporter tests yields 28 passed / the same 4 failures. These fixtures were not repaired or counted green in this slice.

The searchable picker and Ash-to-Matrix switch were observed in Windows Terminal; screenshot crop `artifacts/rust-tui-port/preview-matrix.png` is local/ignored evidence. The preview was returned to Ash. This is not embedded-terminal verification or new human signoff. Lightweight static review found compact header/footer and long-query clipping; these were reproduced with failing buffer tests and repaired. Real runtime, persistence, custom-theme UI and remaining P0 gates are still open.


## Parallel P0 contract checkpoint — no live-runtime cutover

Ryan requested max-parallel execution with every child on GPT-6.1 Sol. Ten independent High-reasoning source audits ran in parallel, followed by a Sol synthesis and focused review. Their merged contract and remaining gates are in [runtime-contract.md](runtime-contract.md). Reports are source evidence, not executed parity proofs.

Implemented `tests/test_rust_wire_contract.py`: real JSONL reader, dispatcher, serializer, GUI negotiation, app event helpers and approval futures with explicit inert boundaries. No LiteTUI construction or live provider. **18 wire tests passed; 42 focused tests passed**, including five capture-packaging tests and existing relevant contracts. Nine synthetic serialized cases are exported by `scripts/capture_rust_wire_contract.py` into a unique run; hash-bound `completed.json` is published only after full pytest teardown succeeds. Current completed evidence: `artifacts/rust-tui-port/wire-captures/run-499a6635da0a404dbb3c7e46a6495da1/completed.json`.

The audit found a real malformed-command crash: non-string `type` reached `.startswith`. Five regression cases reproduced it before a narrow RPC guard fixed it; the next valid request still dispatches. This is the only Python product behavior change in the checkpoint. New-file Ruff and capture-runner mypy pass. The existing RPC module retains six Ruff and three mypy findings, independently reproduced against its exact pre-change blob; they were not hidden or repaired as unrelated cleanup.

Captured evidence confirms raw-plus-GUI event mirrors, refusal before accepted ACK, nullable operation identity/rates, separate approval-target and correlation IDs, and stale/duplicate rejection. No full turn identity, real-child isolation, subprocess transport, resource baseline, embedded-host parity or performance claim follows. Next gate: complete producer/queue/native correlation and lifecycle mapping; prove owned-startup containment before launching the real runtime. No inventory/parity rows were automatically promoted.

### Offline Rust wire implementation — subsequent checkpoint

The Rust library now implements incremental bounded JSONL decoding and versioned negotiation, command correlation, mirror selection and fail-closed uncertainty handling. Ten decoder tests and seventeen client tests supplement the existing preview/theme/input tests. Review-driven regressions cover exact numeric preservation, literal-object/type preservation, and duplicate-key depth validation. This code is **not wired to the preview or a live runtime**. The next producer/capture/containment gates remain open; see section 9 of [runtime-contract.md](runtime-contract.md) for implementation boundaries and active tool-policy blockers. The separately passing real-owned construction probe still needs fixture hardening and is not a completed containment gate. Full parity remains incomplete.

## Offline composer checkpoint — editing state, not full input parity

The preview now owns a scalar-indexed UTF-8-safe composer with insertion, selection, directional deletion and prompt-history draft restoration. Focused-input keys follow the inspected Textual bindings: Ctrl+A/Home, Ctrl+E/End, Ctrl+Shift+A/select all, Up/Down/history; F2 expands tools and PgUp/PgDn/wheel scroll the transcript. Whole-grapheme rendering preserves wide glyphs during horizontal scrolling and visually snaps internal scalar caret positions to their trailing edge. Confirmation repeats cannot submit the preserved draft after closing a modal; ordinary typing/movement repeats remain enabled. This depends on the terminal identifying repeat events.

Verification: **72 Rust tests passed**, Cargo formatting, all-target Clippy with warnings denied, and build. Oversized-grapheme caret underflow (rendered as a temporary replacement glyph without altering text), wide-glyph clipping, interior-grapheme caret and modal-repeat regressions were observed failing before repair. Windows Terminal previously showed cursor insertion (`aXlpha 雪 omega`), local mock submission and restored draft text; `artifacts/rust-tui-port/preview-composer.png` is a local crop. The final review-driven renderer/repeat repairs have automated evidence only. **Ryan closed the preview; it remains closed.** No new desktop interaction or runtime launch was used for these fixes.

Limits remain preview-only (500 characters / 100 history entries). Word editing, clipboard, autocomplete, source paste-event parity, footer navigation and runtime integration remain open. The existing P0 capture/containment policy blockers remain active; none of these mock tests promotes a real-runtime parity gate.

### Offline word-editing continuation

Ctrl+Left/Right (and shifted selection), Ctrl+W and Ctrl+F now implement Textual's word-start navigation/deletion, including existing-selection precedence. The routed regression was observed failing before implementation. The generated word table targets **Python 3.11.9 / Unicode 14.0.0**, not arbitrary runtime Unicode versions. An offline exporter produces the Rust table and 110 prefix/suffix cursor cases; tests validate the ranges against Python regex for every Unicode scalar and validate the oracle against mounted Textual 8.1.0 Input actions. Rust replays these cases with both selection orientations. No LiteTUI constructor, saved settings, real clipboard, desktop input or runtime was used.

Verification: **75 Rust tests passed**; **8 focused Python tests passed** (word exporter/oracle and existing theme exporter); fmt, all-target Clippy, Ruff, exporter mypy, generated-output check and build passed. Review found no word-logic defect; its missing Ctrl+Shift+Right routed coverage was added and passed. Unicode-version negotiation, OS clipboard, autocomplete, footer navigation and runtime gates remain open. Preview remains closed.

### Offline slash-completion presentation

A non-modal picker now filters source-ordered candidate snapshots, ranks prefixes before substring matches, caps results at 200, and takes Up/Down before history. Tab replaces the whole draft with the selected name plus a space; Escape dismisses until text changes; Enter still submits the typed draft. Click acceptance uses rendered row offsets and matching press/release on one row; borders, stale filters, hidden views and covered lists cannot accept. The preview catalog is three explicitly mock entries and cannot dispatch anything. Real discovery, alias/collision deduplication and live catalog delivery remain producer gates; Rust lowercase filtering is not claimed equivalent across Python Unicode versions.

The source-resolved primary token now drives the rounded picker border across all 36 themes. Resize tests retain selected rows and command/skill labels; a VS16-heart name overflow was reproduced red and repaired with whole-grapheme clipping. Review also identified missing mouse selection; rendered-offset click routing and invalidation tests were added. Picker-specific wheel scrolling remains open.

Verification: **87 Rust tests passed**, formatting, all-target Clippy, build, **8 focused Python exporter/oracle tests**, theme generated-output check, focused Ruff and exporter mypy. This is automated presentation evidence only: preview stays closed, no desktop action or real-runtime launch. Existing LiteTUI-construction-based slash-command tests were not used as a substitute for the still-open owned-construction gate.


### Bracketed-paste event contract

A minimal Textual App mounting the actual source PromptInput established that focused bracketed Paste consumes the first splitlines line, replaces its selection and stops before the application handler. The Rust route now matches that first-line/selection contract, including all Python line separators, empty events and empty first lines. Its prior multiline-join behavior was reproduced failing before repair. Preview control sanitization and caller limits remain explicit differences; the separate OS Ctrl+V path is not implemented and no system clipboard was touched. Approval/hidden-input guards and completion refresh are tested.

Verification: **91 Rust tests passed**, **9 focused Python tests passed** (source paste probe, word oracle, theme exporter), fmt/all-target Clippy/build and focused Ruff. The source probe constructs only a minimal Textual App and PromptInput, not LiteTUI or a runtime. The preview remains closed; full parity and runtime gates remain incomplete.


### User-triggered clipboard boundary — compiled, not live-host verified

Ctrl+C now copies selected composer text via Textual-style OSC52 base64, or explains Ctrl+Q when there is no selection; it no longer exits. This routing was reproduced red before repair and measured in a minimal source PromptInput host. Ctrl+V is a distinct guarded OS-text operation: CRLF/CR normalization and nonblank-line joining insert at the active cursor without deleting selected text. The actual source _insert_into_input method is tested against a minimal widget host without constructing LiteTUI. The native Windows boundary uses a clipboard ownership guard and checks the Windows text allocation against an explicit caller-owned 1MiB preview cap before conversion. Non-Windows reads report unsupported; no fallback subprocess is supplied. Missing text/errors preserve drafts; modals and small viewports do not call the boundary.

Verification: **97 Rust tests passed**, **11 focused Python tests passed**, fmt/all-target Clippy/build and focused Ruff; static High review found no blockers. Tests use fake clipboard boundaries and pure OSC52 encoding; **no native clipboard read/write or terminal copy sequence was executed during verification**. Native behavior and host acceptance remain unverified while the preview stays closed. Internal clipboard cache, transcript copy, image actions, fallback/cross-platform support and full runtime parity remain open. Source Ctrl+X is priority clear-image, not cut; the preview does not silently assign cut semantics to it.


### Footer layout and focus — fixture projection, not runtime transitions

Implemented separate status/permission rows with source compact/drop order, grapheme-safe seat shortening and fail-closed overflow. Configured GLOBAL navigation order is preserved, restricted to actually rendered actionable fields; no row-major redesign or unknown-authority default is introduced. Selection keeps a stable chip identity. Completion/history precedence, typing/paste exit, independent row budgets, invisible selected-chip removal, inert intent notices and selection styling across all 36 themes are covered. Mock help moved to the composer border.

The initial Down/Enter draft-submission regression was reproduced red and repaired. High review found Down repeat toggling, modified Enter leaking through focus, and an oversized first grapheme defeating the seat minimum; each was reproduced red and repaired. An Arabic Lam-Alef TestBackend regression independently demonstrated that whole-string width undercounted Ratatui's rendered cells; layout now sums rendered grapheme widths. These controls do not mutate authority or plan state. Visible-only navigation, overflow and stale-Enter handling deliberately tighten source inconsistencies; other source semantic colors, meters/toggle, normalization/visibility producers and mouse actions remain open.

Verification: **116 Rust tests passed**, fmt, all-target Clippy with warnings denied, and build. The preview stayed closed; no desktop interaction, native clipboard access, constructor or runtime launch occurred. Full runtime/parity gates remain incomplete. Sentinel relayed Ryan's deny-floor ruling: no classifier changes; use ordinary native read/edit/write tools for artifact/source operations and stop any sub-step those tools refuse. That clarification does not itself establish constructor containment or runtime readiness.

### Owned-construction/storage gate — narrow in-process evidence

The native file channel accepted the previously blocked test-source hardening without any deny-floor change or canonical-settings write. `tests/test_rust_owned_construction.py` now observes partial instances before constructor delegation, registers session cleanup immediately, and independently releases store/client/backend/session through AsyncExitStack while tripwires remain installed. A durable append-before-raise ledger detects swallowed prohibited attempts. Explicit MCP activation denials replace inherited silent no-ops; socket DNS/connect/connect_ex, HTTP, subprocess, provider discovery and live seat-registration boundaries are guarded. Suite home/data/configuration redirections are acknowledged rather than mistaken for an unmodified environment.

Tests exercise real agent ownership, constructor, backend/client creation, transcript materialization/readback, repeated materialization, conversation exclusion and separate agent/conversation leases. Thread events force overlap in the real registration orchestration while the transport is a deterministic stub, covering both success and refusal/no-retry/reacquisition. Partial-constructor, three cleanup-failure and swallowed-attempt selftests are included. Those tests initially caught a cleanup bug: `store` is a lazy property backed by `_store`, so looking for `vars(app)['store']` skipped repository cleanup. Corrected to `_store`; the client is closed and the agent lease can be reacquired after each tested failure.

Verification: **9 gate tests + 39 existing launch-context tests = 48 passed**, focused Ruff passed; High static review found no blockers for this narrow claim. Cleanup-failure injection raises AFTER each real cleanup succeeds: this establishes independent callback execution, not recovery from a release that itself fails early. Final filesystem assertions can mask the ledger in diagnostics while still failing the gate. Clean-import containment, mounted lifecycle, live transport, child process/OS sandbox, provider readiness, registration cancellation/transport exceptions, and injected persistence failures remain unproven. No runtime or preview was launched. The Python capture manifest is now readable through native read; Rust replay was still open at this gate checkpoint.

### Python-produced output replay — nine streams, not a live child

Added portable output-only imports of all nine synthetic cases from `run-d2912ff6c98d47f182ea4bbbc130a073`, totaling 31 JSONL records. Native text tools/Windows checkouts can represent LF as CRLF, so the replay reconstructs only LF framing and verifies each entire stream against its ORIGINAL output SHA-256 from the completed index before decoding. All nine hashes match. This proves byte identity of the replayed streams, not physical working-tree files or integrity of the omitted input/manifest/log bundle. No source capture rerun, shell enumeration, runtime startup, or classifier change was needed.

The Rust decoder receives every stream whole, at every two-part byte split and in single-byte chunks. Selected semantic assertions cover repeated event pairs, escaping, operation IDs, explicit nulls, direct errors, approval-response order and refusal-before-ACK. The WireClient uses the literal captured IDs: `fixture-hello` does not match its generated IDs, cannot consume a pending generated hello or activate compatibility. Cold replay produces the expected provisional/unmatched/uncertain/quarantined sequence. No positive correlation is manufactured, no traffic-derived allowlist is installed, and no ACK is treated as execution success.

Verification: **122 Rust tests passed**, fmt, all-target Clippy with warnings denied, build; High static review found no blockers for the output-only scope and compared all nine pins with the original index. SHA-256 is a pinned dev-only dependency (`sha2=0.10.9`, cached/offline). The original run's `18 passed` is historical Python evidence, not eighteen exported captures or a new test run. Full bundle preservation, payload schema/reducer, production delivery inventory, process/fd lifecycle, execution provenance and real-child integration remain open. Preview remains closed.

### Producer envelope event identity — additive metadata only

`LiteTUI._rpc_emit` now allocates a canonical UUID4 `gui.event.event_id` once per produced envelope, before either raw or wrapped sink write. Existing session and operation attribution is also captured before fanout. Legacy payloads, including their own `event_id` fields, remain unchanged. Equal consecutive emissions receive different envelope IDs. This identifies an emission, not an execution, admission, turn, successful delivery or retry policy; it does not repair mutable execution attribution.

Two guarded real-constructor tests cover distinct IDs and a sink that changes session/operation state between writes. The existing wire-contract assertions validate envelope UUIDs before comparing unchanged legacy shapes. A Rust decoder/client regression preserves absent IDs from old producers, distinct new IDs and repeated replay IDs without introducing deduplication. Historical capture files and their original hashes are unchanged.

Verification: **107 focused Python tests and 123 Rust tests passed**; focused test Ruff, Rust fmt, all-target Clippy with warnings denied, and build passed. Full `app.py` Ruff has the same 112 code/message findings as HEAD; no new findings. Advisory `mypy src` remains red (357 diagnostic lines, none in the changed emitter range; no full baseline comparison claimed). Preview stays closed. Mounted lifecycle, immutable execution provenance, process transport/reducer and full parity are still open.

### Bounded mounted runtime — queued input through real orchestration

`tests/test_rust_mounted_runtime.py` mounts the real owned app and exercises discovery/readiness orchestration, hook selection/authorization/feed/drain/verdict parsing, GUI admission, the generic stream/TurnEngine path, a held GUI host tool, round-boundary queued delivery, and transcript readback. A is still the active operation while B queues; the second model request has the paired tool result before exactly one B input. Real admission hooks receive distinct hook IDs, and the final envelope remains attributed to operation A under the existing contract. No new execution provenance is implemented by these tests.

The fixture uses an explicit temporary workspace, owned session and suite-isolated home/data/config. The selected catalog loads real `core_tools` and `self_compact`; optional plugins, RPC stdin-reader startup and updater startup are excluded. Backend HTTP, registration, model transport and hook process pipes are scripted. No real hook executable, provider service, live child or preview is launched by this fixture. The real model-transport request implementation is substituted, so this is not evidence for its HTTP/JIT controls. Default thinking avoids probes.

Cleanup keeps transport tripwires through unmount, owned-work settlement and independent resource callbacks. Context-tagged asyncio tasks and observed Textual workers are checked, including work born during unmount. Hook outcomes and cleanup failures are recorded separately from forbidden attempts. An active body/settlement/cleanup exception plus verifier findings is propagated as an exception group: an expected boundary assertion cannot hide an unrelated failure. Task-factory replacement is reported and another owner's factory is not overwritten.

Ten fixture cases cover startup, the queued turn, body failure while the host tool is held, swallowed DNS, simultaneous DNS/cleanup failure, a late teardown task, failed shutdown-hook observation, combined denial/body and denial/settlement failures, and task-factory replacement. **136 focused Python tests passed**, including existing hook, queue, GUI management and ownership tests; focused Ruff passed. High re-review cleared the targeted outcome-masking and task-factory findings. Cleanup/settlement fault injections occur after their real operation succeeds; they do not prove recovery from a release or task that never settles. Context-tagged task observation is not general OS, arbitrary timer/thread, full-plugin or clean-import containment. The preview remains closed; immutable execution origin, native producer coverage, runtime process transport/reducer and full parity were still open at this checkpoint.

### Explicit execution origin for selected generic stream output

Admission now returns a frozen `EventOrigin` through result-bearing helpers while retaining the existing Boolean wrappers. The synchronous `_stream` scheduler and `_stream_inline` coroutine factory allocate a fresh invocation UUID before the asynchronous body can run. The Textual worker retains its `_stream` name, chat group, exclusivity and error policy. Owner-driven compaction uses the inline entry with its accepted origin; direct entries without one capture only their session and do not borrow stale operation/hook/source fields.

Only generic `turn_start`, `text_delta`, `reasoning_delta` and `turn_end` emissions receive explicit execution metadata. Their envelopes retain the originating session, operation and hook across queued admission B; legacy raw payloads are unchanged. Tool/native/usage/refresh/retry callbacks remain unattributed by this increment. There is no mutable active-execution slot, ContextVar fallback, event deduplication, replay policy or universal late-callback claim. Rust's bytewise transport preserves explicit fields, null input identity and subsequent legacy envelopes without inventing missing provenance.

Verification: **570 Python tests passed; 39 failed with the exact same failing node IDs reproduced against archived HEAD e973279**. The 11 new origin cases cover queued A/B output with reasoning, immutable scalar normalization, immediate capture for scheduled and inline entries, deferred-worker behavior, pre/post-admission lifecycle waits, and actual generic success/error execution through both entries. The broader run includes existing admission, authority, native steering, owner compaction, queue, wake, stream settlement and usage contracts; boundary doubles were updated without removing behavioral assertions. The baseline failures remain failures, not a green full-suite claim. **124 Rust tests passed**, plus fmt, all-target Clippy with warnings denied and build. New-module/test Ruff and origin-module mypy pass; app/hook Ruff retains the same 112 code/message findings. Full-source mypy remains red (312 errors versus 313 at HEAD, with no new error signatures); it is not a clean type-check claim.

High static review found no blockers for this scope. Admission metadata is captured when its coroutine begins, before its first suspension, not when the admission worker is enqueued; execution capture is synchronous at `_stream` / `_stream_inline`. Documentation is rendered and checked with `render.py --preserve-inventory` (plus `--check`) so updating progress does not regenerate historical inventory evidence.

This is a bounded provenance increment, not full runtime integration or 1:1 port completion. Native producers, tools and late callbacks, payload schema/projection, bounded delivery queues, process lifecycle containment and full UI parity remain open. No live runtime or preview was launched.
