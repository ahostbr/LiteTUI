# C7 async questions checkpoint

Task: REL-20260914-CODEX-PARITY. Candidate: feat/codex-host-parity, based on
8b2f1cbb8fa2be7979e0e539987b672499bc6bf4. This is partial implementation evidence,
not full C7 or release acceptance.

Native agentMessage notifications with delivery=async and questions now open the
shared human question UI independently of the native event reader. They are not
JSON-RPC requests. Explicit answers become ordinary user input through the existing
durable admission/steering queue. Pending questions and answer delivery identity are
persisted before presentation/queue visibility. Natural turn completion leaves the
question open; stop, conversation switch, and disconnect cancel its presentation
without inventing an answer. Read-only replay offers an explicit Answer question
button. Cancelled presentations can be reopened. Textual questions serialize through
one cancellable presentation slot; RPC questions retain independent IDs.

## Validation

From the candidate root with PYTHONPATH=src:

```powershell
python -m pytest tests/test_codex_async_questions.py tests/test_codex_question_lifecycle.py tests/test_codex_questions.py tests/test_codex_app_server.py tests/test_codex_steering.py tests/test_codex_tool_ui.py tests/test_codex_compaction_ui.py -q
```

Result: **59 passed in 4.47s**. Includes actual ConversationRepository disk reload,
duplicate restore, actual Textual replay button, synthetic native event reader,
stop/switch/disconnect, admission denial, save failure, queued dialog cancellation,
and omitted-stream negative control. Ruff check of all six changed source/test
files passed. No intentional native async-question runtime probe has been run.

## Test-boundary incident and correction

An earlier version of the synthetic reader test omitted stream=True. The production
transport deliberately routes app-bound nonstream calls without tools through an
isolated title/evaluator app-server. Consequently one unintended real provider call
with synthetic input `hello` occurred. The call returned and its isolated process
closed; process inspection found only the pre-existing desktop app-server, which
was left untouched. This incident is not acceptance evidence and was disclosed to
Ryan and Sentinel (message 132b3c5d-0aeb-433a-8cc8-8daa2f033333).

The reader test now uses stream=True. An autouse fixture makes AppServer.start fail
before process creation. test_omitted_stream_sidecall_is_blocked_before_real_process_start
reproduces the original omitted-stream route and verifies that exact guard failure.
The negative control did not repeat a real provider call.

## Remaining acceptance work

- Verify native async-question behavior with an intentional isolated runtime probe.
- Carry origin identity through all client question/reply surfaces and provide
  explicit restore UX beyond the Textual card.
- Reconcile duplicate persisted metadata snapshots and authoritative native history.
- Audit persistence-failure recovery UX and broader restart/approval interactions.
- Complete remaining C1-C10 and all-client/package gates. LiteGUI remains report-only
  pending Ryan's separately routed scope decision. Suite source remains uncommitted
  under its existing ownership restrictions. No release clearance is implied.
