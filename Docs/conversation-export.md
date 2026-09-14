# Export a saved conversation

```powershell
litetui --export-conversation "C:\path\to\convo.jsonl" --export-output "C:\path\to\conversation.md"
```

This reads the saved transcript and writes a new Markdown file. It does not start
the app, call a model, refresh native history, or modify the conversation. Export
after reopening and refreshing a Codex conversation to include recovered activity.
The output path must not already exist.

The document includes user and assistant messages, legacy tool messages, and saved
Codex tool/plan/compaction records. Native item identities prevent mirrored metadata
from duplicating the trace. Assistant phase/state, tool status, arguments, results
and known duration are retained. Missing duration is marked unknown.

Message and tool bodies are fenced as literal text. System instructions are not
part of the visible transcript export. Embedded PNG, JPEG, WebP and GIF images are
copied into a sibling `<output filename>.assets` folder and linked from the document.
Repeated images share a file. Keep this folder beside the Markdown when moving it.
Neither the output file nor its assets folder may already exist. Invalid embedded
images fail the export. Remote image URLs are not fetched and retain placeholders.
Saved Codex question
cards include titles, options, latest state and known delivery state. Queued answers
appear once; an answer already materialised as a user message is not repeated in
the question card. The source conversation remains the full saved record.
