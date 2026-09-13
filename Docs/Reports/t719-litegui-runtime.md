# T719 — LiteGUI runtime interfaces and shared ownership

Owner: RustAxis, worker, `01a096ad-0296-7c82-94a9-4f596dfb57b2`. Branch: `feat/t719-litegui-runtime`. Worktree: `C:/Projects/LiteTUI/.worktrees/feat-t719-litegui-runtime`. Original base: `9735a96`. Review base after the separate census and shutdown corrections: `928a9b7`. Sentinel owns review and merge; this report does not claim a release or Ryan's real-backend acceptance.

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


### Coordinated MCP writes and dependency baseline

The existing MCP add/remove read-modify-write now uses the same cross-process configuration lock. The test `test_two_process_mcp_edits_preserve_both_servers` starts two real Python children, holds the first after its read, and lets the second attempt its edit. Before the fix only the first server survived; after the fix both declarations survive. It never starts an MCP server or model. Malformed hook recovery uses the existing exact-byte repair conflict guard, so a concurrent external edit is retained rather than overwritten.

Sentinel directed the pre-existing kill-tree census correction onto a separate branch inside this same worktree. Commit `05b1f655` changes only that census test, explicitly permitting the two cancellable providers and rejecting additions. Its seven named tests passed **68 passed in 20.41s**, with Ruff **3 before / 3 after**. Sentinel independently reported 68 passing tests and merged it first. T719 was then rebased cleanly onto that commit, and later onto T723 merge `928a9b7`. The T723 `_shutdown` settling override and `side_panel` changes are unchanged. T719 releases the conversation lease after the existing `on_unmount` hook drain. No application change was hidden in the census correction.

### Final disk-derived regression scope

The original 156-file list remained unchanged. Under Sentinel's explicit final GO, one runner executed that set plus `tests/test_queued_input_delivery.py`, `tests/test_mcp_timeout_bounds.py` and the new dependency guard `tests/test_shutdown_prune_guard.py`. The final union contains **159 named files**. Only `test_footer_fields.py` is excluded. The six guard files are included in the original list. This is not a full repository suite.

Selection expression (followed by the explicit guard files and named extras):

```text
gui_rpc|shared_state|\brpc\b|ConversationRepository|\bconvo_settings\b|\bsettings_mod\b|settings_screen|\bllm_backend\b|\brouter_record\b|\brow_store\b|\bpccontrol_tool\b|\bmodel_switch\b|\bgoal_loop\b|\bcron\b|\bscheduler\b|_fire_job|_vram_gate_allows|_rpc_emit|_materialise_convo|_resume\b|_deliver_inbox|stop_turn_over_rpc|MCPManager|\bpaths\b|\bappsvc\b|\bcli\b|\blisten_tool\b|\bseat_guard\b|\bglassbox_plugin\b|\bskills_plugin\b|\bsettings_ui\b|active_work|llama_executable|configured_flags|configured_build|hook_host|accept_prompt|admit_prompt|queued_prompt|_submit_text|current_operation|OPERATION_CONTEXT|mcp_client|MCPManager|_load_doc|_save_doc
```

Exact final union:

