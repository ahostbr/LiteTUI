"""Read-only Markdown projection of saved visible conversation activity."""

import base64
import binascii
import hashlib
import re
from types import SimpleNamespace
from urllib.parse import quote

from litetui.codex_async_questions import latest_question
from litetui.codex_trace import records
from litetui.conversation import ConversationRepository


def literal(text):
    text = str(text or "")
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def image_url(block):
    value = block.get("image_url", block.get("imageUrl", block.get("url")))
    return value.get("url") if isinstance(value, dict) else value


def content(value, image_refs=None):
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
            reference = (image_refs or {}).get(image_url(block))
            parts.append(f"[Image attachment: {reference}]" if reference else
                         "[Image attachment; image bytes are not included in this text export]")
    return "\n\n".join(parts)


def markdown(messages, image_refs=None):
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
    seen_questions = set()
    materialised_deliveries = {(message.get("codex_delivery") or {}).get("id") for message in messages}
    question_source = SimpleNamespace(conversation=messages)
    sections = ["# Conversation"]
    for message in messages:
        meta = message.get("provider_metadata") or {}
        trace = records(meta)
        role = message.get("role")
        text = content(message.get("content"), image_refs)
        traced_answer = any(r.get("kind") == "agentMessage" and r.get("result") for r in trace)
        if text and role in ("user", "assistant", "tool") and not (role == "assistant" and traced_answer):
            label = {"user": "You", "assistant": "Assistant", "tool": "Tool"}[role]
            sections.append(f"## {label}\n\n{literal(text)}")
        entries = meta.get("async_questions", [])
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get("version") != 1 or not entry.get("id"):
                continue
            key = (entry.get("threadId"), entry["id"])
            if key in seen_questions:
                continue
            found = latest_question(question_source, entry["id"], entry.get("threadId"))
            if found is None:
                continue
            owner, current = found
            seen_questions.add(key)
            lines = [f"State: {current.get('state', 'unknown')}"]
            questions = current.get("questions", [])
            for question in questions if isinstance(questions, list) else []:
                if isinstance(question, dict):
                    lines.append(str(question.get("title", "")))
                    options = question.get("options", [])
                    if isinstance(options, list):
                        lines.extend(f"Option: {option}" for option in options if isinstance(option, str))
            delivery_id = current.get("deliveryId")
            if current.get("state") == "answered" and delivery_id:
                deliveries = owner.get("steering", [])
                delivery = next((item for item in deliveries if isinstance(item, dict)
                                 and item.get("id") == delivery_id), None) if isinstance(deliveries, list) else None
                if delivery:
                    lines.append(f"Delivery: {delivery.get('state', 'unknown')}")
                    if delivery_id not in materialised_deliveries:
                        lines.append(content((delivery.get("item") or {}).get("content")))
            sections.append("## Codex question\n\n" + literal("\n\n".join(lines)))
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
    assets = destination.with_name(destination.name + ".assets")
    files, references = {}, {}
    extensions = {"png": "png", "jpeg": "jpg", "webp": "webp", "gif": "gif"}
    for message in messages:
        blocks = message.get("content")
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict) or block.get("type") not in ("image_url", "input_image"):
                continue
            url = image_url(block)
            if not isinstance(url, str) or not url.startswith("data:"):
                continue  # Never fetch remote attachments while exporting.
            match = re.fullmatch(r"data:image/(png|jpeg|webp|gif);base64,(.*)", url, re.DOTALL)
            if not match:
                raise ValueError("Unsupported embedded image format in conversation")
            try:
                data = base64.b64decode(match[2], validate=True)
            except binascii.Error as error:
                raise ValueError("Invalid embedded image in conversation") from error
            name = hashlib.sha256(data).hexdigest() + "." + extensions[match[1]]
            files[name] = data
            references[url] = assets.name + "/" + name
    text = markdown(messages, references)
    if references:
        text += "\n## Image attachments\n\n" + "\n\n".join(
            f"![Image attachment {index}](<{quote(reference)}>)"
            for index, reference in enumerate(dict.fromkeys(references.values()), 1)
        ) + "\n"
    created = []
    made_assets = False
    with destination.open("x", encoding="utf-8") as stream:
        try:
            if files:
                assets.mkdir()
                made_assets = True
                for name, data in files.items():
                    target = assets / name
                    with target.open("xb") as image:
                        created.append(target)
                        image.write(data)
            stream.write(text)
        except BaseException:
            stream.close()
            for target in created:
                target.unlink()
            if made_assets:
                assets.rmdir()
            destination.unlink()
            raise
