# Remaining LiteTUI Rust parity work

Source audit: 2026-11-01, work/rust-tui-OpenBolt, prior checkpoint 28e608ba9.
This is an implementation map, not release acceptance. The newest checkpoint in
[master.md](master.md) supersedes historical status in older plans. Claude
continuation and pre-start refusal handling are committed; bounded session browsing has
passed independent review and the full controlled process gate. Reviewed host-history edit/retry is implemented and independently reviewed; its final integrated/package evidence is recorded in master.md.

## Current boundary

Rust has owned Windows Python startup, bounded transport and verified event mirrors,
captured execution origins, multiple text turns, reasoning/tools, cancellation,
approvals/questions, composer/transcript, model/thinking controls,
exact-UUID resume, held-input recovery, F6 bounded saved-history inspection,
bounded F8 session browsing with independent saved-row inspection and captured Open,
F9 backend switching, F10 budget editing, F11 command/skill controls and explicit
F12 NInfer/Strata engine management, reviewed F8 New/current Rename, F6 host-history edit/retry and source-owned F4 themes. Automated evidence does not replace
both-host acceptance. Source remains authoritative for execution and persistence.

Current anchors: live.rs Options::parse accepts the canonical nine startup backends; live_chat.rs
Chat::submit explicitly refuses slash commands; the source-backed command router handles
slash input separately and prepares skills for explicit attended submission. management.rs handles startup,
model/thinking and restored input; history.rs validates compact context descriptors and inert pages; history_view.rs owns
the exclusive current/saved-conversation inspector; conversation_browser.rs pages the catalog, selects read-only inspection and separately reviews captured Open. Source gui_rpc.py OPERATIONS is authoritative.
Historical inventory.md checkboxes are discovery data, not current feature status.

## Independent implementation packages

| Package | Source authority / Rust surface | Dependencies and acceptance |
| --- | --- | --- |
| Sessions and history | gui.conversations list/read/open/create/rename/edit/retry/compact; new browser/history views | Catalog paging/open/preflight and current-session New/Rename are implemented. Current-conversation paging, inert rich replay and captured Open are implemented with controlled source/native/package evidence. Alternate-conversation/read-only inspection preserves active context and uses exact-UUID refresh. Reviewed current host-history plain-text edit/retry preserves canonical compaction/wake and joins owned workers. Manual compaction, native-provider rewind, transaction-aware prewrite hook approvals and background-result continuation remain. Preserve draft, UUID/owner, settings and provider continuity. |
| Backends and engines | llm_backend.py catalog/classes; gui.backend.set and gui.engine operations | Canonical startup flags and controlled generic-event paths are implemented. Guarded F9 backend/model switching and F10 settings inspection are implemented with controlled source/process validation. Conversation output-budget edits and explicit NInfer/Strata start/stop are implemented. Engine configuration, second-instance confirmation, other settings edits and actual installed-service/inference acceptance remain. Preserve owned-vs-shared lifecycle and persistent connect-only restrictions. |
| Settings and models | Settings service/scope/runtime; gui.settings, context, model discovery/reconnect/load/unload/configure/confirm | F10 requested/effective inspection and guarded conversation output-budget edits are implemented; broader editing and lifecycle controls remain. Independent readback after mutation; failed persistence/reconnect cannot imply effective change. Existing model/thinking picker is partial coverage. |
| Work management | gui.jobs/tasks/goals/calendar/work | Jobs/calendar, task output/cancel and goal views can use separate files. Disposable owned jobs must prove actual single execution, cancellation and cleanup. |
| Plugins and commands | PluginRegistry and command/palette contributors; extensions/skills/tools operations | Live source catalog, native palette, verified slash routing and exact prepared-skill submission are implemented. Unsupported plugin screens/actions remain visible with reasons. Use structured adapters for Textual-dependent UI; no arbitrary eval/getattr bridge. Preserve registration/unload/reload ownership and inventory private plugin specimens. |
| Hooks and MCP | gui.hooks/mcp and host-tool operations | Exact source schemas, independent controls, persistence/readback, reload/disconnect failures. Current startup forces external MCP off; enabling it needs authority and lifecycle tests. |
| Memory, agents, diagnostics | gui.memory/system/diagnostics/glassbox/subagents and fleet contributors | Bounded read views first, then source-owned writes/cancellation with exact own-session identities and missing-process cases. |
| Images and voice | images.attach/clear, screen.mark, staging/viewer and listen/STT/TTS UI | Clipboard image, indicator, clear-image priority, inline/fallback rendering and replay. Voice requires real selected provider/device tests. Both hosts need capability evidence; current gaming constraint precludes desktop interaction. |
| Input, themes, footer | Input controls, PromptBox/bindings, themes/settings, footer/NowCard | Live source theme selection, custom editing and persistence are implemented. Remaining shortcuts/modes/autocomplete/history/copy-selection, other preferences and navigation. Source compact hysteresis, Unicode, paste and focus ownership per feature. |
| Packaging | New stage-package.ps1/package-launch.ps1 and Python project metadata | Tracked runtime/resources only, external Python explicit, relocated verification. Clean dependency environment, binary/source provenance, upgrade/uninstall and release review remain. |
| Fault and resource acceptance | Full-feature controlled process tests; runtime-contract.md P4 protocol | Complete fault/cleanup coverage. Measure Python against Rust + retained Python + owned descendants under equivalent workloads before Ryan approves numerical targets. |
| Host acceptance | Windows Terminal and LiteSuite embedded terminal | Layout, shortcuts, images, clipboard, scroll/resize and terminal restoration in both; Ryan's visual signoff. Preview approval is not full feature acceptance. |

