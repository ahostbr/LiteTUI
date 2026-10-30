# Remaining LiteTUI Rust parity work

Source audit: 2026-10-29, work/rust-tui-OpenBolt, continuation base d821a158.
This is an implementation map, not release acceptance. The newest checkpoint in
[master.md](master.md) supersedes historical status in older plans. Claude
continuation and pre-start refusal handling are committed; session browsing is
under integration validation and portable staging has passed independent review.

## Current boundary

Rust has owned Windows Python startup, bounded transport and verified event mirrors,
captured execution origins, multiple text turns, reasoning/tools, cancellation,
approvals/questions, composer/transcript, model/thinking controls, static themes,
exact-UUID resume and held-input recovery. Automated evidence does not replace
both-host acceptance. Source remains authoritative for execution and persistence.

Current anchors: live.rs Options::parse accepts Claude/Codex only; live_chat.rs
Chat::submit explicitly refuses slash commands; management.rs handles startup,
model/thinking and restored input; resume.rs has bounded saved-history projection,
not session browsing/pagination. Source gui_rpc.py OPERATIONS is authoritative.
Historical inventory.md checkboxes are discovery data, not current feature status.

## Independent implementation packages

| Package | Source authority / Rust surface | Dependencies and acceptance |
| --- | --- | --- |
| Sessions and history | gui.conversations list/read/open/create/rename/edit/retry/compact; new browser/history views | Source connection/history barrier and recovered-input policy first. Preserve draft, UUID/owner, settings and provider continuity. Add paging beyond 100 visible messages, native activity/image replay and failed-open/readback tests. |
| Backends and engines | llm_backend.py catalog/classes; gui.backend.set and gui.engine operations | Source-derived startup/event contracts before lifting the two-backend gate. Custom, LM Studio, llama.cpp, NInfer, Strata, Cline and Free remain. Distinguish owned engine processes from shared services. |
| Settings and models | Settings service/scope/runtime; gui.settings, context, model discovery/reconnect/load/unload/configure/confirm | Native requested/effective/reconnect/restart UI. Independent readback after mutation; failed persistence/reconnect cannot imply effective change. Existing model/thinking picker is partial coverage. |
| Work management | gui.jobs/tasks/goals/calendar/work | Jobs/calendar, task output/cancel and goal views can use separate files. Disposable owned jobs must prove actual single execution, cancellation and cleanup. |
| Plugins and commands | PluginRegistry and command/palette contributors; extensions/skills/tools operations | Slash/palette/skills routing through source handlers. Structured adapters for Textual-dependent UI; no arbitrary eval/getattr bridge. Preserve registration/unload/reload ownership and inventory private plugin specimens. |
| Hooks and MCP | gui.hooks/mcp and host-tool operations | Exact source schemas, independent controls, persistence/readback, reload/disconnect failures. Current startup forces external MCP off; enabling it needs authority and lifecycle tests. |
| Memory, agents, diagnostics | gui.memory/system/diagnostics/glassbox/subagents and fleet contributors | Bounded read views first, then source-owned writes/cancellation with exact own-session identities and missing-process cases. |
| Images and voice | images.attach/clear, screen.mark, staging/viewer and listen/STT/TTS UI | Clipboard image, indicator, clear-image priority, inline/fallback rendering and replay. Voice requires real selected provider/device tests. Both hosts need capability evidence; current gaming constraint precludes desktop interaction. |
| Input, themes, footer | Input controls, PromptBox/bindings, themes/settings, footer/NowCard | Remaining shortcuts/modes/autocomplete/history/copy-selection, custom tokens/persistence/preferences and navigation. Source compact hysteresis, Unicode, paste and focus ownership per feature. |
| Packaging | New stage-package.ps1/package-launch.ps1 and Python project metadata | Tracked runtime/resources only, external Python explicit, relocated verification. Clean dependency environment, binary/source provenance, upgrade/uninstall and release review remain. |
| Fault and resource acceptance | Full-feature controlled process tests; runtime-contract.md P4 protocol | Complete fault/cleanup coverage. Measure Python against Rust + retained Python + owned descendants under equivalent workloads before Ryan approves numerical targets. |
| Host acceptance | Windows Terminal and LiteSuite embedded terminal | Layout, shortcuts, images, clipboard, scroll/resize and terminal restoration in both; Ryan's visual signoff. Preview approval is not full feature acceptance. |

## Parallel waves

1. Current: bounded source session browsing, native open/readback integration and
   controlled-process evidence; remaining backend startup options and fixtures.
   Portable package staging is reviewed; release acceptance is still open.
2. Disjoint contract/view lanes: sessions; settings/backends/engines;
   jobs/tasks/goals/calendar; plugins/commands; hooks/MCP; memory/agents/diagnostics;
   images/voice; input/themes/footer. One integrator owns shared management.rs,
   live.rs, demo.rs and ui.rs per wave.
3. Real-source controlled integration for each operation, complete parity rows,
   focused Python/Rust lint/types/tests, package and dependency checks.
4. Both-host comparisons, process-tree measurements, fault/environment/upgrade
   acceptance, independent synthesis/review and Ryan's visual/numerical signoff.
   Merge/push/cutover remain separate authority.

## Portable staging

These scripts require PowerShell 7 (pwsh.exe); Windows PowerShell 5.1 is unsupported.
Build separately, then stage a supplied executable:

    ./rust/litetui-terminal/stage-package.ps1 -Destination C:/packages/litetui-rust-test -Executable ./rust/litetui-terminal/target/release/litetui-rs.exe

Verify a relocated package without Python, Cargo or runtime startup:

    ./launch.ps1 -VerifyOnly

Run with the intended Python environment and explicit model:

    ./launch.ps1 -Python C:/path/to/venv/Scripts/python.exe -Backend claude -Model <selected-model>

The manifest binds exact file bytes, base Git revision and tracked runtime
modifications. The supplied binary is hashed but is NOT asserted to have been built
from that source revision. Hash checks detect corruption, not authenticity.

Staging refuses an existing destination; incomplete output is retained for diagnosis.
It includes tracked package data and root helper tools while excluding credentials,
conversations, agent state, venvs, caches and untracked drafts. Python/dependencies
remain external and are described by the staged pyproject.toml/uv.lock.

Data defaults to a unique retained directory under local app data, or an explicit
external DataRoot. Launch preserves the Rust authority picker unless ToolProfile was
explicit, and forwards Tools/Workspace/Resume/Timeout. It does not initialize or
rewrite a resumed root. Python bytecode writing is disabled for the owned child.

Browser screenshots, relay PID/logs/default shots and the direct desktop screenshot
helper defaults (Python and PowerShell) now follow LITETUI_DATA_ROOT when configured.
Unset standalone helpers retain their legacy locations. Controlled tests invoke these writers against synthetic
images/sockets/process boundaries and AST-only PowerShell path functions, then reverify
the unchanged staged package afterward. Launch rejects linked ancestors of either the
package or DataRoot, including fresh child paths beneath a junction.
This closes the concrete immutable-package reopen failure; broader clean-environment,
release provenance and full-parity acceptance remain open.
