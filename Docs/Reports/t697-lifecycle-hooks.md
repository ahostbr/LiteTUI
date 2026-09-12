# T697 lifecycle hooks implementation evidence

Owner: RustAxis (`01a096ad-0296-7c82-94a9-4f596dfb57b2`), worker. Branch: `codex/litetui-lifecycle-hooks`. Worktree: `C:/Projects/LiteTUI/.worktrees/codex-lifecycle-hooks`. Base: `aba39c9` (includes T689, T694, T695). Sentinel owns review and merging.

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
