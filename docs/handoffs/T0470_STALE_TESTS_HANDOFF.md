# T0470 stale-test repair handoff

## Current authority and stopping point

| Item | Contract / evidence |
|---|---|
| Next assignment | Repair every stale LiteTUI test before the release push, however many. Test files only; do not change product behavior to satisfy stale fixtures. This handoff is the outgoing worker's final action, not the start of that broader repair. Leader inbox authorization accompanying this handoff; task `T0470`. |
| Branch | `work/T0470-stale-tests`; handoff parent `1a7be4cec109d1bea767f6d558afcb6464bd3b6a`. Use `git branch --show-current`, `git log -1`, and `git status --short`. |
| Integration | Both candidates were independently APPROVE and locally merged by the leader; neither was pushed. No reviewer revisions outstanding. Merge review is not human visual acceptance or release permission. See exact merge SHAs below. |
| Boundaries | Do not amend/rebase the existing repair chain. Do not merge/push from the worker worktree. No test deletion, skip/xfail addition, arbitrary assertion weakening, or production ownership bypass. Existing `.lock` files are intentionally untracked (`git status --short`). |
| Next seat | Continue in a newly assigned isolated seat/worktree at the leader's selected baseline. The outgoing seat must not begin the expanded repair. Check the live inbox and board assignment before editing. |

## What is already repaired and merged

All line references below are at `1a7be4c` unless a different SHA is explicit.

| Batch | Candidate / local merge | What it establishes |
|---|---|---|
| Original 42 failures | Seven commits after `ac37a16`, tip `f4849f284fa8eea12cdf628f3f07c03848df41d5`; merge `0fddb927ec7d155320a86cf2efa240551b7d00ae` | Six original files repaired; collection guard strengthened plus a new regression file. Repaired six + guard: 112 passed. Original red: 42 failed / 67 passed. |
| Additional 17 failures | Five commits after `f4849f2`, tip `1a7be4cec109d1bea767f6d558afcb6464bd3b6a`; merge `3eaad56954768b977f37b141f7f9e3d8418669a9` | Denial 11, recovery 2, live-state 4 repaired. New task-guard fixture cases included. Leader's independent combined eleven-file run: 150 passed. |
| Source unchanged | `git diff ac37a16..1a7be4c -- src` | Empty. Temporary mutation proofs were restored byte-for-byte; none committed. |
| Merge equality | `git diff 0fddb92 f4849f2 --`; `git diff 3eaad56 1a7be4c --` | Both merged trees equal the corresponding candidate trees, as verified by the leader and reproducible from Git. |

| Repair commit | File(s) / reusable symbol |
|---|---|
| `9e62417c460240ea14706e74e64a5804e9dc875c` | `tests/conftest.py`; `tests/test_conftest_harness_guard.py`: force `LITETUI_NO_HARNESS=1` even when inherited empty. |
| `4be06a16dcbd3ba99a634f21d6a12aa9e6cc744e` | `tests/test_claude_backend.py::_identity_app`, `_fixture_prompt_facts`, `_assert_unrecorded_fixture_refused`. |
| `5922d147e8dae5b168a300e0a732ebf31d0e1747` | `tests/test_compaction_ui.py::_app`, `off_local_lm_studio`. |
| `115dd61aa319adda153df7afc9a5f0cb71015e10` | `tests/test_compaction_meter.py::_app`: reuse corrected compaction fixture. |
| `c203b292fbdafc7f09dd35e3be21b3f28ca2bce4` | `tests/test_lazy_convo.py::_app`: owned home before measurement. |
| `3d993587a3faad3d9060b13e4c1fb43a77ccdf1a` | `tests/test_fleet_identity.py`: owned construction/resume provenance. |
| `f4849f284fa8eea12cdf628f3f07c03848df41d5` | `tests/test_gui_owned_storage.py`: actual `shared_state.Lease.acquire` seam, not removed `gui_rpc.Lease`. |
| `701bca6156733909078fcad555fd1e7ede081616` | `tests/conftest.py::_never_write_the_live_task_store`; `tests/test_conftest_task_guard.py`. |
| `bcc75d7a61ca161607271e078829faba04788453` | `tests/test_deny_stops_the_turn.py::_app`. |
| `0746bb285b00781f812c5da36ed80067a478d83f` | `tests/test_recovery_composition.py`: owned local construction seam, live ownership during native reopen, recorded prompt provenance. |
| `c4e7737febb22855c6d9dacf8e3762a40161faae` | `tests/test_live_state_guard.py::owned_task_store` and four repaired tests. |
| `1a7be4cec109d1bea767f6d558afcb6464bd3b6a` | Recovery emitted prompt compares exact UTF-8 bytes, not normalized text. |

## File-by-file repair recipe

