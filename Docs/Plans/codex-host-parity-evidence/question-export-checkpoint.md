# C3 structured question export checkpoint

Markdown export now includes version-1 saved async question cards. It selects the
latest record using the same revision/terminal-state ordering as the UI, scopes
identity to the native thread, and emits each card once. Titles/options and saved
question state are literal text. For an answered question, the associated durable
delivery state is shown separately: queued does not mean accepted. The queued
answer text is included if no corresponding codex_delivery user message already
represents it. Export never submits or advances a question or delivery.

Validation: 23 export/async-question tests pass in 1.79s. New cases cover pending,
cancel, unanswered and answered states; an older pending copy after a newer revision;
queued versus materialised answer deduplication; and source-object preservation.
Scoped Ruff passes. No provider calls, app launch or external messages are part
of export. Documentation updated in Docs/conversation-export.md.

Attachment copying and remaining cross-client/full-plan gates remain open.
