# T697 lifecycle hooks implementation evidence

Owner: RustAxis (`01a096ad-0296-7c82-94a9-4f596dfb57b2`), worker. Branch: `codex/litetui-lifecycle-hooks`. Worktree: `C:/Projects/LiteTUI/.worktrees/codex-lifecycle-hooks`. Current base: `881ed0d` (includes T698, T699, T700); original validation below is historical. Sentinel owns review and merging.

Specification: `Docs/Plans/litetui-lifecycle-hooks.md`. User guide: `Docs/lifecycle-hooks.md`.

## Plan-to-code and executable evidence

| Plan section | Implementation | Named test evidence |
|---|---|---|
| Global/project precedence, disabled overrides, immutable event snapshots | `src/litetui/lifecycle_hooks.py:189`, `HookConfig.snapshot`, frozen `Hook`/`Snapshot` | `test_scope_disabled_override_and_order`, `test_explicit_isolation_and_literal_tool_patterns` |
| Atomic saves, serialization, independent edits, conflicts, malformed config | `src/litetui/lifecycle_hooks.py:199`, `HookConfig.save`, `repair`, shared `row_store.apply_delta` | `test_concurrent_independent_edits_merge_and_same_entry_conflicts`, `test_simultaneous_writers_preserve_independent_rows`, `test_invalid_config_blocks_instead_of_dropping_gates` |
| Direct executable/argv, JSON stdin, output/time bounds and child cleanup | `src/litetui/lifecycle_hooks.py:295`, `run_hook`, `event_document` | `test_real_script_receives_json_and_denies`, `test_bad_gate_output_fails_closed`, `test_timeout_and_cancellation_kill_fixture_child_tree`, `test_event_total_text_bound_and_image_omission` |
| Existing policy/approval reuse, effective profile, stable hook identity | `src/litetui/app.py:1815`, `_authorize_action`; `hook_host.invoke`; `Hook.approval_name` | `test_hook_process_cannot_escape_effective_policy`, `test_human_hook_denial_stops_turn_but_test_panel_does_not`, existing tool approval/RPC tests |
| Prompt admission and FIFO delivery | `hook_host.start_prompt`, `admit_prompt`, `queued_prompt`; app producers and RPC | `test_external_producers_reach_admission_once`, all six cases of `test_prompt_admission_rejects_once_and_retains_input`, existing message queue/delivery tests |
| Tool gate and terminal background observation | `src/litetui/app.py:1899`, `_execute_tool`, `_observe_tool_outcome` | `test_gate_prevents_real_tool_side_effect_and_allows_recovery`, `test_background_after_only_when_work_finishes` |
| Completion gate before finalizers, three corrective continuations, pause | `hook_host.completion`; `_stream` before `plugins.finalize_turn` | `test_completion_cap_and_internal_prompt_bypass`, both deny-all and deny-then-accept cases of `test_real_stream_rejected_completion_never_finalizes` |
| No compaction hooks | `_compact` explicitly passes `hooks_enabled=False`; `_wake_after_compact` suppresses hooks for its synthetic turn | `test_actual_compact_caller_bypasses_hooks` drives a real compact worker through a fixture transport and tool; `test_compaction_tool_and_wake_suppress_all_hooks`; existing compaction/wake tests |
| App/conversation lifecycle | `hook_host.queue_lifecycle`, `drain_lifecycle`, enter/leave helpers; app mount/unmount/materialization/resume | `test_lifecycle_transitions_and_graceful_shutdown_once` |
| Full editor, scopes, testing, retained prompts, malformed-file repair | `src/litetui/hooks_screen.py:31`, `HooksEditor`; Settings Hooks tab; `/hooks` uses the same editor and existing dialog host | `test_author_save_validate_test_and_reopen` (mouse and keyboard), `test_settings_tab_and_slash_command_share_editor`, `test_disabled_project_override_and_delete_reveal_global`, `test_malformed_file_can_be_repaired_in_editor` |
| No live configuration/fleet/model access in tests | `tests/conftest.py` redirects both hook config paths; existing host isolation fixtures | Six existing guards named below; fixture transport is local and does not contact a backend |

## Validation boundaries

- Named 22-file regression slice: **191 passed, 7 skipped** in 80.14 seconds. Skips are opt-in live RPC/backend tests (`LITETUI_E2E=1`), which were not enabled. Output: `artifacts/t697/final-tests.txt` in the worktree.
- A subsequent actual-compaction fixture found a missed bypass in `_compact` (its tool arguments are named `fname`/`fargs`). It failed before the one-line correction. The compaction/boundary slice passed **38 tests in 12.23 seconds** after correcting that actual caller. The 191 result predates this last correction and is not represented as a rerun of the final SHA.
- New modules and new tests pass Ruff. Mypy with `--follow-imports=skip` passes the three new source modules. Existing touched modules have **96 pre-existing Ruff diagnostics, 96 after the change, zero additions**, compared to the base by diagnostic code/message. This is not a claim that the repository has a clean full lint/typecheck baseline.
- `tools/tool_door_gate.py` still finds exactly one dispatched-tool execution door and two callers.
- Rendered editor exported to `artifacts/t697/hooks-editor.svg`, rasterized locally to `hooks-editor.png`, and visually inspected. Form controls and modal bounds are visible; scroll, author/save/test/reopen, and sidebar availability are exercised by the UI tests. The renderer is a temporary `uv --with resvg-py` tool, not a project dependency.
- No full repository suite or live model test was run. Sentinel's test coordination rule limits this seat to named files. The six guards included were `test_live_state_guard`, `test_script_guard_coverage`, `test_seat_guard`, `test_seat_guard_backends`, `test_ttyguard`, and `test_wake_guard_abandoned_turn`.

