# Saved question delivery status

Task: REL-20260914-CODEX-PARITY

A reopened pending question card previously changed to "Answer queued" for every
answered state, even after its steering entry was accepted, denied or uncertain.
It now follows the matching delivery ID and native thread. Queue, admission,
sending, acceptance, denial, next-turn fallback and uncertain delivery have distinct
labels. Missing or mismatched delivery evidence stays unconfirmed. A current
store persistence error displays "Answer state not saved" without claiming which
write failed or whether Codex accepted the answer.

The button remains disabled after submission; this change does not automatically
retry uncertain answers or invent native acceptance. A mounted Textual regression
drives queued/accepted/denied/uncertain transitions, persistence failure and a
foreign-thread delivery. Thirty async/native question lifecycle tests pass;
scoped Ruff and diff checks pass. Native live questions and GUI/Suite rendering
remain separate open acceptance gates.