| Step / variant | Apply this recipe | Evidence / rerunnable query |
|---|---|---|
| Define the intended behavior first | Separate a fixture refusal from the behavior the test names. Record baseline error and retain all meaningful behavior assertions. An ownership check firing is not proof of a product defect. | `src/litetui/app.py:1571-1573`; `src/litetui/owned_storage.py::require_conversation` at `1a7be4c`. |
| Locate the actual constructor | A bare `LiteTUI()`, `app_mod.LiteTUI()`, or local helper forwarding empty kwargs is stale for mutable app use. Follow imported `_app` helpers: the failure may be in another file. A keyword `agent_session=some_variable` does not prove that variable is non-None or still owned. | `git grep -n 'LiteTUI(' -- tests`; `tests/test_pause.py::_agentic_app` -> `tests/test_card_summary.py::_app`. |
| Give the app a real capability | Prefer the existing `make_app` pytest fixture. Reusable non-fixture helper code can delegate to `_owned_seat.owned_app`. These acquire through `agent_launch_context.ordinary` in the temporary data root, then pass `agent_session=session` to the real constructor. Do not fake ownership internals or disable the constructor guard. | `tests/conftest.py:499-513`; `tests/_owned_seat.py:29-41`. |
| Preserve ownership lifetime | Use the app only while its capability remains held. Exiting `app.run_test` performs real shutdown and releases ownership. Keep native-session/ledger reopening inside that context if that is the test's actual subject. For a genuine process restart, acquire a fresh session explicitly instead; the repaired recovery test does NOT cover that case. | `tests/test_recovery_composition.py:290-345`, commit `0746bb2`. |
| Match execution authority | A local custom transport fixture should start with `initial_backend="custom", initial_model="fixture"`; take `model_id` from session authority. Changing settings/model after acquiring incompatible authority can make the test fail before inference. Reuse the compaction helper instead of another independent implementation. | `tests/test_compaction_ui.py:103-116`; `tests/test_deny_stops_the_turn.py:97-119`. |
| Stub only external boundaries | Acknowledge registration as a fixture and stub model-listing/transport I/O, `_connect`, and `_fetch_ctx_window` as the existing helper does. Preserve ownership validation/readiness when it is part of the assertion. Do not globally fake successful readiness to hide refusal. | `tests/test_compaction_ui.py::_app`; `tests/test_owned_app_first_use.py` for first-use tests that deliberately leave the model unchosen. |
| Persistent prompts / memory | Named store files now belong to `session.memory_root`, not a conversation directory. Synthetic saved native prompts require recorded store provenance on the actual persisted row; snapshot provenance cannot be assumed inherited from preceding meta rows. Keep the explicit refusal test for genuinely unrecorded prompts. | `tests/test_claude_backend.py::_fixture_prompt_facts`, `_assert_unrecorded_fixture_refused`; contract change `a2f8e82`, not `fc986bc3`. |
| SDK prompt argument | The SDK gets a file descriptor (`type=file`, path), not the former large plain string. Assert descriptor plus exact file bytes and retained ledger prose/identity/native-binding behavior. Do not drop the prompt-content assertion. | `tests/test_recovery_composition.py:335-340`; `src/litetui/claude_turn.py::system_prompt_file`; `1a7be4c`. |
| Lazy conversation | Construct/reserve the owned home before taking the intended baseline. Judge immediate directory-set equality plus recursive absence of a transcript. It is NOT a full recursive tree snapshot. | `tests/test_lazy_convo.py`, commit `c203b29`; use `git show c203b29 -- tests/test_lazy_convo.py`. |
| Raw task save | Use a real session and canonical conversation ID. Materialize the conversation where required, derive its path via `session.conversation_directory(id)`, and call `tasks.save(rows, path, agent_session=session)`. Preserve row visibility/LOST/merge semantics being tested. Never use a fabricated capability or change production storage permission. | `tests/test_live_state_guard.py::owned_task_store`, `test_an_explicit_root_is_still_honoured`; `src/litetui/tasks.py::save`, `save_by_convo`. |
| Testing the guard itself | The fixture's owned arm is SAVE-ONLY and SINGLE-CONVERSATION. It resolves inside the per-test temp root; no routing state or load change. Owned readers load their explicit conversation destination. Use the temporary UUID-named checkout stand-in, never the actual checkout as a test mutation target. | `tests/conftest.py:261-274`; `tests/test_conftest_task_guard.py::install_guard`. |
| Prove discrimination | Temporarily remove the behavior guard and require the named assertion to fail; restore exact bytes in finally and never stage production mutations. Keep positive controls explicit. | Mutation logs listed below; `T0470-T-second-evidence.py` in scratch. |
| Commit and report | One test-file domain per commit; separately commit shared fixtures and dedicated fixture tests. Use conventional subject, message file and five trailers. Required review gates merge; human acceptance gates completion. | `git show --format=full 701bca6`; task `T0470`. |

### Concrete before / after examples

