# T0245 — compaction thresholds and interrupted-turn recovery

Status: implemented and focused-test verified in `ember/T0245`; awaiting independent review, integration sequencing, merge/intent gates and Ryan inspection. NOT running-product verified. No restart, rebuild, public push, merge, live model query, or full suite performed.

## Rebased integration verification

Rebased cleanly onto main `156f7b88d4961c2efd7a26feca1494002b5555e1` after T0251 `a16293b` and T0253 identity landed. `git merge-base --is-ancestor 156f7b8 HEAD` succeeded. Rebased implementation: `f1e1280`; rebased original report: `8188b15`. Identity matcher, registered-only refresh and persistence remain unchanged by the T0245 diff.

Actual rebased-tree run, with no concurrent tracked edits: **309 passed in 51.51s**. Command is the 231-test run below plus `tests/test_claude_backend.py tests/test_claude_persistence.py` (78 additional identity/admission contracts). Evidence: `C:/Projects/LiteTUI/output/tasks/t-53ad8f14582d446d8e41d5b10b676d1a.log`. Focused Ruff for three Claude modules and four touched/new test files passed again; three Claude modules' focused mypy passed; `git -c core.whitespace=cr-at-eol diff 156f7b8..HEAD --check` passed. An initial invocation named nonexistent `tests/test_seat_shutdown.py` and collected zero tests; corrected command produced the 309-pass result, not a claimed shutdown gate. No production code edits required by the rebase. Independent T0250/T0245 overlap review and Ryan inspection remain pending.

## Scoped review fix — input ownership during summary

Independent review found that suppressing a synthetic wake was insufficient: a real busy-submit during summary is already admitted to the OLD segment, so selecting a new segment stranded that queue and made selected-segment `/claude continue` unable to recover it. Reproduced RED on `d7b552b5` for both real typed and correlated RPC submissions using `LiteTUI._submit_text`.

Fix: inspect the original segment's durable pending deliveries before close/transition, excluding ONLY the proven-unsent preflight original that this compaction may transfer. If any other input owns that segment, defer compaction, leave its owner selected, and consume the synthetic continuation. Recheck after awaited cleanup because admission can occur during close too. No RPC operation ID transfer, and no changes to T0250 queue admission/uncertainty guards or T0253 identity.

`tests/test_compaction_busy_input.py` suspends the actual summary/cleanup await on `asyncio.Event`, concurrently invokes real busy `LiteTUI._submit_text`, then allows a successful provider result/cleanup. It drives selected owner, `queue_ready`, idle `LiteTUI._flush_pending_input`, and `/claude continue`. All eight combinations (typed/RPC × arrival during summary/cleanup × with/without preflight-held original) retain a deliverable original segment and preserve correlated operation ID. External provider dispatch alone is substituted, collecting admitted content and settling its real ledger delivery terminal. Newer input is authorized ONCE; `/continue` never replays it and recovers ONLY the original unsent prompt when present. All pending entries and FIFO clear, and no old synthetic continuation is scheduled. Before-close deferral retains the live session; cleanup-race deferral retains selected native session reference for reopen. Exclusion binds the exact known original durable entry ID, never source/profile matching.

Final scoped round: **317 passed in 45.94s**, command = rebased 309-test run above plus `tests/test_compaction_busy_input.py`, no concurrent tracked edits. Evidence: `C:/Projects/LiteTUI/output/tasks/t-0b641bb4d3384b74b35071cc9dbccc42.log`. Focused Ruff and `claude_compact.py` mypy pass; CRLF-aware diff check passes. Production fix commit `1ca4cdd`; subsequent test/evidence completion is the same single scoped review round. Independent revised overlap review verdict and running-product verification remain pending.

## Approved semantics

- Read Claude's effective serving window with pinned SDK `get_context_usage()` on the existing SDK owner task, after model selection and before sending. Use `maxTokens`, not a static 1M guess or the largest cumulative result `modelUsage.contextWindow`.
- Preserve configured auto-compaction enabled/threshold values (defaults remain enabled/80%). Do not auto-promote model IDs to `[1m]`.
- Interrupt/drain Claude at its configured threshold; compact only after the chat worker exits and its partial answer/activity is saved.
- Mandatory one-shot continuation for threshold-interrupted Claude turns and the existing local/non-native `compact_due` tool-loop pause. Ordinary/manual compaction still follows the unchanged `wake_after_compact` preference (default false).
- User stop, queued/new input, changed conversation/backend/session/input win. Failed compaction and uncertain delivery never authorize automatic replay.

## State transitions

