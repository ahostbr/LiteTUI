# Native async answer and host persistence probe

Task: REL-20260914-CODEX-PARITY

The --reply variant of codex_async_question_probe.py completed two bounded native
Astra Medium turns in the same temporary thread. The first produced the exact
synthetic async question. After that turn ended, AsyncQuestions opened the actual
shared ask_user_question RPC handler. The fixture explicitly selected Blue via
resolve_over_rpc; it did not use a preselected value as an implicit submission.

ConversationRepository persisted the question and queued answer. The probe loaded
the conversation from disk, emptied the in-memory input queue, and invoked
restore_queue twice. It verified exactly one recovered entry with the same answer
text. A second native turn received that recovered text and returned BLUE_ACK.
Both turns completed, the app-server closed and the temporary home was removed.
native-async-answer-live.json records these boolean outcomes; scoped Ruff passes.

Boundary: the second native request is issued directly by the probe using
turn/start. This validates native notification -> shared RPC answer capture ->
repository persistence/reload -> native input compatibility. It does not validate
the installed clients' automatic queue flush, authorization/admission sequence,
delivery-ledger settlement, process-restart recovery, or GUI/Suite rendering.
The first notification-only evidence remains unchanged. No native command was
requested and the command-launch policy blocker was not bypassed.

Full C7 and release acceptance remain open. No merge, push or deployment occurred.