| Case | Before (reproduce with git show) | After |
|---|---|---|
| Bare mutable app | `ac37a16:tests/test_deny_stops_the_turn.py:100`: `a = app_mod.LiteTUI()` | `1a7be4c:tests/test_deny_stops_the_turn.py:99-113`: import `_app as owned_app` from `test_compaction_ui`, then construct `a = owned_app(...)` with the strict profile/iteration settings retained. Its underlying real constructor is `tests/_owned_seat.py:41`. |
| Optional session defaults to None | `ac37a16:tests/test_claude_backend.py:349-355`: `_identity_app(..., agent_session=None)` calls `LiteTUI(agent_session=agent_session)` and seeds conversation-local memory | `1a7be4c:tests/test_claude_backend.py:349-356`: use `owned_app()` when no session supplied, otherwise retain supplied real session; seed `app._agent_session.memory_root`. |
| Raw task save | `ac37a16:tests/test_live_state_guard.py:92-96`: task convo ID `c1`, then `tasks_mod.save([task], paths.ROOT)` | `1a7be4c:tests/test_live_state_guard.py:93-110`: real owned fixture + canonical ID + temp checkout stand-in; `tasks_mod.save([task], checkout, agent_session=app._agent_session)`, with real owned redirect destination. For ordinary tests, use the explicit owned path at lines 147-151 instead. |

## Named remaining reds: 16 cases in two unchanged files

These were deliberately left alone under the old narrow scope. They are now part of the newly authorized broader repair. Every row below failed identically with prior `f4849f2` conftest and candidate conftest. Compare scratch XML node outcomes, not just totals.

| File / test | Evidence / cause |
|---|---|
| `tests/test_background_tasks.py::test_store_round_trips_and_marks_running_rows_lost` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; raw tasks.save without owned session. |
| `tests/test_task_screens.py::test_the_prompt_survives_the_store` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; raw tasks.save without owned session. |
| `tests/test_task_screens.py::test_neither_panel_can_show_the_other_kind` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_a_finished_task_is_in_neither` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_the_background_row_carries_a_live_clock` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_the_agent_block_names_all_three_stages_and_shows_the_prompt` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_the_thinking_stage_says_why_there_is_no_text` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_rpc_gets_the_data_and_never_a_dialog[open_background-bash]` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_rpc_gets_the_data_and_never_a_dialog[open_subagents-subagent]` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_the_tui_path_still_opens_the_dialog` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_enter_on_the_chip_puts_the_panel_on_the_screen[bg-bash-BackgroundProcessesBody]` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_enter_on_the_chip_puts_the_panel_on_the_screen[agents-subagent-SubagentsBody]` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_the_panel_clears_when_the_agent_is_done` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_the_clock_moves_without_rebuilding_the_panel` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_every_background_row_has_a_stop_that_runs_the_kill` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |
| `tests/test_task_screens.py::test_the_subagents_panel_offers_no_stop` | `1a7be4c`; `T0470-T-second-fixture-baseline.xml` / `T0470-T-second-fixture-users.xml`; bare app constructor in local make_app. |

The three unchanged comparison files were background_tasks, task_screens and router_record: 54 identical node outcomes, 16 failed / 38 passed. The candidate fixture-user run added four new passing guard cases, hence 16 failed / 42 passed. Router-record was green. No repairs to those two red files have been started.

## Static census: 169 construction sites + 4 real save sites in 125 files

This is the snapshot at `1a7be4c`, not a promise of 173 failing tests. It includes two non-test probe scripts under tests/; pytest-named files contribute 167 constructor sites + 4 save sites. Category 3 (other distinct observed construction sites with the two specified StoreError refusal lines) is 0. One deliberately mocked no-session save is excluded; one dynamic CLI forwarding wrapper remains unresolved. Four local **kwargs sites were read and included because their callers/parameter sets lack an owned session.

The census itself ran no tests. background_tasks/task_screens were already run in the authorized fixture-user comparison; card_summary's stale constructor was reached indirectly by recovery. Most other files have no run evidence in this task. Do not repeat the inaccurate statement that none of the 125 files has ever been run. No knowledge of all historical fleet runs is claimed.

### Exact executable invocation and retained artifacts

From this outgoing isolated worktree, the following resolves to the same interpreter and script argv used for the census (relative spellings avoid a machine-specific root):

```powershell
$Python = (Resolve-Path ../../.venv/Scripts/python.exe).Path
$Scratch = (Resolve-Path ../../../.scratch/astra).Path
& $Python "$Scratch/T0470-T-census.py" > "$Scratch/T0470-T-static-census.log"
```

The original scanner retains its scratch output location internally: inspect that path before reusing it from a different layout. It is read-only AGAINST THE CHECKOUT; it writes scratch JSON/log artifacts. No test import or pytest call occurs in the scanner. Exact retained names: `T0470-T-census.py`, `T0470-T-static-census.json`, `T0470-T-static-census.log`. Full per-file result is also embedded below, so the handoff does not depend on a private machine path. Rerunnable source query for every row: `git show 1a7be4c:<file>` and inspect the listed lines. Re-scan the current baseline after new repairs; these line numbers are intentionally anchored to the old tip.

