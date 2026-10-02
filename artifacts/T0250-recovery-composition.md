# T0250 / T0245 merged recovery composition receipt

## Authority and exact source

Authorized workspace: `C:/Projects/.scratch/EmberRecoveryPreview/LiteTUI`, branch `ember/recovery-preview`.
Validation source HEAD: `4631f12f2cee81f63f47e68c572ed86848a6d992` (main `156f7b88d4961c2efd7a26feca1494002b5555e1` plus both approved fixes).
Both ancestry checks returned 0:

```powershell
Set-Location 'C:/Projects/.scratch/EmberRecoveryPreview/LiteTUI'
git merge-base --is-ancestor f788f67f3f17fc724e01bfae308fab89c55c22e0 HEAD
git merge-base --is-ancestor b1e3e76 HEAD
```

Tests commit `f849c565e772d25c5826406f9c90c2d00b4b0749` adds only `tests/test_recovery_composition.py`; the immediately following receipt commit adds this file. Both approved source branches remain frozen. No main merge, push, product restart, live provider execution, download, or production edit was performed.

## Five acceptance contracts and actual assertions

All IDs below are prefixed `tests/test_recovery_composition.py::`.

1. **`test_startup_resolution_then_threshold_keeps_new_input_unsent`**: real hook admission holds NEW startup input while the prior send is uncertain; `/claude continue` does not change ledger bytes or append a row. Explicit resolve/continue prepares a distinct new ID, never replays the old ID. Real `stream_turn` context preflight schedules `/compact` without a native query; new input stays prepared. A real failed native summary request (asserted exact request) leaves the NEW entry prepared on disk.
2. **`test_delivered_threshold_without_terminal_has_no_compaction_or_recovery`**: real native stream sends exactly one original ID and interrupts once at threshold. Missing terminal result produces uncertain ledger state and no `_claude_compact_resume`. Generic budget-check callback **is scheduled** (`maybe`); this is not a performed recovery. Actual subsequent compact refuses before native summary query. Continue on both original interrupted host and maintenance host cannot execute; selected segment, conversation, and uncertain ledger bytes remain unchanged.
3. **`test_newer_prompt_during_summary_retains_owner_fifo_and_recovers_once`**: native summary event stream is suspended on `asyncio.Event`; two real `_submit_text` calls admit newer work concurrently. Successful summary events are delivered, but real compaction defers due to held input. Old native session and selected segment remain usable. Real hook admission/idle flush delivers newer FIFO entries first; explicit continue recovers the original prepared entry once. Repeated continue/flush gives precisely three distinct admitted IDs, all old-owner bound, no pending work or synthetic callback. Provider execution is substituted by a terminal ledger adapter; this establishes exactly-once **admission/dispatch**, not live SDK delivery semantics.
4. **`test_local_tool_threshold_summary_wake_never_exposes_held_claude_input`**: real Textual LOCAL tool loop executes the registered probe, crosses threshold, requests summary containing the completed tool result, installs returned summary, and reaches the real guarded wake once even with optional wake off. A pending wrong-owner Claude head intentionally suppresses synthetic wake; calls are exactly `turn, compaction`, not a resumed third request. Repeated real wake/flush remain suppressed. Claude-held private text and local queued tail appear in neither provider request snapshots nor transcript; FIFO, conversation owner, selected Claude segment, and ledger bytes remain unchanged. This scopes privacy evidence to transcript/model-request surfaces.
5. **`test_summary_new_segment_reopen_refreshes_one_identity_preserving_prose`**: real Claude compaction on a real LiteTUI host installs the returned summary and creates a distinct seeded segment. A recorded user-edited prompt containing quoted old identity prose and duplicate active identities is then persisted on that actual fresh segment. Disk reload and real backend option/session-open adapter repair preserve exact user prose/tool text/summary seed while emitting exactly one CURRENT registered identity. Changing the registered agent ID before second reopen replaces the first identity rather than appending. Native session binding is simulated explicitly (Claude learns its native ID on first turn, not session-open); second SDK options carry `resume=native-new`. Tool inventory is unchanged. Only external SDK/session objects are substituted; no claim of a live Anthropic session.

## Validation commands

Interpreter: `C:/Projects/LiteTUI/.venv/Scripts/python.exe`.
Every composition command explicitly changes to the preview and sets its source root:

