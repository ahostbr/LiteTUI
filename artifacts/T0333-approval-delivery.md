# T0333 approval delivery — candidate evidence (2026-10-02)

## Scope and causal synthesis

Purpose: repair approval-delivery latency without weakening gates; trunk: reliable visible coordination.
Turing principle: “Understanding a process = the ability to formalize it into a machine that reproduces it.”

Original lost-native-send diagnosis is RETRACTED. Three native APPROVE timestamps match actual maildir replies. Requests first entered Ember's transcript after the 600s deadline; response latency after injection was 6–12s. Source: `C:/Projects/.scratch/ember-lane/T0333-delivery-evidence.md` and `C:/Projects/LiteTUI/.convos/f4c6dadd-27e7-49da-9b90-048e13bffa44/convo.jsonl`.

Safe native TEST (one authorized send to Ember, no approvals): mailbox file `C:/Users/Ryan/.liteharness/inbox/done/20261002_182535__normal__c57ec793a44a.json`, id `0e44e937-4502-4185-9e47-f12842e30ba5`, timestamp `18:25:35.240320Z`; leader measured USER injection `18:29:40.251Z`: **245.01068s mailbox→transcript**. Self-mail and fabricated recipient attempts refused before delivery as designed; neither is a send defect.

Readonly extraction shows ordinary messages injected after successive tool results while requests lagged, plus a ~320s compaction stall. No steering journal on affected incoming messages: FIFO backlog+compaction is supported; uncertain native steering is not established as the historical cause.

Reproducer: 80 routine messages then an approval request through real `_deliver_inbox`/`_deliver_queued_input`; initial safe boundary consumed `progress0` instead of approval (red). Candidate prioritizes the request at the next safe boundary.

## Contract implemented

- Ordinary body text never grants priority, even a complete copied APPROVAL envelope.
- Relay `Seat.send` uses existing authoritative CLI/body-file writer with supported `--type QUESTION`, `--thread-id` containing bounded JSON `{kind: approval-request, id, requester, approver}`. No new mailbox writer, dependency, sys.path hack or OSS edits.
- Only exact typed kind/schema, nonce, actual sender and addressee/frozen approver (or its current registered spawner for notification) receive priority. Local mailbox provenance is still forgeable; priority does not grant approval authority.
- Priority remains stable within urgent/routine classes. Claude-owned/native-journaled/interrupt entries remain barriers. No cancellation, ownership transfer or uncertain-send retry.
- Creation-origin 60s completion contract: dispatch notification at approximately49s from request creation, bound CLI completion at59s, reserve1s for durable visible state by60s on a responsive host. Initial send/UI setup count toward these deadlines and cannot postpone dispatch. Notification targets approver's own registered spawner by exact id/launch lineage; no names/hardcoded UUID. Same structured priority and original approver metadata. Original future and reply authority stay frozen; original answer expiry also runs from creation.
- Notification sent/absent/failed state saved in request conversation metadata, visible system notice, bounded runtime stage. No autoapprove. Normal timeout unchanged.
- Runtime stages distinguish `sent`, `received`, `queued`, `injected`, `responded`, and escalation transport state. Injected means host input admission, NOT proof of provider processing. Maildir write means transport, NOT model receipt.

## Verification

- `python -m pytest tests/test_approval_delivery_t0333.py tests/test_message_queue.py tests/test_approval_relay_t1049b.py tests/test_approval_relay_t1049b2.py tests/test_codex_steering.py tests/test_harness_takeover_hint.py -q`: 117 passed.
- New focused file: 20 passing tests, including actual installed CLI parser/writer sandbox roundtrip and same-metadata escalation; all sandbox HOME/presence/mailbox isolated. Requires installed CLI on base interpreter; clean machines without it fail this explicit integration proof, rather than pretending it passed.
- `python -X utf8 tests/test_harness_tool.py`: 30/30 passed (UTF-8 required on Windows console).
- New module/test/helper Ruff and new-module mypy `--follow-imports=skip`: passed. Existing touched-module Ruff has baseline import/broad-exception/timezone findings; no unrelated cleanup.
- Legacy `test_human_relay_override_t0183.py`: 3 failures (no registered parent fixture, no send). Same failures with committed156f7b8 harness+relay dynamically substituted. This is **module-substitution comparison**, not archived-tree validation; do not claim full baseline suite.
- `temp-working-dir/t0333_verify_diff.py`: app prefix/suffix byte-identical; original 10 bare-LF lines preserved. `git -c core.whitespace=cr-at-eol diff --check` clean. No whole-file normalization.

## Held boundaries / next owner

NOT DONE. No merge/push/restart/live destructive probes. Ryan end inspection outstanding.
Initial hold required T0245+T0250 to land before authorized rebase and combined review. This prerequisite is now met as recorded below; independent combined review and Ryan inspection remain outstanding.
Seconds cannot be guaranteed while native provider/compaction is blocked. Sentinel defined this card's fallback as durable spawner-inbox transport notification plus visible saved state, NOT out-of-band UI interruption or guaranteed model receipt. Slow real-use follow-up remains outside this card.

## Authorized rebase / combined verification (2026-10-02)