```text
tests/test_abort_releases_a_parked_ask.py
tests/test_approval_over_rpc.py
tests/test_ask_over_rpc.py
tests/test_ask_user_question.py
tests/test_authority_footer_and_key.py
tests/test_autoscroll.py
tests/test_backend_switch.py
tests/test_background_tasks.py
tests/test_bash_tool_git_bash.py
tests/test_calendar.py
tests/test_calendar_ui.py
tests/test_chat_ready_call_sites.py
tests/test_chrome_tool.py
tests/test_cli.py
tests/test_cli_model_flag.py
tests/test_collapsible_tool_cards.py
tests/test_colorpicker.py
tests/test_command_palette.py
tests/test_compaction_ui.py
tests/test_connect_banner.py
tests/test_context_length.py
tests/test_convo_picked.py
tests/test_convo_rename.py
tests/test_convo_settings.py
tests/test_convos.py
tests/test_convos_picker.py
tests/test_cron_wiring.py
tests/test_ctx_resync.py
tests/test_data_root.py
tests/test_deny_stops_the_turn.py
tests/test_desktop_tools.py
tests/test_dialog_geometry.py
tests/test_file_tools.py
tests/test_first_boot.py
tests/test_fleet_identity.py
tests/test_footer.py
tests/test_footer_nav.py
tests/test_glassbox_emission.py
tests/test_glassbox_plugin.py
tests/test_goal_loop.py
tests/test_gui_management.py
tests/test_headless_never_loads.py
tests/test_hook_boundaries.py
tests/test_hooks_ui.py
tests/test_input.py
tests/test_integrations.py
tests/test_job_builder_ui.py
tests/test_jobs_store_lost_update.py
tests/test_jobs_store_root.py
tests/test_kill_tree_honesty.py
tests/test_lazy_convo.py
tests/test_lifecycle_hooks.py
tests/test_live_state_guard.py
tests/test_live_state_isolation.py
tests/test_llama_chat_ready.py
tests/test_llama_discovery.py
tests/test_llama_flags.py
tests/test_llama_ini.py
tests/test_llama_router.py
tests/test_llama_single_model.py
tests/test_lms_backend.py
tests/test_loop_list.py
tests/test_loop_model_pickers.py
tests/test_mcp_command.py
tests/test_mcp_config_writer.py
tests/test_mcp_dialog.py
tests/test_mcp_lifecycle.py
tests/test_mcp_timeout_bounds.py
tests/test_message_queue.py
tests/test_mmproj_autopair.py
tests/test_modal_centering.py
tests/test_modals.py
tests/test_model_picked.py
tests/test_model_screen.py
tests/test_modelcfg_validation.py
tests/test_no_dead_controls.py
tests/test_no_dialog_over_rpc.py
tests/test_no_fleet_registration.py
tests/test_no_implicit_model_load.py
tests/test_no_network_at_construction.py
tests/test_oauth_transport.py
tests/test_oauth_ui.py
tests/test_paths.py
tests/test_persist_error_report.py
tests/test_plan_mode.py
tests/test_plugins_command.py
tests/test_prompt_compiler.py
tests/test_prompt_compose.py
tests/test_queued_input_delivery.py
tests/test_ready_payload.py
tests/test_request_overrides.py
tests/test_resume_identity.py
tests/test_router_coexistence.py
tests/test_router_eviction_notice.py
tests/test_router_owner_exit.py
tests/test_router_record.py
tests/test_rpc.py
tests/test_rpc_isolation.py
tests/test_rpc_jobs.py
tests/test_rpc_model_switch.py
tests/test_rpc_tasks.py
tests/test_runtime_log_producers.py
tests/test_schedule_builder.py
tests/test_scheduler.py
tests/test_script_guard_coverage.py
tests/test_seat_convo_identity.py
tests/test_seat_guard.py
tests/test_seat_guard_backends.py
tests/test_seat_identity.py
tests/test_second_instance_guard.py
tests/test_self_store_writes.py
tests/test_set_over_rpc.py
tests/test_settings.py
tests/test_settings_controls.py
tests/test_settings_live.py
tests/test_shutdown_prune_guard.py
tests/test_sidebar_dialog.py
tests/test_sidebar_side.py
tests/test_skill_dir_binding.py
tests/test_skill_invocation.py
tests/test_skills_cache.py
tests/test_skills_library.py
tests/test_skills_picker.py
tests/test_skills_visible.py
tests/test_store.py
tests/test_store_lost_update.py
tests/test_studio_tool.py
tests/test_swap_button_in_real_dialogs.py
tests/test_swap_control_is_wired.py
tests/test_swap_teardown_mount_race.py
tests/test_target_id_is_not_the_correlation_id.py
tests/test_task_log_path_is_absolute.py
tests/test_task_screens.py
tests/test_theme_extra_tokens.py
tests/test_themes.py
tests/test_think_load_pickers.py
tests/test_thinking_timer.py
tests/test_toggle_tools_persists.py
tests/test_token_counts.py
tests/test_tool_approval.py
tests/test_tool_context.py
tests/test_tool_denied.py
tests/test_tool_list_dialog.py
tests/test_tool_policy.py
tests/test_tool_policy_wiring.py
tests/test_tool_profile_choices.py
tests/test_tool_schemas.py
tests/test_tools_disabled.py
tests/test_tools_prompt.py
tests/test_tools_registered.py
tests/test_torn_transcript_tail.py
tests/test_ttyguard.py
tests/test_turn_stop_line.py
tests/test_two_instances_coexist.py
tests/test_unattended_profile.py
tests/test_usage_on_the_wire.py
tests/test_view_image.py
tests/test_wake_after_compact.py
tests/test_wake_guard_abandoned_turn.py
```