```powershell
Set-Location 'C:/Projects/.scratch/EmberRecoveryPreview/LiteTUI'
$env:PYTHONPATH="$PWD/src"
& 'C:/Projects/LiteTUI/.venv/Scripts/python.exe' -m pytest tests/test_recovery_composition.py tests/test_claude_uncertain_startup.py tests/test_claude_turn.py tests/test_claude_compact.py tests/test_claude_backend.py tests/test_claude_persistence.py tests/test_claude_session.py tests/test_claude_submit.py tests/test_compaction_recovery.py tests/test_compaction_busy_input.py tests/test_local_compaction_loop.py tests/test_local_compaction_recovery.py tests/test_message_queue.py tests/test_queued_input_delivery.py tests/test_wake_after_compact.py tests/test_wake_guard_abandoned_turn.py tests/test_autocompact.py tests/test_self_compact.py -q --tb=short --deselect='tests/test_self_compact.py::test_real_round_compacts_after_all_results_and_resumes_with_handoff[success]' --deselect='tests/test_self_compact.py::test_real_round_compacts_after_all_results_and_resumes_with_handoff[failure]' --deselect='tests/test_self_compact.py::test_real_round_compacts_after_all_results_and_resumes_with_handoff[stop]'
& 'C:/Projects/LiteTUI/.venv/Scripts/python.exe' -m ruff check tests/test_recovery_composition.py
git -c core.whitespace=cr-at-eol diff --check 4631f12f2cee81f63f47e68c572ed86848a6d992 HEAD
```

Final affected-contract run after strengthening assertions: **292 passed, 3 deselected, 36.13s**, log `C:/Projects/LiteTUI/output/tasks/t-d63269d9b6a74e3099050c1c0941454b.log`. This includes all five exact composition IDs above.
Earlier same affected selection: **292 passed, 3 deselected, 34.43s**, log `C:/Projects/LiteTUI/output/tasks/t-0e0fac3e907844298f8c32615e8074cf.log`.
Scoped Ruff: **All checks passed**. Diff whitespace check: clean. No full suite was run.

### Exact exclusions, independently reproduced baseline only

Only the three exact `test_real_round_compacts_after_all_results_and_resumes_with_handoff[success|failure|stop]` IDs above were deselected. Their fake transport deepcopies the whole kwargs dict, including bound `retry_notice`, and fails `TypeError: cannot pickle '_asyncio.Task' object`. Same three failures reproduced against actual archived main `156f7b88d4961c2efd7a26feca1494002b5555e1`, with import paths and committed source bytes checked, at `C:/Projects/.scratch/T0250/baseline156f7b8`; evidence `C:/Projects/LiteTUI/output/tasks/t-74ec074254044678a9415d364e9578d1.log` (6 pass, same 3 fail). Composition fake snapshots **messages only**, avoiding that unrelated fixture bug.

The earlier CLI startup and canonical fleet-floor baseline failures are not hidden by broad `-k` filtering: neither file is part of this affected-contract selection.

## Actual import proof

A local inspectable diagnostic `output/recovery_source_proof.py` asserted every resolved module path is inside this preview's `src`, then printed SHA256 values:

| Module path relative to preview | SHA256 |
| --- | --- |
| `src/litetui/app.py` | `0c5431a9d2ae701b4d70dea6db8ebae949f0a378d6e5606e759f0356553aa835` |
| `src/litetui/claude_backend.py` | `fd9a7da43c2df6bdd7f933bd49d13ef824186e6385922fc8dc647f98e3f35fc4` |
| `src/litetui/claude_compact.py` | `da825ccb29accca4b4e2ee1595716707ab8cfc64cb0c06e1139f82c5186bc7ef` |
| `src/litetui/claude_persistence.py` | `83ddf682b7920a7448ea4e0522aabcdb0b80cc91615e0298797e5848f7ab1ced` |
| `src/litetui/claude_turn.py` | `415b8652cd3755017eb2e7449a757e717b0a7f0614f53a3231c1c729d208b830` |
| `src/litetui/hook_host.py` | `f5826e38921be45966d8b0e7e80ab8862c9ecc219413a87e2dbec7c2d3ecb4f5` |

## Boundaries and handoff

Tests use existing repo helpers; native SDK streams, native execution in case 3, and LOCAL model transport are hermetic substitutes. Real admission, ownership, queue, ledger persistence, native stream preflight/interruption, compaction, LOCAL tool round, Textual workers, wake guards, prompt repair, and backend SDK-options construction execute shipped preview code. Case 1/2 use separate minimal maintenance UI hosts reading the same disk ledger, not a launched product. Case 5 preserves user-edited recorded prompt prose; summary fidelity beyond the supplied external-provider response is not assessed.

Confidence: high for these operational contracts and regression selection; no live service/product smoke claim. New tests and receipt require leader-directed review of this preview before merge. Kanban remains reviewing; merge/product verification/human final inspection are not represented as completed by this receipt.