1. Normal Claude delivery: `prepared -> submitted -> terminal`. Threshold interrupt requests SDK interrupt once, waits for provider drain, consumes terminal result, saves partial display/activity, then schedules summary. Successful summary creates a fresh segment; one admitted resume instruction follows. Delivered original input/tools are NOT replayed.
2. Missing terminal/drain failure: `submitted -> uncertain`; no mandatory continuation. Existing uncertain-delivery compaction refusal remains intact.
3. Threshold already reached before non-RPC input sends: original remains `prepared`, no `query` of original content. Failed summary leaves that original prepared in the unchanged segment. Successful summary creates a fresh `prepared` input preserving original content/profile/source, then closes original `prepared -> terminal` with `not_sent_compaction`. Only the fresh prepared input is queued through normal admission. If stop/new input wins after summary, fresh input stays recoverable with `/claude continue`, not automatically sent.
4. Context measurement unavailable/malformed: stale window cleared, input stays `prepared`, no query and no teardown of healthy SDK session. Visible notice names `/claude continue`; retry after metadata recovery.
5. Correlated RPC input already above threshold: stays `prepared` in original segment. No cross-segment `operation_id` transfer invented. Inspect `/claude status`, run `/claude new`, then manually resend with a NEW RPC operation ID. Old held input stays saved without replay; `/claude continue` cannot shrink its full original context.
6. Local existing threshold pause: completed tool results remain saved; summary success schedules one guarded continuation even with optional wake false. Failure/refusal consumes the one-shot intent. Codex-owned native compaction path remains unchanged.

## Evidence

Initial new behavioral tests reproduced three failures: absent owner-task context API, stale 1M window suppressing 200k threshold interruption, successful compact scheduling zero continuations with optional wake off.

Final focused run (pinned shared venv interpreter, worktree cwd, hermetic test data): **231 passed in 51.57s**:

```powershell
C:/Projects/LiteTUI/.venv/Scripts/python.exe -m pytest tests/test_compaction_recovery.py tests/test_local_compaction_recovery.py tests/test_local_compaction_loop.py tests/test_wake_after_compact.py tests/test_wake_guard_abandoned_turn.py tests/test_autocompact.py tests/test_claude_turn.py tests/test_claude_compact.py tests/test_claude_session.py tests/test_claude_thinking_after_tools.py tests/test_claude_turn_order_pilot.py tests/test_claude_events.py tests/test_claude_submit.py tests/test_compaction_event.py tests/test_compaction_ui.py -q
```

Covers 200k/1M actual-window distinction, stale cumulative result window, SDK owner-task call, healthy metadata failure, pre-query refusal, preflight prompt preservation, failed/over-limit summary, uncertain outcome refusal, exactly-once callback, stop/new-input/ownership races, existing wake/manual contracts. Real local tool-loop test observes exactly `turn -> compaction -> turn`, completed tool result before summary, and resume message in the last request with wake setting OFF.

Focused Ruff passes for three Claude source files and four changed/new test files. Existing `app.py` advisory findings: **105 Ruff diagnostics unchanged** (exact code/message multiset against HEAD), **51 mypy diagnostics unchanged** (same package/import topology baseline against HEAD). Three Claude source files pass focused mypy with `--follow-imports=silent --ignore-missing-imports`.

`git -c core.whitespace=cr-at-eol diff --check` passes. Mixed app.py line endings preserved via exact count-checked byte replacements; no whole-file normalization.

Final command evidence: `C:/Projects/LiteTUI/output/tasks/t-5d11ea5dd20b4f76a7ec8ccbff9a9493.log`.

## Limitations and separate follow-up candidates

- Already beyond hard model limit can still reject the summary request. No success/automatic recovery promised there. Threshold reserve cannot guarantee fitting arbitrarily large user/tool payloads or all output spikes.
- No live SDK/model integration or Ryan running-product inspection performed. Actual SDK call path is tested with fake external client boundary on the real session owner; not an authenticated CLI probe.
- RPC correlated-input cross-segment transfer deliberately excluded; separate policy/persistence follow-up needed if automatic transfer is desired.
- Continuation intent is process-local, not a crash/restart replay permission. Uncertain-ledger policy is T0250, identity is T0253.
- Existing `tests/test_self_compact.py` integration cases success/failure/stop fail BEFORE the first request because fixture deepcopies a bound callback containing an asyncio Task. Same three failures reproduced using a narrow HEAD `app.py` substitution through a temporary pytest plugin (other worktree modules retained), and current app; six other tests pass. This is NOT a full archived-checkout proof. Not silently fixed. Baseline evidence retained at `temp-working-dir/self_compact_baseline.log`; report to leader for separate fixture repair.
- One intermediate 231-pass run had root-write teardown error because README was edited while tests were running. Corrected workflow and reran without edits: final 231-pass result above is clean.

## Inspection path for Ryan (after approved integration)

Use a standard 200k Claude ID and a `[1m]` selection separately; inspect window chip and configured threshold. Run an agentic tool task until threshold pause; expect compact card then one continuation without manual continue. Stop or submit new instructions around compaction; expect no stale continuation. Test unavailable context metadata and summary failure: held input remains visible/recoverable, no claimed success. Ordinary manual `/compact` with optional wake disabled should remain idle. Local-backend existing tool-loop threshold pause should compact then continue exactly once.