T719-owned changed files, excluding the separately merged census dependency:

```text
Docs/Reports/t719-litegui-runtime.md
src/litetui/app.py
src/litetui/appsvc.py
src/litetui/cli.py
src/litetui/conversation.py
src/litetui/cron.py
src/litetui/goal_loop.py
src/litetui/gui_rpc.py
src/litetui/hook_host.py
src/litetui/listen_tool.py
src/litetui/llm_backend.py
src/litetui/mcp_client.py
src/litetui/paths.py
src/litetui/pccontrol_tool.py
src/litetui/plugins/glassbox_plugin.py
src/litetui/plugins/model_switch.py
src/litetui/plugins/settings_ui.py
src/litetui/plugins/skills_plugin.py
src/litetui/router_record.py
src/litetui/row_store.py
src/litetui/rpc.py
src/litetui/scheduler.py
src/litetui/seat_guard.py
src/litetui/settings.py
src/litetui/settings_screen.py
src/litetui/shared_state.py
tests/test_gui_management.py
tests/test_llama_router.py
tests/test_router_coexistence.py
tests/test_router_record.py
tests/test_store_lost_update.py
tests/test_target_id_is_not_the_correlation_id.py
tests/test_torn_transcript_tail.py
```

### Final verified gate

- Tested source commit: `4580223dbf9dfbdf5bee881a4a272a54171aae50`, rebased onto `928a9b798b0fe007f0e013171c6dca249897b3a3`. The final evidence commit changes this report only.
- `git merge-base --is-ancestor 928a9b7 HEAD`: exit 0, **ANCESTOR-OK**. Original base `9735a96` also remains an ancestor.
- **159 named test files**, **32 touched Python source/test files**, **33 T719-owned changed files total**. The runtime source map above includes all 25 changed runtime modules.
- Verbatim pytest summary: **1971 passed, 7 skipped, 6 xfailed in 520.35s (0:08:40)**. Exit 0; no failed test names or flaky-failure classification to report. This is one coordinated run, not repeated attempts until green.
- Ruff on all 32 touched Python source/test files versus pristine `928a9b7` blobs: **168 baseline diagnostics / 168 current diagnostics**, identical counts by code, **zero additions**. Both sides use `C:/Projects/LiteTUI/.venv/Scripts/python.exe` and the same explicit `pyproject.toml`. The two new runtime modules and new management test file are clean. This does not claim the repository's existing lint baseline is clean.
- Host: **Windows 11 Pro 10.0.26200, build 26200**; canonical test interpreter **Python 3.11.9, Windows AMD64**. Final run used the quiet coordinated heavy-runner window: Sentinel confirmed T716/T723 were finished and root paused integration and packaging. The initial failing run overlapped other work and is recorded separately above.
- Only this worker's leftover `.scheduler.lease` test artifact was removed after the runner exited. Final tracked/untracked status is checked after the report commit; ignored verification artifacts stay in `artifacts/t719/`.

Exact final environment and argv:

