# Remaining LiteTUI Rust parity work

Source audit: 2026-10-30, work/rust-tui-OpenBolt, prior checkpoint 1f14b8c6.
This is an implementation map, not release acceptance. The newest checkpoint in
[master.md](master.md) supersedes historical status in older plans. Claude
continuation and pre-start refusal handling are committed; bounded session browsing has
passed independent review and the full controlled process gate and portable staging has passed independent review.

## Current boundary

Rust has owned Windows Python startup, bounded transport and verified event mirrors,
captured execution origins, multiple text turns, reasoning/tools, cancellation,
approvals/questions, composer/transcript, model/thinking controls, static themes,
exact-UUID resume, held-input recovery, bounded F8 session browsing with preflight,
F9 backend switching, F10 budget editing, F11 command/skill controls and explicit
F12 NInfer/Strata engine management, plus reviewed F8 New/current Rename. Automated evidence does not replace
both-host acceptance. Source remains authoritative for execution and persistence.

Current anchors: live.rs Options::parse accepts the canonical nine startup backends; live_chat.rs
Chat::submit explicitly refuses slash commands; the source-backed command router handles
slash input separately and prepares skills for explicit attended submission. management.rs handles startup,
model/thinking and restored input; resume.rs still has bounded saved-history projection; conversation_browser.rs pages
the catalog and validates history before opening. Source gui_rpc.py OPERATIONS is authoritative.
Historical inventory.md checkboxes are discovery data, not current feature status.

## Independent implementation packages

| Package | Source authority / Rust surface | Dependencies and acceptance |
| --- | --- | --- |
| Sessions and history | gui.conversations list/read/open/create/rename/edit/retry/compact; new browser/history views | Catalog paging/open/preflight and current-session New/Rename are implemented; expand history/edit/retry next. Preserve draft, UUID/owner, settings and provider continuity. Add paging beyond 100 visible messages, native activity/image replay and failed-open/readback tests. |
| Backends and engines | llm_backend.py catalog/classes; gui.backend.set and gui.engine operations | Canonical startup flags and controlled generic-event paths are implemented. Guarded F9 backend/model switching and F10 settings inspection are implemented with controlled source/process validation. Conversation output-budget edits and explicit NInfer/Strata start/stop are implemented. Engine configuration, second-instance confirmation, other settings edits and actual installed-service/inference acceptance remain. Preserve owned-vs-shared lifecycle and persistent connect-only restrictions. |
| Settings and models | Settings service/scope/runtime; gui.settings, context, model discovery/reconnect/load/unload/configure/confirm | F10 requested/effective inspection and guarded conversation output-budget edits are implemented; broader editing and lifecycle controls remain. Independent readback after mutation; failed persistence/reconnect cannot imply effective change. Existing model/thinking picker is partial coverage. |
| Work management | gui.jobs/tasks/goals/calendar/work | Jobs/calendar, task output/cancel and goal views can use separate files. Disposable owned jobs must prove actual single execution, cancellation and cleanup. |
| Plugins and commands | PluginRegistry and command/palette contributors; extensions/skills/tools operations | Live source catalog, native palette, verified slash routing and exact prepared-skill submission are implemented. Unsupported plugin screens/actions remain visible with reasons. Use structured adapters for Textual-dependent UI; no arbitrary eval/getattr bridge. Preserve registration/unload/reload ownership and inventory private plugin specimens. |
| Hooks and MCP | gui.hooks/mcp and host-tool operations | Exact source schemas, independent controls, persistence/readback, reload/disconnect failures. Current startup forces external MCP off; enabling it needs authority and lifecycle tests. |
| Memory, agents, diagnostics | gui.memory/system/diagnostics/glassbox/subagents and fleet contributors | Bounded read views first, then source-owned writes/cancellation with exact own-session identities and missing-process cases. |
| Images and voice | images.attach/clear, screen.mark, staging/viewer and listen/STT/TTS UI | Clipboard image, indicator, clear-image priority, inline/fallback rendering and replay. Voice requires real selected provider/device tests. Both hosts need capability evidence; current gaming constraint precludes desktop interaction. |
| Input, themes, footer | Input controls, PromptBox/bindings, themes/settings, footer/NowCard | Remaining shortcuts/modes/autocomplete/history/copy-selection, custom tokens/persistence/preferences and navigation. Source compact hysteresis, Unicode, paste and focus ownership per feature. |
| Packaging | New stage-package.ps1/package-launch.ps1 and Python project metadata | Tracked runtime/resources only, external Python explicit, relocated verification. Clean dependency environment, binary/source provenance, upgrade/uninstall and release review remain. |
| Fault and resource acceptance | Full-feature controlled process tests; runtime-contract.md P4 protocol | Complete fault/cleanup coverage. Measure Python against Rust + retained Python + owned descendants under equivalent workloads before Ryan approves numerical targets. |
| Host acceptance | Windows Terminal and LiteSuite embedded terminal | Layout, shortcuts, images, clipboard, scroll/resize and terminal restoration in both; Ryan's visual signoff. Preview approval is not full feature acceptance. |

## Parallel waves

1. Current: bounded owned browser and canonical backend startup are implemented
   with controlled-process evidence. Continue native history/session actions,
   backend/settings/engine controls and the independent screen/plugin lanes.
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

## Session actions and remaining history work