The editor's explicit Test action uses the same authorization helper with `stop_on_denial=False`: denying a test does not stop unrelated conversation work. Normal hook/tool approval denial retains the existing stop behavior. No hook runtime log producer or payload logging was added.

Independent Intent Gate is pending Sentinel's review of the commit and this map. This worker does not merge.

## First review correction

Sentinel's first pass on `0c68d90` returned the `/mark` image prompt profile as the only requested correction: it stamped the global default onto a human turn instead of the conversation's choice. Both immediate delivery and the queued image item now carry `chosen_tool_profile`. `test_mark_image_prompt_uses_conversation_profile` exercises the real image handoff worker and, for its queued case, the actual flush path; both cases failed with global `autonomous` versus chosen `interactive` before the correction. The revised boundary/conversation-settings/six-guard slice passed **107 tests in 21.25 seconds**. The new test passes Ruff and the tool-door invariant remains one door/two callers. Sentinel's final recheck and merge remain pending.

## Second review correction and expanded regression scope

Sentinel returned the branch after a broader merged-tree gate exposed 13 failures. This seat rebased onto `ccc9251` and reproduced those 13 plus a Windows console encoding failure: **14 failed, 717 passed, 6 xfailed in 184.34 seconds**, in 49 files. The original 22-file slice did not cover these contracts. The final rebase is onto `881ed0d`; main and Ryan's running checkout were not edited.

| Failure class and exact arms | Correction | Why the assertion still tests the same contract |
|---|---|---|
| `test_tool_policy_wiring.py`: `test_read_executes_without_a_modal`, `test_sensitive_interactive_call_requires_one_host_decision`, `test_denied_modal_and_scheduled_profile_never_execute` | If a host has no bound `_authorize_action`, bind the shared LiteTUI implementation to that host. Hosts without hook configuration retain the original tool path without the outcome wrapper. | The test hosts and all assertions are unchanged. Read execution, one approval, and denied/no-execution behavior still exercise the real authorization door; policy is never skipped. |
| Same file: `test_a_cron_turn_is_AUTONOMOUS_whatever_the_conversation_is_set_to`, `test_a_cron_turn_asks_NOBODY_even_when_the_conversation_is_ask_first`, `test_midturn_queue_adopts_the_delivered_items_profile` | Prompt acceptance materializes a conversation when the host supplies that method. Minimal older prompt hosts retain their existing append path. | No edits to this test file. The real LiteTUI still materializes; scheduled authority and queued-item profile adoption assertions are unchanged. |
| `test_set_over_rpc.py`: `test_prompt_now_honours_tool_profile`, `test_a_prompt_without_the_field_is_unchanged`; `test_target_id_is_not_the_correlation_id.py::test_CONTROL_a_command_with_no_target_is_unaffected` | Doubles accept the added keyword-only `source` and assert it equals `rpc`. The unknown-profile negative-control double uses the same signature. | Original submission, reply, profile and ID assertions remain. These tests isolate RPC dispatch, so adapting the fake callee's signature retains their subject and adds an assertion for source routing. |
| `test_deny_stops_the_turn.py::test_explicit_compaction_can_persist_after_a_previous_stop[False/True]` | Fake execute accepts keyword-only `hooks_enabled` and asserts it is false. | **A fake signature mismatch, not another production bypass miss.** The actual compaction caller already passes false; its caught TypeError prevented the fake from recording the write. Both original write and resulting-summary assertions remain for both previous-stop states. The separate real-worker/real-tool boundary fixture still checks actual bypass. |
| `test_settings.py::test_the_six_sections_are_tabs` (now `test_the_settings_sections_are_tabs`) | Replace the stale seven-tab count with the exact ordered eight pane IDs, including Hooks. | Still verifies mounted tabs; now detects a missing, duplicate, reordered or wrong section rather than only a count. |
| `test_swap_control_is_wired.py::test_every_body_that_can_lose_an_edit_declares_get_state` | Add actual HooksEditor state capture/restore and HooksBody delegation. SettingsBody delegates the embedded editor too. | The census is untouched; HooksBody is not exempted. Four new live UI arms verify sidebar-to-modal-to-sidebar carries raw invalid edits, rows, scope, selection, sample/repair text and the original disk baseline, for both dialog hosts and valid/malformed files. A concurrent writer still prevents overwrite after a swap. |
| `test_settings_live.py::test_the_live_settings_wiring_holds` | Run with `PYTHONUTF8=1`. | No file or assertion edit. The extra local failure was cp1252 attempting to print a Unicode label, not a branch regression. |