```powershell
$env:PYTHONUTF8='1'
& C:\Projects\LiteTUI\.venv\Scripts\python.exe -m pytest tests/test_abort_releases_a_parked_ask.py tests/test_approval_over_rpc.py tests/test_ask_over_rpc.py tests/test_ask_user_question.py tests/test_authority_footer_and_key.py tests/test_autoscroll.py tests/test_backend_switch.py tests/test_background_tasks.py tests/test_bash_tool_git_bash.py tests/test_calendar.py tests/test_calendar_ui.py tests/test_chat_ready_call_sites.py tests/test_chrome_tool.py tests/test_cli.py tests/test_cli_model_flag.py tests/test_collapsible_tool_cards.py tests/test_colorpicker.py tests/test_command_palette.py tests/test_compaction_ui.py tests/test_connect_banner.py tests/test_context_length.py tests/test_convo_picked.py tests/test_convo_rename.py tests/test_convo_settings.py tests/test_convos.py tests/test_convos_picker.py tests/test_cron_wiring.py tests/test_ctx_resync.py tests/test_data_root.py tests/test_deny_stops_the_turn.py tests/test_desktop_tools.py tests/test_dialog_geometry.py tests/test_file_tools.py tests/test_first_boot.py tests/test_fleet_identity.py tests/test_footer.py tests/test_footer_nav.py tests/test_glassbox_emission.py tests/test_glassbox_plugin.py tests/test_goal_loop.py tests/test_gui_management.py tests/test_headless_never_loads.py tests/test_hook_boundaries.py tests/test_hooks_ui.py tests/test_input.py tests/test_integrations.py tests/test_job_builder_ui.py tests/test_jobs_store_lost_update.py tests/test_jobs_store_root.py tests/test_kill_tree_honesty.py tests/test_lazy_convo.py tests/test_lifecycle_hooks.py tests/test_live_state_guard.py tests/test_live_state_isolation.py tests/test_llama_chat_ready.py tests/test_llama_discovery.py tests/test_llama_flags.py tests/test_llama_ini.py tests/test_llama_router.py tests/test_llama_single_model.py tests/test_lms_backend.py tests/test_loop_list.py tests/test_loop_model_pickers.py tests/test_mcp_command.py tests/test_mcp_config_writer.py tests/test_mcp_dialog.py tests/test_mcp_lifecycle.py tests/test_message_queue.py tests/test_mmproj_autopair.py tests/test_modal_centering.py tests/test_modals.py tests/test_model_picked.py tests/test_model_screen.py tests/test_modelcfg_validation.py tests/test_no_dead_controls.py tests/test_no_dialog_over_rpc.py tests/test_no_fleet_registration.py tests/test_no_implicit_model_load.py tests/test_no_network_at_construction.py tests/test_oauth_transport.py tests/test_oauth_ui.py tests/test_paths.py tests/test_persist_error_report.py tests/test_plan_mode.py tests/test_plugins_command.py tests/test_prompt_compiler.py tests/test_prompt_compose.py tests/test_ready_payload.py tests/test_request_overrides.py tests/test_resume_identity.py tests/test_router_coexistence.py tests/test_router_eviction_notice.py tests/test_router_owner_exit.py tests/test_router_record.py tests/test_rpc.py tests/test_rpc_isolation.py tests/test_rpc_jobs.py tests/test_rpc_model_switch.py tests/test_rpc_tasks.py tests/test_runtime_log_producers.py tests/test_schedule_builder.py tests/test_scheduler.py tests/test_script_guard_coverage.py tests/test_seat_convo_identity.py tests/test_seat_guard.py tests/test_seat_guard_backends.py tests/test_seat_identity.py tests/test_second_instance_guard.py tests/test_self_store_writes.py tests/test_set_over_rpc.py tests/test_settings.py tests/test_settings_controls.py tests/test_settings_live.py tests/test_sidebar_dialog.py tests/test_sidebar_side.py tests/test_skill_dir_binding.py tests/test_skill_invocation.py tests/test_skills_cache.py tests/test_skills_library.py tests/test_skills_picker.py tests/test_skills_visible.py tests/test_store.py tests/test_store_lost_update.py tests/test_studio_tool.py tests/test_swap_button_in_real_dialogs.py tests/test_swap_control_is_wired.py tests/test_swap_teardown_mount_race.py tests/test_target_id_is_not_the_correlation_id.py tests/test_task_log_path_is_absolute.py tests/test_task_screens.py tests/test_theme_extra_tokens.py tests/test_themes.py tests/test_think_load_pickers.py tests/test_thinking_timer.py tests/test_toggle_tools_persists.py tests/test_token_counts.py tests/test_tool_approval.py tests/test_tool_context.py tests/test_tool_denied.py tests/test_tool_list_dialog.py tests/test_tool_policy.py tests/test_tool_policy_wiring.py tests/test_tool_profile_choices.py tests/test_tool_schemas.py tests/test_tools_disabled.py tests/test_tools_prompt.py tests/test_tools_registered.py tests/test_torn_transcript_tail.py tests/test_ttyguard.py tests/test_turn_stop_line.py tests/test_two_instances_coexist.py tests/test_unattended_profile.py tests/test_usage_on_the_wire.py tests/test_view_image.py tests/test_wake_after_compact.py tests/test_wake_guard_abandoned_turn.py tests/test_mcp_timeout_bounds.py tests/test_queued_input_delivery.py tests/test_shutdown_prune_guard.py -q -p no:cacheprovider
```

