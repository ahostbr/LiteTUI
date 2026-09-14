"""Display-only native trace stored in provider metadata, never model input."""

from litetui.widgets import FoldBlock, ToolMessage


def records(metadata):
    trace = (metadata or {}).get("display_trace", {})
    if not isinstance(trace, dict) or trace.get("version") != 1:
        return []
    items = trace.get("items", [])
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def replay(app, metadata, seen):
    count = 0
    thread = metadata.get("app_server_thread_id")
    for record in records(metadata):
        key = (thread, record.get("turnId"), record.get("id"))
        if not record.get("id") or key in seen:
            continue
        seen.add(key)
        if record.get("kind") == "agentMessage":
            if record.get("result"):
                card = app._assistant_bubble()
                card.body.set_markdown(record["result"])
            continue
        if record.get("kind") == "plan":
            app.query_one("#chat-log").mount(
                FoldBlock("Codex plan", record.get("result", ""), expanded=False)
            )
            continue
        count += 1
        card = ToolMessage(record.get("name", "Codex tool"))
        app.query_one("#chat-log").mount(card)
        card.set_args(record.get("args", ""))
        duration = record.get("durationMs")
        terminal = record.get("state") != "running"
        card.set_result(
            record.get("result", "")
            if terminal
            else "No completion was saved before this conversation closed.",
            bool(record.get("ok")) if terminal else False,
            elapsed=duration / 1000 if isinstance(duration, (int, float)) else None,
            duration_unknown=not isinstance(duration, (int, float)),
        )
    return count
