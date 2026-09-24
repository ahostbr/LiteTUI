# Claude native backend — implementation and E2E verification complete

Ryan's goal `6df388f49d27`: add Claude support to LiteTUI end to end.

**Outcome:** the experimental local integration is implemented, committed locally, and verified through actual SDK, Textual, and spawned JSONL RPC paths. **Nothing was pushed. Ryan has not personally verified it. Distribution authorization remains a separate open gate.**

## Runtime, authentication, and scope

- Pinned Python `claude-agent-sdk==0.2.159`, bundled CLI `2.1.281`.
- Claude owns its runtime, native tools, agent loop, context, and automatic compaction. LiteTUI owns presentation, durable input admission, and guarded host services through in-process MCP. Displayed native activity is never re-executed by a host loop.
- Authentication follows LiteSuite ClaudeAdapter: the official SDK/CLI handles existing Claude Code login and inherited environment. No token extraction, custom OAuth, or mandatory API-key onboarding.
- Mandatory `PreToolUse` policy gates native tools even when native allow rules would bypass `can_use_tool`. Host services also use the existing LiteTUI dispatcher.
- Text/thinking, native activity, questions, approvals, stop/continue, model selection, durable busy queue, exact session resume, and recovery commands are integrated.

## Final verification

| Gate | Result and evidence |
|---|---|
| SDK context, exact resume, missing-ID refusal, native denial | PASS; `claude-sdk-live-evidence.json` summarizes the full local `claude-p0-live.json` |
| TUI two turns and recreated-backend resume | PASS; `claude-tui-smoke.json`; SVG visually inspected: native `think:default`, truthful unknown context ceiling |
| Actual RPC process | PASS; queue, approved/denied native Write, answered question text, abort pending question then correct next answer, host MCP call exactly once, exit 0; `claude-rpc-evidence-summary.json` |
| Tools-off despite advertised/autoallowed native and MCP tools | PASS; no native file or host execution; Agent launch denied; `claude-negative-probe.json` |
| Unanswered approval timeout | PASS; real SDK Write denied with owned decision cancelled; accelerated 0.2s local/2s hook deadlines, not a five-minute wait; `claude-timeout-probe.json` |
| Settings hooks, external MCP, installed plugin isolation | PASS positive/negative pair; control has 4 hook starts, 3 MCP servers, project/plugin markers; production options have zero hooks/servers/markers; `claude-p0-plugin-ctrl.json`, `claude-p0-plugin-neg.json` |
| Actual OS-process restart via GUI conversation-open | PASS; exact native identity, remembered random nonce, no foreign-history import, no owned child survivors; `claude-restart-probe-shareable.json` |
| Actual OS-process restart via `--convo` | PASS after repairing the dead startup flag; `claude-restart-cli-evidence.json` |
| Forced stuck shutdown | Before: close still pending at 25s and owned child alive. After: close returned in 22.2s, verified owned child gone, escalation explicitly reported; `claude-close-escalation-before-evidence.json`, `claude-close-escalation-evidence.json` |

Final live TUI + RPC + CLI restart repeated **after cleanup commit `fd9b568`**, all PASS (task `t-90a85050d6e84e61aa6e212b56f3c9ad`, 48s). The parent independently repeated the forced-shutdown probe (task `t-5e430a4d95244494b2e626712d333219`); its cleanup found the child already gone.

The restart probe inserts foreign-provider display records rather than running inference on a second backend. It proves the import boundary, not live second-provider inference. Normal/forced child identity checks use PID plus creation time, never process-name killing. The artificial shutdown wedge ignores even child death; its still-pending Python owner is reported explicitly, then released by the probe. Production teardown does not claim that owner finished when it did not.

## Tests and project checks