Evidence on disk: `artifacts/t719/final-evidence.json`, `final-command.txt`, `pytest-final.log`, `ruff-final-base.json`, `ruff-final-current.json`, `review-tests-list.txt`, and `review-tests-final-union.txt`. SHA-256:

| Artifact | SHA-256 |
|---|---|
| `pytest-final.log` | `44518717f983aac1fa408d1a47587d1511a91049bc022d9f9ff408994978e5bc` |
| `final-command.txt` | `b6dec9e46075b21981f81361d205bdbfe77938ea0349b33784362614fb6de52c` |
| `review-tests-list.txt` | `be4bc3eeceb5a54c4444412c366a437587cbe603df16bd253cc64cee66f75e78` |
| `review-tests-final-union.txt` | `67d3844fe3188846d345ebf31d13431b91db1681cafb8110ccd2062dc062b4d2` |


Sentinel's independent intent review and coordinated merge remain the leader's responsibility. This worker did not push or merge T719; only the separately authorized census correction was pushed.


The package-scoped tool-authority AST gate passes: one dispatched execution door inside `LiteTUI._execute_tool`. Its three callers are the existing stream and compaction paths plus structured GUI tool execution. The gate intentionally permits additional callers that enter the same door and identifies them for review; GUI tools preserve normal authority and approval policy.

## Sentinel send-back: scheduler admission fixture

Sentinel's merged-tree review reported `test_hook_boundaries.py::test_external_producers_reach_admission_once` failing. Both main `4b4c05c` and T719 `f51f0be` already expected exactly `typed, rpc, harness, scheduled`; the failure was missing scheduled delivery, not an extra source. The untouched T719 worktree passed the single arm (`1 passed in 1.09s`) and entire file (`20 passed in 9.30s`). Main's newer changes only concern `tests/test_hooks_ui.py`, so no rebase was needed for this fixture correction.

The fixture had only an in-memory fake job and mocked scheduler persistence. T719 correctly rereads automatic candidates under its lease and rejects candidates deleted from an existing job store. Consequently, the test's result depended on whether the checkout already contained `jobs.json`. Conftest redirected scheduler load/save but not this authoritative reread.

Red first: isolate `paths.data_root()` to the test directory and create an empty store while retaining the old fake. The same arm then produced `1 failed in 1.27s`: actual `['typed', 'rpc', 'harness']`, expected `['typed', 'rpc', 'harness', 'scheduled']`. Evidence: `artifacts/t719/hook-boundaries-empty-store-red.log`.

Correction: keep the isolated initially empty store, persist a real `scheduler.Job` through production `scheduler.save`, remove the prepare/save mocks, and assert the persisted execution count is one. The exact four-source admission assertion remains unchanged, with a comment explaining shared-scheduler candidate ownership. No production source changed.

Verification: `PYTHONUTF8=1 C:/Projects/LiteTUI/.venv/Scripts/python.exe -m pytest tests/test_hook_boundaries.py -q -p no:cacheprovider` -> **`20 passed in 8.47s`**, exit 0. Evidence: `artifacts/t719/hook-boundaries-review-green.log`. Ruff on this test file using the canonical interpreter and repository configuration: **0 baseline / 0 current**. This supplement changes one test file and this report only; the 159-file runtime gate above remains evidence for unchanged runtime code. Sentinel explicitly authorized one correction commit and push on `feat/t719-litegui-runtime`; merging remains Sentinel's responsibility.
