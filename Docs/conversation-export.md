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
part of the visible transcript export. Image attachments receive a placeholder;
this text export does not copy attachments or embed their bytes. Structured question
cards are not yet included. The source conversation remains the full saved record.
