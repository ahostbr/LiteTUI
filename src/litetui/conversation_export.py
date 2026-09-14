"""Read-only Markdown projection of saved visible conversation activity."""

import re

from litetui.codex_trace import records
from litetui.conversation import ConversationRepository


def literal(text):
    text = str(text or "")
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def content(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts = []
    for block in value:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("text", "input_text"):
            parts.append(str(block.get("text", "")))
        elif block.get("type") in ("image_url", "input_image"):
            parts.append("[Image attachment; image bytes are not included in this text export]")
    return "\n\n".join(parts)


def markdown(messages):
    # Later saved snapshots win, but each native item keeps its first visible
    # position. Metadata may be mirrored on both user and assistant records.
    latest = {}
    for message in messages:
        meta = message.get("provider_metadata") or {}
        thread = meta.get("app_server_thread_id")
        for record in records(meta):
            if record.get("id"):
                latest[(thread, record.get("turnId"), record["id"])] = record
    seen = set()
    sections = ["# Conversation"]
    for message in messages:
        meta = message.get("provider_metadata") or {}
        trace = records(meta)
        role = message.get("role")
        text = content(message.get("content"))
        traced_answer = any(r.get("kind") == "agentMessage" and r.get("result") for r in trace)
        if text and role in ("user", "assistant", "tool") and not (role == "assistant" and traced_answer):
            label = {"user": "You", "assistant": "Assistant", "tool": "Tool"}[role]
            sections.append(f"## {label}\n\n{literal(text)}")
        for saved in trace:
            key = (meta.get("app_server_thread_id"), saved.get("turnId"), saved.get("id"))
            if not key[2] or key in seen:
                continue
            seen.add(key)
            record = latest[key]
            if record.get("kind") == "agentMessage":
                sections.append("## Assistant\n\n" + literal(
                    f"Phase: {record.get('phase') or 'unspecified'}; state: {record.get('state', 'unknown')}"
                ) + "\n\n" + literal(record.get("result")))
                continue
            duration = record.get("durationMs")
            timing = f"{duration / 1000:g}s" if type(duration) in (int, float) and duration >= 0 else "unknown duration"
            sections.append("## Tool activity\n\n" + literal(record.get("name", record.get("kind", "Codex tool")))
                            + f"\n\nStatus: {record.get('state', 'unknown')} · {timing}"
                            + "\n\nArguments:\n\n" + literal(record.get("args"))
                            + "\n\nResult:\n\n" + literal(record.get("result")))
    return "\n\n".join(sections) + "\n"


def export(source, destination):
    _, messages = ConversationRepository.read(source)
    text = markdown(messages)
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(text)
