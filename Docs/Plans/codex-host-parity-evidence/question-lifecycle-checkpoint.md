# Native question lifecycle checkpoint

Task REL-20260914-CODEX-PARITY, LiteTUI isolated candidate.

## Implemented

`QuestionRequests` gives native server-request questions their own Textual worker
(or asyncio task in headless tests), deduplicates request IDs and rejects stale
turn/thread questions as unanswered. The turn reader continues processing text,
usage, progress and stop signals while the shared question widget is open.

A task-local lifetime Event crosses `asyncio.to_thread` into ask_user_question.
Native cancellation releases only that question's waiting thread and resolves its
RPC event or closes the matching Textual body. It matches the shared Event rather
than dismissing an unrelated screen. Stop releases questions before awaiting native
interrupt acknowledgment. Turn completion/disconnect/finally cancels remaining
questions; stale answers do not get sent into a closed turn.

The native reader owns interruption. Explicit cancellation of a blocking question
requests Stop; cancelling a nonblocking question sends unanswered data without
stopping work. Existing structured labels, IDs, free text and secret-input handling
remain in the shared question path. Legacy question polling/serialization stays intact.

## Evidence and boundaries

43 focused tests passed across native question lifecycle, structured questions,
app-server transport, existing RPC questions and existing abort-release regression.
This includes real shared tool threads, concurrent questions, stale/duplicate/late
requests, real Textual modal and sidebar closure, and the actual native stream reader
continuing text while unanswered then cleaning up on stop or synthetic disconnect.
No real account/provider inference was launched for this checkpoint.

Scoped Ruff checks passed for new lifecycle module, transport, structured-result seam
and lifecycle tests. ask_user_question.py retains exactly its seven HEAD baseline
findings (I001, F401, TRY004, two F821, RUF012, BLE001), verified using identical stdin
linting of HEAD and candidate. No additional lint findings remain in that file.

## Still open

This is not full C7 acceptance. `agentMessage.questions` from
`request_user_input_async` is a separate notification flow, whose answer must become
a new user message through durable admission/steering. It is not a pending JSON-RPC
request and must not receive a fabricated RPC reply. Its UI, persistence/replay,
late-answer delivery and all-client acceptance still need implementation. Full live
server question acceptance and packaged/runtime checks also remain open.

References checked against installed 0.154.0 schema and pinned official source:
- [Native request handling](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/app-server/src/bespoke_event_handling.rs): per-turn request cancellation and detached question response handling.
- [Blocking request tool](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/src/tools/handlers/request_user_input.rs): isBlocking depends on collaboration mode.
- [Async question tool](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/src/tools/handlers/request_user_input_async.rs): emits an async agentMessage with questions; user reply is a new message.