F8 New and current-session Rename now use source-owned native_snapshot,
native_validate and native_apply contracts. Pending Rename explicitly materializes
before saving; New joins provider/lifecycle work, preserves old durable history,
stages its reviewed UUID and rebinds settings/system/task state. Captured hooks obey
existing policy, with actual terminal settlement retained through cancellation.
Native independently rereads state/history/held input/current catalogue before
publication. Quit/EOF joins the action before storage release; unresolved cleanup
cannot produce a successful exit acknowledgement. The master checkpoint and
runtime contract describe the evidence and precise shutdown limits.

Direct /new and /rename slash routes remain unsupported and should open the same
reviewed native controls through canonical command resolution. Do not reintroduce
legacy mutable handler dispatch. Larger history, rich replay and edit/retry remain
separate implementation waves.

Legacy edit/retry truncates live history, saves a snapshot and calls _submit_text,
then returns accepted:true without checking admission. It cannot serve as a native
success receipt. Provider-native history/thread continuity, durable edited history,
normal attended admission, execution identity, refusal and uncertain publication
must be handled together; retry must never silently duplicate provider work.

History paging needs a distinct bounded source projection, not a larger unbounded
transport frame. Current catalog/open guards allow at most 100 visible messages,
1,000 total messages, an 8 MiB source transcript and a 1 MiB preview, while native
retained/display budgets are enforced independently. Preserve those bounds per
page, bind pages to a stable owned transcript revision, and keep full source model
context independent of the visible window. Rich replay must interpret source
message/activity/image shapes instead of presenting their JSON as faithful UI.

## Next contracts identified by source audit

Settings edits must use the canonical SettingsService scoped revision checks and
settings_runtime.apply_saved_result. Legacy gui.settings.apply invokes
_on_settings_saved, which can change live settings before persistence and trigger
loads/reconnects. The implemented bounded editor covers conversation output budgets
with explicit draft/validate/confirm/save/readback; disable those fields when a
launch max_tokens override still owns actual requests or when the native provider
owns output budgets. Canonical scopes/timing and backend controls own eligibility.

Engine controls now negotiate explicit management-only readiness for stopped
NInfer/Strata services, with F12 inspected/reviewed start and retained-owned stop.
Frozen plans bind source snapshots, actual execution paths and bounded header/config
file evidence. Canonical lifecycle/resource gates remain authoritative; this is not
full artifact hashing or completed production resource calibration. Second-instance
confirmation currently returns an explicit refusal. Runtime backend/session and
legacy lifecycle reconfiguration require an explicit reopen while this capability
exists. Broader configuration and resource-confirmation controls remain to port.
Quit joins in-flight actions, stops retained ownership and returns structured
shutdown evidence; hard-timeout containment does not establish resource release.

Command/skill metadata must derive from the live PluginRegistry. Reserve every
command alias when resolving skill shadowing; existing completion only reserves
primary names. Native adapters may open already verified controls but cannot invoke
arbitrary Textual screen handlers. Existing gui.skills.use drops arguments and
bypasses normal prompt admission. A skill adapter must bind the exact discovered
file and digest (case and folder-alias resolution can otherwise change bodies),
limit the prepared UTF-8 prompt and enter normal owned prompt admission.

Prepared skills use separate lossless draft storage because ordinary composer insertion
flattens CR/LF/TAB and has a scalar cap. The implementation preserves full SKILL.md
content, canonical directory binding and exact arguments, bounds the complete UTF-8
prompt, and submits only through explicit normal attended admission. A file digest
binds the freshly prepared revision; a metadata catalog alone does not bind file bytes.
Existing transcript/display limits still apply and can refuse a large preparation.
The native skill path does not invoke legacy gui.skills.use.

## Theme persistence identified by source audit

F4 currently selects one of 36 generated public dark palettes in native memory;
Demo always starts from Ash. Live source selection is separate: the themes plugin
registers Textual/LiteTUI/custom definitions and applies settings.theme_name.
LiteTUI's palette is intentionally independent of LiteSuite's; do not re-sync it.
A runtime catalogue should derive from the actual registered source themes, include
valid custom themes, and expose requested versus resolved/effective selection.

The existing export_rust_preview_themes.resolved_colors function is a pure semantic
mapping into the native fourteen-color Theme shape. Extract/reuse this mapping for
live resolution instead of copying Textual color derivation or reading private live
settings in the fixture generator. Preserve optional thinking/tool/footer overrides,
whole source-defined dark catalog order, and explicit source refusal/skipping of
invalid definitions. Runtime theme bytes need bounded names/rows/tokens and escaped
rendering; a catalogue row alone is not a saved-selection acknowledgement.

Settings classify theme_name and custom_themes as device-scoped and immediate.
Native saves should use SettingsService revisions and actual device destination,
then independent saved/effective catalog readback. App.watch_theme catches save
OSError and keeps an in-memory selection: observing that property does not prove
persistence. Generic apply_saved_result updates custom_themes but does not register
new definitions or repaint a changed theme with the same name. Share the canonical
registration/refresh seam before claiming a new or edited palette is effective.

The source editor trims a nonempty name, creates or overwrites that exact key and
selects it. Ten core #RRGGBB tokens are required; optional thinking/tool values fall
back when missing/invalid and footer overrides are omitted when invalid. Use the
single THEME_FORM_TOKENS schema and show resolved values, including fallback, in a
separate explicit draft/review. Keep this work independent of provider/model
settings, authority and the ordinary chat draft. Real host/clipboard/color-picker
acceptance remains separate from controlled token and persistence tests.