- Verified `main` exact `72706a4118abddbe6228ac6abc27d9092279db02` and original156f7b8 ancestor. Rebased d269e0e cleanly, no conflicts, to `cb2b4110aa38707e374bed9c569198fdbca01a15`.
- Combined regression exposed a semantic collision: T0250 `hold_input` retains ownership via `_claude_segment`/`_claude_conversation` **without** `_claude_entry`. Initial T0333 priority barrier overlooked this shape. Two actual hold-input/approval regressions failed before the minimal fix: recognize `_claude_segment` as a priority barrier. Recovery files and gate logic are not edited.
- Hooks-enabled and hooks-disabled paths now retain held owner at head, urgent request behind it, with ledger bytes/segment/conversation unchanged after a backend switch. Both exercise the real merged before-pop gates.
- Project runner: `$env:PYTHONPATH="$PWD/src"; C:/Projects/LiteTUI/.venv/Scripts/python.exe -m pytest tests/test_approval_delivery_t0333.py tests/test_message_queue.py tests/test_approval_relay_t1049b.py tests/test_approval_relay_t1049b2.py tests/test_codex_steering.py tests/test_harness_takeover_hint.py tests/test_recovery_composition.py tests/test_claude_uncertain_startup.py tests/test_compaction_busy_input.py tests/test_compaction_recovery.py tests/test_claude_compact.py -q` → **200 passed**, 19.84s. Focused T0333 file includes22 tests now.
- System interpreter combined invocation initially failed collection (`tests.test_card_summary` shadowed by unrelated installed tests package); project venv runner resolves it without source/sys.path changes. Sandbox CLI subprocess still explicitly uses base interpreter with installed CLI.
- Ruff on new module/test/helper PASS; mypy new module `--follow-imports=skip` PASS; harness script UTF8 **30/30 PASS**. CR-at-eol diffcheck PASS.
- `temp-working-dir/t0333_rebased_bytes.py` confirms Claude turn/compact sources byte-identical to72706a4, and all app/hook bytes outside original T0333 insertions unchanged. `queue_ready` remains before pop in both app boundaries and hooks-enabled queued prompt.
- No main merge, push, product restart, download, deletion, new card or scope expansion. Candidate awaits one independent combined-queue reviewer and Ryan end inspection; later standing order supersedes retirement: worker remains active until merge/drop.

## Independent review fixes R1/R2 (2026-10-02)

- R1: additional actual `queue_ready` startup-binding regression verifies `_claude_segment` without entry remains a priority barrier, alongside actual `hold_input` regressions.
- R2: corrected former wait60s-then-send30s behavior. Separate monotonic original answer deadline from escalation completion deadline. Production starts transport at49s, reserves10s CLI budget ending59s, and1s for visible saved state before60s. Early notification is intentional to satisfy the completion contract; no original answer timeout inflation.
- Original answer timeout<60s now expires without starting an escalation after expiry. Inbox and local-human replies are refused after the original answer deadline; a precompleted late future is also rejected.
- Transport deadline reaches actual existing authoritative CLI subprocess timeout, computed after metadata/body-file preparation. Real installed CLI sandbox delayed0.4s with0.08s budget is killed/reaped by subprocess.run; after another0.45s no extra mailbox file exists. This is not merely cancellation of a threadpool waiter.
- An outer timeout still records visible saved `escalation_failed`/unconfirmed state, never success or a retry. OS scheduling/filesystem stalls cannot be hard-bounded; the CLI checks remaining deadline before launch and positive receipt before deadline. A publication whose acknowledgement is uncertain stays failed/unconfirmed, without approval authority changes.
- Budget-scaled delayed transport tests cover success and failure, durable request state, absent-spawner state, short original expiry and late inbox/human/future rejection. Structured priority and frozen original approver metadata remain unchanged.
- Same11-file project-venv command above: **207 passed**,42.40s. New-file Ruff PASS; new-module focused mypy PASS; recovery/app/hook byte proof PASS; CR-at-eol diffcheck PASS. No recovery source edits.

## Creation-origin review correction (2026-10-02)

- Prior exact d2a1b18846e30f71d4d60b2f25996ddbbe404b4d approval was SUPERSEDED by REQUEST-CHANGES: its clock began after initial send/UI. Sentinel's binding clock starts when the seat raises the request, end-to-end. This candidate captures monotonic creation at the first line of `ask_spawner`; both original answer expiry and49/59/60 escalation budget derive from it.
- Pending/frozen authority and original deadline are registered synchronously before responses. Independent deadline processing is scheduled before initial transport/UI setup. Initial send is bounded by min(creation+30s, original answer deadline); setup cannot postpone notification. Immediate transport refusal remains absent, but late setup cannot overwrite a settled request. Setup/timer cancellation and deadline/pending cleanup are awaited on every settled or cancelled path.
- Actual `ask_spawner` delayed initial transport and stalled UI mount tests independently observe escalation mailbox write and durable state within scaled60s while setup is still pending; only the frozen original approver settles DENY. Actual short-expiry/cancellation regressions verify original send/answer deadlines match, no post-expiry escalation, cleared pending/deadlines, rejected late inbox/human answers, and no late setup notice.
- First combined run: **208 passed,1 failed**. Test assumed remaining scaled transport budget<=0.101s; measured Windows monotonic is GetTickCount64 with0.015625s resolution, allowing an asyncio timer to wake one tick early. Corrected test derives tolerance from `time.get_clock_info('monotonic').resolution` AND asserts absolute transport deadline equals creation+0.6-0.01 exactly. Only the timer wake/remaining duration varies; the absolute deadline and production budgets are unchanged.
- Final focused approval+relay3files: **91 passed**,27.47s. Final same11-file project-venv suite: **211 passed**,33.45s; log `C:/Projects/LiteTUI/output/tasks/t-6178ad76904d42358a284ee6f03cb823.log`. New-file Ruff, new-module focused mypy, recovery/app/hook byte proof against72706a4 and CR-at-eol diffcheck PASS.
- NOT DONE: exact new SHA requires independent re-review, Sentinel merge gate and Ryan end inspection. No main merge/push/restart or recovery-source edits. Responsive-host timing proof is not a guarantee through a blocked filesystem/OS or provider; transport receipt remains explicitly unconfirmed.
