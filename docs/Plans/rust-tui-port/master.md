# LiteTUI Rust terminal port

**T0565 · v9 planning · single workstream · implementation not started**

## Provenance

- Created: 2026-10-15.
- Modified: [2026-10-15 initial plan].
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

2026-10-15: Ryan selected Tier 2 and High, then said “i want you to start this plan after compact”. Subsequently “go ahead bud” authorized continuing. **P0 is in progress; no Rust frontend exists yet.**

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