| File at 1a7be4c | Constructor sites (lines) | Save sites (lines) | State / evidence |
|---|---|---|---|
| `tests/picture_probe.py` | 1: 124 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/picture_probe.py` |
| `tests/t723_shutdown_prune_probe.py` | 1: 107 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/t723_shutdown_prune_probe.py` |
| `tests/test_approval_persistence_t0340.py` | 1: 28 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_approval_persistence_t0340.py` |
| `tests/test_ask_over_rpc.py` | 1: 191 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_ask_over_rpc.py` |
| `tests/test_ask_user_question.py` | 1: 79 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_ask_user_question.py` |
| `tests/test_ask_user_question_sidebar.py` | 1: 38 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_ask_user_question_sidebar.py` |
| `tests/test_assistant_card_autocollapse_live.py` | 1: 19 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_assistant_card_autocollapse_live.py` |
| `tests/test_authority_footer_and_key.py` | 1: 63 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_authority_footer_and_key.py` |
| `tests/test_autonomy_lock_t1049.py` | 1: 14 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_autonomy_lock_t1049.py` |
| `tests/test_autoscroll_reflow.py` | 1: 11 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_autoscroll_reflow.py` |
| `tests/test_backend_switch.py` | 1: 24 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_backend_switch.py` |
| `tests/test_background_tasks.py` | 0: — | 1: 55 | Named red above; baseline/candidate comparison; `git show 1a7be4c:tests/test_background_tasks.py` |
| `tests/test_bg_chip_repaint.py` | 1: 36 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_bg_chip_repaint.py` |
| `tests/test_calendar.py` | 5: 248, 270, 317, 346, 364 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_calendar.py` |
| `tests/test_calendar_ui.py` | 1: 64 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_calendar_ui.py` |
| `tests/test_cancel_tool_placement.py` | 1: 35 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_cancel_tool_placement.py` |
| `tests/test_card_summary.py` | 1: 35 | 0: — | Reached indirectly in old recovery failure; helper itself left unchanged; `git show 1a7be4c:tests/test_card_summary.py` |
| `tests/test_chat_ready_call_sites.py` | 1: 141 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_chat_ready_call_sites.py` |
| `tests/test_claude_settings.py` | 1: 58 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_claude_settings.py` |
| `tests/test_claude_turn_order_pilot.py` | 1: 59 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_claude_turn_order_pilot.py` |
| `tests/test_claude_uncertain_startup.py` | 2: 187, 275 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_claude_uncertain_startup.py` |
| `tests/test_cli_prompt_exact.py` | 1: 65 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_cli_prompt_exact.py` |
| `tests/test_codex_native_tps_wiring.py` | 1: 129 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_codex_native_tps_wiring.py` |
| `tests/test_codex_resize.py` | 1: 18 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_codex_resize.py` |
| `tests/test_codex_settings.py` | 1: 75 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_codex_settings.py` |
| `tests/test_collapsible_tool_cards.py` | 1: 18 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_collapsible_tool_cards.py` |
| `tests/test_colorpicker.py` | 1: 195 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_colorpicker.py` |
| `tests/test_colorpicker_keys.py` | 1: 59 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_colorpicker_keys.py` |
| `tests/test_command_palette.py` | 1: 42 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_command_palette.py` |
| `tests/test_compact_pane_t1124.py` | 1: 20 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_compact_pane_t1124.py` |
| `tests/test_compaction_card_renders.py` | 1: 33 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_compaction_card_renders.py` |
| `tests/test_compaction_event.py` | 1: 131 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_compaction_event.py` |
| `tests/test_confirm_stop_sidebar.py` | 1: 35 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_confirm_stop_sidebar.py` |
| `tests/test_connect_banner.py` | 1: 91 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_connect_banner.py` |
| `tests/test_connect_error_path.py` | 1: 66 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_connect_error_path.py` |
| `tests/test_context_length.py` | 1: 66 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_context_length.py` |
| `tests/test_covered_modal_teardown.py` | 1: 40 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_covered_modal_teardown.py` |
| `tests/test_cron_wiring.py` | 1: 42 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_cron_wiring.py` |
| `tests/test_ctx_resync.py` | 1: 45 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_ctx_resync.py` |
| `tests/test_dialog_geometry.py` | 1: 63 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_dialog_geometry.py` |
| `tests/test_first_boot.py` | 1: 26 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_first_boot.py` |
| `tests/test_footer_fields.py` | 1: 52 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_footer_fields.py` |
| `tests/test_footer_live_work.py` | 1: 29 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_footer_live_work.py` |
| `tests/test_footer_nav.py` | 1: 30 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_footer_nav.py` |
| `tests/test_footer_order.py` | 1: 73 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_footer_order.py` |
| `tests/test_footer_task_manager.py` | 1: 15 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_footer_task_manager.py` |
| `tests/test_glassbox_emission.py` | 1: 82 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_glassbox_emission.py` |
| `tests/test_glassbox_plugin.py` | 2: 191, 205 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_glassbox_plugin.py` |
| `tests/test_goal_loop.py` | 1: 221 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_goal_loop.py` |
| `tests/test_gui_management.py` | 7: 75, 132, 172, 214, 239, 352, 434 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_gui_management.py` |
| `tests/test_hook_boundaries.py` | 1: 16 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_hook_boundaries.py` |
| `tests/test_hooks_ui.py` | 1: 20 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_hooks_ui.py` |
| `tests/test_image_viewer.py` | 2: 49, 204 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_image_viewer.py` |
| `tests/test_input.py` | 1: 53 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_input.py` |
| `tests/test_job_builder_ui.py` | 1: 53 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_job_builder_ui.py` |
| `tests/test_keys_toggle.py` | 1: 36 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_keys_toggle.py` |
| `tests/test_literal_display_sinks.py` | 1: 72 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_literal_display_sinks.py` |
| `tests/test_loop_list.py` | 1: 34 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_loop_list.py` |
| `tests/test_mark.py` | 1: 60 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_mark.py` |
| `tests/test_mcp_command_glue.py` | 1: 100 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_mcp_command_glue.py` |
| `tests/test_mcp_dialog.py` | 1: 60 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_mcp_dialog.py` |
| `tests/test_mcp_off_the_startup_path.py` | 2: 110, 121 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_mcp_off_the_startup_path.py` |
| `tests/test_mcp_toolless_retry.py` | 2: 64, 218 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_mcp_toolless_retry.py` |
| `tests/test_mmproj_autopair.py` | 1: 369 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_mmproj_autopair.py` |
| `tests/test_modal_centering.py` | 1: 51 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_modal_centering.py` |
| `tests/test_modals.py` | 1: 29 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_modals.py` |
| `tests/test_model_screen.py` | 1: 51 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_model_screen.py` |
| `tests/test_modelcfg_validation.py` | 1: 54 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_modelcfg_validation.py` |
| `tests/test_narrow_layout.py` | 1: 21 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_narrow_layout.py` |
| `tests/test_no_dialog_over_rpc.py` | 1: 45 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_no_dialog_over_rpc.py` |
| `tests/test_no_implicit_model_load.py` | 1: 78 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_no_implicit_model_load.py` |
| `tests/test_no_network_at_construction.py` | 2: 107, 151 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_no_network_at_construction.py` |
| `tests/test_oauth_ui.py` | 1: 97 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_oauth_ui.py` |
| `tests/test_oversize_preflight.py` | 1: 34 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_oversize_preflight.py` |
| `tests/test_palette_groups.py` | 1: 37 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_palette_groups.py` |
| `tests/test_pending_background_cancel.py` | 1: 49 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_pending_background_cancel.py` |
| `tests/test_picker_sidebar.py` | 1: 38 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_picker_sidebar.py` |
| `tests/test_plan_mode.py` | 1: 37 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_plan_mode.py` |
| `tests/test_plugin_dogfood.py` | 1: 22 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_plugin_dogfood.py` |
| `tests/test_plugins_command.py` | 1: 20 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_plugins_command.py` |
| `tests/test_prompt_compose.py` | 4: 122, 136, 153, 170 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_prompt_compose.py` |
| `tests/test_prompt_controls.py` | 1: 161 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_prompt_controls.py` |
| `tests/test_queued_input_delivery.py` | 1: 35 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_queued_input_delivery.py` |
| `tests/test_resume_connection.py` | 6: 51, 92, 129, 160, 206, 220 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_resume_connection.py` |
| `tests/test_resume_identity.py` | 5: 34, 50, 58, 74, 95 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_resume_identity.py` |
| `tests/test_resumed_seat_name.py` | 2: 68, 109 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_resumed_seat_name.py` |
| `tests/test_rpc_tasks.py` | 1: 33 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_rpc_tasks.py` |
| `tests/test_schedule_level_t1082.py` | 1: 32 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_schedule_level_t1082.py` |
| `tests/test_seat_name_refresh.py` | 1: 31 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_seat_name_refresh.py` |
| `tests/test_selection_defaults.py` | 2: 23, 191 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_selection_defaults.py` |
| `tests/test_self_compact.py` | 1: 14 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_self_compact.py` |
| `tests/test_set_over_rpc.py` | 1: 32 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_set_over_rpc.py` |
| `tests/test_settings_live.py` | 4: 54, 187, 246, 278 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_settings_live.py` |
| `tests/test_shutdown_prune_guard.py` | 3: 82, 111, 121 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_shutdown_prune_guard.py` |
| `tests/test_sidebar_dialog.py` | 1: 47 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_sidebar_dialog.py` |
| `tests/test_sidebar_side.py` | 1: 34 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_sidebar_side.py` |
| `tests/test_skill_autocomplete.py` | 1: 45 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_skill_autocomplete.py` |
| `tests/test_skills_visible.py` | 2: 29, 53 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_skills_visible.py` |
| `tests/test_slash_autocomplete_commands.py` | 1: 83 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_slash_autocomplete_commands.py` |
| `tests/test_spawned_seat_identity.py` | 1: 78 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_spawned_seat_identity.py` |
| `tests/test_spawner_errors.py` | 2: 59, 75 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_spawner_errors.py` |
| `tests/test_store_lost_update.py` | 0: — | 2: 106, 298 | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_store_lost_update.py` |
| `tests/test_stream_sink.py` | 1: 17 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_stream_sink.py` |
| `tests/test_studio_tool.py` | 2: 44, 62 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_studio_tool.py` |
| `tests/test_submit_admission.py` | 1: 116 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_submit_admission.py` |
| `tests/test_swap_button_in_real_dialogs.py` | 1: 70 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_swap_button_in_real_dialogs.py` |
| `tests/test_swap_control_is_wired.py` | 1: 80 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_swap_control_is_wired.py` |
| `tests/test_swap_teardown_mount_race.py` | 1: 92 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_swap_teardown_mount_race.py` |
| `tests/test_task_screens.py` | 1: 39 | 1: 85 | Named red above; baseline/candidate comparison; `git show 1a7be4c:tests/test_task_screens.py` |
| `tests/test_task_visibility_scope.py` | 1: 26 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_task_visibility_scope.py` |
| `tests/test_theme_extra_tokens.py` | 1: 58 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_theme_extra_tokens.py` |
| `tests/test_think_load_pickers.py` | 2: 319, 334 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_think_load_pickers.py` |
| `tests/test_thinking_autoscroll.py` | 1: 82 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_thinking_autoscroll.py` |
| `tests/test_toggle_tools_persists.py` | 2: 28, 76 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_toggle_tools_persists.py` |
| `tests/test_token_counts.py` | 1: 229 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_token_counts.py` |
| `tests/test_tool_approval_sidebar.py` | 1: 42 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_tool_approval_sidebar.py` |
| `tests/test_tool_list_dialog.py` | 1: 35 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_tool_list_dialog.py` |
| `tests/test_tool_list_disable.py` | 1: 111 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_tool_list_disable.py` |
| `tests/test_tools_prompt.py` | 1: 36 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_tools_prompt.py` |
| `tests/test_tools_registered.py` | 1: 52 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_tools_registered.py` |
| `tests/test_turn_stop_line.py` | 2: 102, 116 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_turn_stop_line.py` |
| `tests/test_user_bubble_height.py` | 1: 28 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_user_bubble_height.py` |
| `tests/test_user_name.py` | 6: 34, 53, 75, 108, 117, 126 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_user_name.py` |
| `tests/test_wake_after_compact.py` | 1: 117 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_wake_after_compact.py` |
| `tests/test_wake_guard_abandoned_turn.py` | 1: 141 | 0: — | Not run for this census; no direct run evidence here; `git show 1a7be4c:tests/test_wake_guard_abandoned_turn.py` |

### Census exclusions and traps

| Trap | What to preserve / what cannot be inferred | Evidence / query |
|---|---|---|
| Deliberately unowned host | Some tests intentionally build an app-shaped object without a capability to prove read-only archive access or mutation refusal. Do NOT give those objects a session. No executed direct `LiteTUI()` case deliberately asserting the new no-session constructor refusal was identified in this task; do not invent one or assume every syntactic hit is stale. Review each test's assertions before changing it. | `tests/test_owned_gui_archive.py::test_archive_rpc_read_no_missing_lease_created` (line 19) and `test_unowned_mutable_rpc_refuses_before_app_or_filesystem_side_effect` (line 30), at `1a7be4c`; `git grep -n -E 'pytest.raises|agent_session=None|_agent_session=None' -- tests`. |
| Legitimate negative authority tests | A real owned constructor can intentionally receive divergent model/backend/thinking inputs and assert refusal; aligning those inputs would destroy the test. | `tests/test_owned_invocation_boundary.py::test_real_constructor_refuses_divergent_execution`. |
| Syntactic match already passing | New no-session save in `test_conftest_task_guard.py:25` passes because the real writer is replaced by a recorder; it was explicitly excluded. Do not repair it. `test_user_name.py:93` forwards dynamic CLI kwargs and is not classified as unowned. None of the remaining unrun matches is asserted to fail; a skipped/monkeypatched/unreachable call can pass today. | `test_no_session_save_keeps_legacy_routing`; `T0470-T-static-census.json` exclusion/unknown sections. |
| Hidden stale helper | A file can have zero direct hits but depend on a stale imported helper. Correct the shared fixture in the new broader scope, then run its actual consumers. Do not count helper calls as independent constructor sites without explaining the metric. | `tests/test_pause.py::_agentic_app`; `tests/test_card_summary.py::_app`; `tests/test_recovery_composition.py:202-205`. |
| Single-conversation save redirect | Do not extend the current guard to multi-conversation or empty rows silently. It only redirects save; load receives explicit owned path. A broader fixture change needs its own specification and tests. | `tests/conftest.py::_never_write_the_live_task_store` at `701bca6`. |
| Positive-control mutation result | Removing redirection makes explicit-root and app-path tests fail at the explicit live-root positive controls, not their own explicit/app operations. The distinct name promises remain independently asserted. | `tests/test_live_state_guard.py:145,211`; `T0470-T-second-mutation-redirect.log`. |
| Native reopen is not process restart | Recovery retains the same live app ownership and reopens ledger/native sessions. It does not prove reacquiring ownership in a fresh process. | `tests/test_recovery_composition.py:290-345`; `0746bb2`. |
| Product code is not the repair target | Do not weaken `LiteTUI.__init__`, `owned_storage.require_conversation`, `AgentSession` leases, registration/authority checks, prompt provenance, or native-session lifecycle. If real product behavior contradicts intended semantics, report it rather than turn the test into a false green. | `git diff ac37a16..1a7be4c -- src` (empty); `src/litetui/app.py:1571-1573`; `src/litetui/owned_storage.py`. |
| Wrong seam / wrong memory root | Patch real external collaborators, not removed imports. Named memory comes from the owned home; attribution is `a2f8e82`. | `f4849f2` GUI lease patch; `4be06a1` prompt fixtures; `git show a2f8e82 -- src/litetui/app.py`. |

## Safety commands, coverage, and honest limits

| Safeguard | Exact mechanism / evidence |
|---|---|
| Refuse unguarded runs | `T0470-T-run.py`: require nonempty `LITETUI_NO_HARNESS`, print it and data root before pytest; `tests/conftest.py` forces it to `1` during collection. Never use inherited empty guard or run from main checkout. |
| Data isolation | Runner creates a fresh temporary `LITETUI_DATA_ROOT`; conftest `_never_write_the_live_data_root` further sets each test's root to `tmp_path`, redirects other writable paths, and `_guard_checkout_root_writes` snapshots checkout-root state. Owned helpers acquire only under this test root. |
| Correct imports | Runner asserts outgoing cwd name and branch, sets `PYTHONPATH` to that worktree's src, and checks `litetui.__file__` before executing pytest. A successor must adapt those explicit identity guards for its own worktree, not remove them. |
| Transport boundary | NO_HARNESS gates harness subprocesses/polling/local RPC publication. Local model/SDK cases use fake transport/session/listing seams and disabled connect/fetch. This is not blanket network isolation: two existing backend-picker tests still reach Strata GET /health and credential status. See next row. |
| Known residual I/O | `tests/test_claude_backend.py:264,340,345` -> `plugins/model_switch.py::backend_rows` -> `_strata_mark` -> `StrataBackend._health_state` -> `urlopen(GET /health)`. NO_HARNESS does not gate this. Left unchanged under old scope; explicitly decide its test-only isolation repair before broad runs. No packet capture was performed. |
| New repaired files | They add no backend-picker or sidecar-settings path: denial stream/compact, recovery mocked native/session transport, live-state `_save_background`. Recheck call graph when sharing fixtures more widely. |
| Mutation safety | `T0470-T-second-evidence.py` restores source bytes in finally and logs hashes; before/after checkout task-artifact snapshots both `{}`. This is a bounded task-store/log write check, not a whole-machine audit. |

Use the existing runner only after checking its cwd/branch checks fit the intended worktree. These are exact rerunnable command lines for the outgoing layout; commands are documentation here, NOT executed during handoff:

```powershell
$Python = (Resolve-Path ../../.venv/Scripts/python.exe).Path
$Scratch = (Resolve-Path ../../../.scratch/astra).Path
$env:LITETUI_NO_HARNESS = '1'
# Runner supplies/prints its own fresh LITETUI_DATA_ROOT before invoking pytest.
& $Python "$Scratch/T0470-T-run.py" T0470-T-second-green tests/test_deny_stops_the_turn.py tests/test_recovery_composition.py tests/test_live_state_guard.py
& $Python "$Scratch/T0470-T-run.py" T0470-T-second-original tests/test_claude_backend.py tests/test_compaction_ui.py tests/test_compaction_meter.py tests/test_lazy_convo.py tests/test_fleet_identity.py tests/test_gui_owned_storage.py tests/test_conftest_harness_guard.py
& $Python "$Scratch/T0470-T-run.py" T0470-T-second-shared tests/test_recovery_composition.py tests/test_deny_stops_the_turn.py tests/test_agent_launch_context.py tests/test_owned_app_first_use.py
& $Python "$Scratch/T0470-T-run.py" T0470-T-second-fixture-users tests/test_conftest_task_guard.py tests/test_background_tasks.py tests/test_task_screens.py tests/test_router_record.py
```

These reproduce historical argv but would overwrite those named logs: on an actual new run use a new task-prefixed tag. The runner then calls `python -m pytest <files> -q --tb=short --junitxml=<scratch>/<tag>.xml` with a bounded timeout. Preserve the existing evidence instead of rewriting it.

| Evidence artifact in scratch | Result / what it means |
|---|---|
| `T0470-T-report.md` | Full two-stage classification, all original 42 + additional 17 rows, exact commits, review corrections and census. |
| `T0470-T-red.log/xml` | Original six-file 42 failed / 67 passed. |
| `T0470-T-final-green.log/xml` | Original six + collection guard: 112 passed. |
| `T0470-T-second-green.log/xml` | Three follow-up files: 34 passed. |
| `T0470-T-second-original.log/xml` | Earlier six + guard: 112 passed. |
| `T0470-T-second-shared.log/xml` | Shared set: 64 passed. |
| `T0470-T-second-fixture-users.log/xml` | 16 failed / 42 passed (four new guard cases included). |
| `T0470-T-second-fixture-baseline.log/xml` | Previous conftest against the same three unchanged files: 16 failed / 38 passed; all 54 shared node outcomes identical; candidate restored byte-for-byte. |
| `T0470-T-second-restored.log/xml` | Repaired three + new guard after mutation restoration: 38 passed. |
| `T0470-T-second-prompt-bytes.log/xml` | Final one-line exact-byte strengthening: 1 passed. |
| `T0470-T-second-mutation-redirect.log/xml` | Four fail outside-owned when redirect removed; explicit/app cases fail at positive controls. |
| `T0470-T-second-mutation-owned-arm.log/xml` | Four fail invalid storage identity when owned arm returns legacy task-store destination. |
| `T0470-T-second-mutation-deny.log/xml` | Stop flag disabled: asks three times instead of once, test fails. |
| `T0470-T-second-mutation-recovery-bytes.log/xml` | Identity refresh disabled: exact prompt bytes differ, test fails. |
| `T0470-T-second-lint-comparison.log` | No added findings; three old unused E402 directives retained. |
| Leader validation (inbox, tip `1a7be4c`) | Eleven files: 150 passed. Not a worker-produced log and not a full-suite claim. |

## Suggested next order (proposal, not work already performed)

| Order | Work unit | Rerunnable dependency query / acceptance |
|---|---|---|
| 1 | Inspect/repair central stale helper families first, especially card_summary/pause, then local make_app in task_screens. Keep genuinely negative/no-session tests outside migration. | `git grep -n -E 'from (tests\.)?test_(card_summary|pause)|test_task_screens|def make_app|def _app' -- tests`; run the changed helper's consumers under bounded isolation. |
| 2 | Repair the 16 already named reds: background_tasks raw save and task_screens owned helper/raw save. This starts from measured failures, not guessed census hits. | Named table above; preserve LOST-state, panel kind, clock, stop/wake and RPC assertions; compare original failure and repaired outcome. |
| 3 | Handle shared stream/compaction/readiness families before their leaves. Check imports of compaction_ui and denial helpers before signature changes. | `git grep -n -E 'test_compaction_ui import|test_deny_stops_the_turn import|test_card_summary import|test_pause import' -- tests`. |
| 4 | Migrate the remaining raw task-store lost-update tests with real owned roots, preserving cross-instance baselines and collision semantics. | `tests/test_store_lost_update.py:106,298` at `1a7be4c`; do not defeat row-store behavior by fake persistence. |
| 5 | Work remaining constructor census by helper clusters, then direct leaf tests. Re-scan after each cluster; classify executed failures, intentional refusals and static false positives separately. | Full table above; `T0470-T-census.py`; no blanket replacement of every `LiteTUI()` string. |
| 6 | Broaden validation in bounded batches, then the leader-selected release acceptance/full-suite process. Ownership is only one source of stale tests. | New authority is every stale test before push; current census is not an exhaustive full-suite inventory. Escalate genuine product defects; test-only mandate remains. |

## What remains unsettled

| Open point | Evidence / boundary |
|---|---|
| True remaining failure count | 169 construction sites + 4 saves is a static site count with helper/dynamic/skip limitations. No full suite was run by this seat. Only the 16 named remaining cases are directly reproduced in the new fixture-user batch. |
| Intentional no-session behavior elsewhere | Concrete unowned GUI archive tests must stay unowned. Other negative constructor tests may be hidden in dynamic code or imports; inspect their specification before migration. `git grep -n -E 'StoreError|owned agent session|LiteTUI\(' -- tests`. |
| Lifecycle beyond native reopen | No fresh-process ownership reacquisition proof was added. `test_summary_new_segment_reopen_refreshes_one_identity_preserving_prose` stays within held ownership. |
| Complete external-I/O isolation | Existing Strata health/credential status path remains. Do not describe the runs as having all network disabled. New broader test safety should address this deliberately in test fixtures, not product source. |
| Unrelated live registration incident | Prior safety pause was resolved enough to resume bounded tests; logs did not establish causation. Do not invent a causal diagnosis. See `T0470-T-report.md` isolation section. |
| Full-suite/release/human gates | Both merges are local and not pushed. APPROVE and 150 passing tests do not establish every stale test is fixed, full-suite green, release published, or human acceptance. Leader owns those decisions. |
| Handoff artifacts | Scratch report/scanner/JSON/logs are retained unchanged by this handoff. This document is the sole committed handoff file. No further repairs or test runs were performed during handoff. |