## Remaining session-action work

F6 host-history edit/retry now uses source-issued selection/review tokens, exact message and prefix hashes, canonical admission before durable publication, captured worker ownership and independent history readback. It does not call the legacy GUI edit/retry handlers or infer authority from displayed text. Source-owned automatic/self-compaction, card summaries and maintenance wake streams are tracked through final settlement.

Codex resumes a native thread and Claude uses a durable native segment; neither can be rewound by slicing the display transcript. Their edit/retry actions stay explicitly ineligible until exact provider-native rewind/branch contracts exist. Manual compaction needs its own reviewed host/Codex/Claude algorithms with typed outcomes, progress, provider continuity and cleanup/readback.

Prewrite hooks that need a new approval refuse before mutation because the current approval audit writes the old user row. Transaction-aware prewrite approval audit remains separate work. In-turn and compaction approvals/questions are supported. Background-result delivery can create held input or wake another turn, so explicit background tools and tools configured for auto-promotion currently refuse before hooks or tool effects. Foreground backgroundable tools with auto-promotion disabled remain supported. A later protocol must preserve background delivery, held-queue ownership and wake continuations before lifting that limit.

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

Canonical /new, /clear and /reset routes now open the reviewed New control;
/rename can prefill the same bounded editor. They invoke no mutable handler and
never apply from resolution alone. Current-conversation paging and inert rich replay
and reviewed host-history edit/retry are implemented. Manual compaction and
provider-native rewind remain separate implementation waves.

Legacy edit/retry truncates live history, saves a snapshot and calls _submit_text,
then returns accepted:true without checking admission. It cannot serve as a native
success receipt. The new host-history protocol binds durable publication, canonical
admission, execution identity, refusal and independent readback together. Provider-native
history/thread continuity remains separate; retry must never silently duplicate provider work.

The version-1 history implementation is verified in controlled source/native and
relocated-package gates. It uses a distinct
source projection and exclusive F6 inspector, with authenticated cursors, stable
UTF-8 fragments and separate raw/context, projection, index and wire bounds.
Whole-context preservation proofs replace visible-row equality in startup, F8,
F9, F12 and New/Rename. Full source model context remains independent of the
resident page. Historical native activity, reasoning, plans and summaries are
inert; image and question notices cannot execute or fetch anything. The old
100-visible/1,000-total limits remain only in legacy protocol compatibility.
F8 previews retain reviewed Open effects and readonly drift refuses mutation;
alternate-conversation/read-only history navigation remains future work.

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

## Theme parity and remaining presentation work

Live F4 now reads actual registered dark themes and custom overrides, while the
36 generated public palettes remain offline fixtures. Selection and custom edits
use canonical device/immediate persistence, normalized token review, explicit save
and independent saved/effective palette readback. Shared registration handles
same-name repaint without another legacy save or unrelated provider/voice changes.
The source watcher saves only its theme-name field. Dynamic shadows, trimmed-name
collisions, optional fallbacks and historical invalid selections are accounted for.

Opaque native RGB cannot faithfully represent alpha, ANSI or automatic colors;
unsupported definitions remain labelled, and source-bound metadata permits an
explicit supported replacement or same-name repair. Real terminal color fidelity,
clipboard/color-picker behavior, remaining footer/input preferences and both-host
acceptance remain separate work. Neither controlled TestBackend rendering nor
source CSS checks establish Windows Terminal/LiteSuite visual acceptance.