The new four swap arms initially found an additional real graceful-shutdown failure: a malformed config was reported to a chat DOM Textual had already removed. Shutdown reporting now emits only fixed metadata and the event name through the logger; it includes no hook output or free-form error text. All draining shutdown events disallow new approvals, and shutdown does not schedule duplicate lifecycle workers. The four live arms leave the malformed file in place and complete real app teardown. The hook/UI/compaction subset then passed **41 tests in 30.05 seconds**.

### Final scope derivation

Read the test files on disk, match the following expression, then add the six guard files already listed above and sort/deduplicate. T700's converted `test_input`, `test_store` and `test_ask_user_question` do not match this expression; there are no matching script-style exclusions on this final tree. This is a named regression slice, not a full-suite claim.

```text
hook_host|lifecycle_hooks|hooks_screen|_authorize_action|_submit_text|_execute_tool|_compact\b|queued_prompt|_deliver_queued_input|settings_screen|settings_ui|_mark_wait|chosen_tool_profile
```

The exact 49-file list (also saved in `artifacts/t697/review-tests-list.txt`):

```text
tests/test_approval_over_rpc.py
tests/test_authority_footer_and_key.py
tests/test_autocompact.py
tests/test_autoscroll.py
tests/test_chat_ready_call_sites.py
tests/test_colorpicker.py
tests/test_compaction_ui.py
tests/test_convo_settings.py
tests/test_cron_wiring.py
tests/test_deny_stops_the_turn.py
tests/test_dialog_geometry.py
tests/test_glassbox_emission.py
tests/test_hook_boundaries.py
tests/test_hooks_ui.py
tests/test_lifecycle_hooks.py
tests/test_live_state_guard.py
tests/test_loop_model_pickers.py
tests/test_mark.py
tests/test_message_queue.py
tests/test_no_dead_controls.py
tests/test_no_dialog_over_rpc.py
tests/test_oauth_ui.py
tests/test_queued_input_delivery.py
tests/test_script_guard_coverage.py
tests/test_seat_guard_backends.py
tests/test_seat_guard.py
tests/test_self_store_writes.py
tests/test_set_over_rpc.py
tests/test_settings_controls.py
tests/test_settings_live.py
tests/test_settings.py
tests/test_sidebar_dialog.py
tests/test_sidebar_side.py
tests/test_skill_invocation.py
tests/test_swap_control_is_wired.py
tests/test_target_id_is_not_the_correlation_id.py
tests/test_theme_extra_tokens.py
tests/test_themes.py
tests/test_token_counts.py
tests/test_tool_approval.py
tests/test_tool_context_wiring.py
tests/test_tool_list_disable.py
tests/test_tool_policy_wiring.py
tests/test_tool_profile_choices.py
tests/test_tools_disabled.py
tests/test_ttyguard.py
tests/test_turn_engine.py
tests/test_wake_after_compact.py
tests/test_wake_guard_abandoned_turn.py
```

### Final verified tree

On source commit `7c2b99a` rebased onto `881ed0d`, the full named run passed **735 tests, 6 xfailed, 0 failures in 191.44 seconds**. Only this report changed after that run; no production or test file changed. Command (PowerShell, from the worktree):

```powershell
$env:PYTHONUTF8 = "1"
$hooksReviewFiles = @(Get-Content artifacts/t697/review-tests-list.txt)
uv run --locked pytest @hooksReviewFiles -q --tb=short --junitxml=artifacts/t697/review-green.xml
```

The six expected failures are existing dialog-geometry cases:
- `tests.test_dialog_geometry::test_no_child_is_clipped_by_its_own_container[24-sidebar-picker]`
- `tests.test_dialog_geometry::test_no_child_is_clipped_by_its_own_container[24-sidebar-tool_approval]`
- `tests.test_dialog_geometry::test_no_child_is_clipped_by_its_own_container[24-sidebar-ask_user_question]`
- `tests.test_dialog_geometry::test_no_child_is_clipped_by_its_own_container[24-modal-picker]`
- `tests.test_dialog_geometry::test_no_child_is_clipped_by_its_own_container[24-modal-tool_approval]`
- `tests.test_dialog_geometry::test_no_child_is_clipped_by_its_own_container[24-modal-ask_user_question]`

- New source modules and new tests: Ruff clean. Mypy `--follow-imports=skip` passes all three new source modules.
- All 17 changed Python files were compared against `881ed0d` with Ruff via `--stdin-filename` under the same repository configuration: **114 baseline diagnostics, 114 current, no added code/message pairs**. Source-only remains 96/96, including app.py 67/67. This is a delta check, not a clean-repository lint claim. Machine-readable details: `artifacts/t697/ruff-baseline.json`.
- Final tool-door check passes: **one `asyncio.to_thread(fn, ...)` dispatch door, two `_execute_tool` callers**.
- Final test list, red/green console output and JUnit XML are saved under `artifacts/t697/` in this worktree. No full repository suite or live model run was performed.
- All existing assertions in the returned policy, compaction, RPC and swap tests remain. The tab count became an exact ordered identity assertion. No expected-failure or skip marker was added.

Sentinel's integration review is still required. This worker has not merged the branch.
