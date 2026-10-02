# T0245 — compaction thresholds and interrupted-turn recovery

Status: implemented and focused-test verified in `ember/T0245`; awaiting independent review, integration sequencing, merge/intent gates and Ryan inspection. NOT running-product verified. No restart, rebuild, public push, merge, live model query, or full suite performed.

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
- Existing `tests/test_self_compact.py` integration cases success/failure/stop fail BEFORE the first request because fixture deepcopies a bound callback containing an asyncio Task. Same three failures reproduced against HEAD app and current app; six other tests pass. Not silently fixed. Baseline evidence retained at `temp-working-dir/self_compact_baseline.log`; report to leader for separate fixture repair.
- One intermediate 231-pass run had root-write teardown error because README was edited while tests were running. Corrected workflow and reran without edits: final 231-pass result above is clean.

## Inspection path for Ryan (after approved integration)

Use a standard 200k Claude ID and a `[1m]` selection separately; inspect window chip and configured threshold. Run an agentic tool task until threshold pause; expect compact card then one continuation without manual continue. Stop or submit new instructions around compaction; expect no stale continuation. Test unavailable context metadata and summary failure: held input remains visible/recoverable, no claimed success. Ordinary manual `/compact` with optional wake disabled should remain idle. Local-backend existing tool-loop threshold pause should compact then continue exactly once.