- **Final nine focused suites: 208 passed** (all eight Claude suites plus CLI conversation resume). Scoped Ruff clean for Claude modules/tests/probes; scoped mypy clean for six Claude modules.
- **72 affected lifecycle/backend/turn tests passed** after forced-cleanup changes.
- Exact partially staged source snapshot: **218 passed**, then **45 passed** after the stale-segment admission fix; CLI follow-up exact staged snapshot: **58 passed**. Independent Sentinel clean detached integration checkout: **207 passed**. These checks demonstrate the commits did not depend on Ryan's unrelated unstaged changes.
- Earlier broader isolated regression: 572 tests in 47 suites, 565 passed/7 failed. Two introduced settings-save failures shared one missing-control cause and were fixed; subsequent settings tests passed. Other five failures reproduced without the integration. See `claude-regression-report-shareable.json` (snapshot evidence, not the final test count).
- Additional settings/palette/slash checks: 28 passed/one preexisting duplicate Browser monitor failure. Additional hooks/launch/settings checks: 77 passed/one preexisting hook-denial failure. Twelve existing Codex/local thinking-persistence failures also reproduce on archived HEAD. Raw baseline evidence remains local.
- Source-wide Ruff: baseline **463**, final **461** findings. Source-wide mypy: baseline **187 errors/28 files**, final **185/27**. Normalized diagnostic multiset comparison finds **no added diagnostics**; removed diagnostics come from unrelated STT changes. These are not globally clean passes.
- No `run_all.py` full-suite gate was requested or run. Tests were selected by affected behavior. Quiet snapshots avoid concurrent live-harness root-file changes without weakening the checkout-root guard.

## Important fixes verified along the way

- Cancellation propagates after cleanup; provider-terminal drain and host-terminal consumption remain distinct admission boundaries.
- All owned close handles are retained; failed cleanup cannot permanently poison later connects. Pending cleanup blocks sending. Real TUI shutdown closes Claude even after switching away.
- Stale segment/conversation input remains prepared and held rather than being submitted to a session the user no longer selected.
- Settings includes the executable override control, so saving any tab no longer fails because that field lacked a widget.
- Thinking snapshot replacement avoids duplicate rendering; context fields update in watcher-safe order; native terminal errors preserve detail even when subtype says `success`.
- `/claude` recovery is discoverable through the command registry/palette/RPC. Live permission changes are distinguished from fixed session tool inventory.
- `--convo` now resumes before launch input; missing or ambiguous identity blocks the launch prompt instead of silently sending it into a fresh conversation.

## Deliberate limitations and release gate

- **Distribution/subscription-login authorization is unresolved.** Local operation through the official SDK does not establish permission to distribute that integration. Keep the backend experimental until Ryan resolves that gate.
- Native agents/background launches, images, native slash passthrough, host manual compaction, legacy subagent/summary calls, local loading/sampling controls, and host goal-loop followups are explicitly unsupported/refused.
- String queries do not provide stable native user-message UUID correlation. Local delivery IDs are not falsely presented as provider acknowledgment.
- Native activity cards persist at turn settlement, not individually during a crash. SDK history is authoritative; in-flight ledger entries become uncertain and are never automatically replayed.
- `/claude status`, `resolve` (without replay), `continue` (prepared inputs only), and `new` provide recovery. No old-segment picker or rollback.
- Tool inventory and creation-time prompt/configuration require a new session to refresh. Authority checks remain live per call.
- Usage from results is separate from message occupancy. Unknown values remain unknown; cumulative cost is not labeled a per-turn delta.
- Forced process-tree cleanup is verified on Windows. Other platforms do not receive an unsafe PID-only kill; inability to verify ownership is reported instead.

## Commits and preservation of user work

Core integration: **67a1469**. Restart probe: **11b34eb**. RPC/config gate refinements: **b79e089**, **c0adaf8**. Native error detail: **9df744b**. CLI resume: **ea44639**. Forced cleanup: **fd9b568**. Earlier SDK/events/session/ledger commits remain in history.

Preexisting Ryan edits in image/STT/compaction/question/settings paths and their tests were preserved unstaged. Shared files were staged by owned hunks and tested from the exact index. No push.

## Installation note

Install with LiteTUI closed: `uv sync --locked --extra claude`; authenticate through `claude auth login` if needed, then launch with `--backend claude` or choose `/backend claude`.

The initial plain sync could not replace this workstation's running `litetui.exe`; no user app was killed. The pinned SDK was installed via `uv pip install --python .venv/Scripts/python.exe claude-agent-sdk==0.2.159`, and an isolated worktree extra sync succeeded. Sentinel's separate **7788f5e** launcher fix syncs dependencies without reinstalling the running project entry point and launches the CLI module. That solves the launcher path, not the historical failed plain-sync attempt.
