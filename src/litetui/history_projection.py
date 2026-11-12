"""Pure, bounded historical display blocks. Never tools, providers, widgets or writes.

Canonical JSONL replay stays in ConversationRepository. This module consumes its
semantic messages and content-keyed card summaries, keeping the all-field context
proof independent from the smaller presentation that a native client retains.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from litetui.recap import split_recap

VERSION = 1
MAX_CONTEXT = 8 * 1024 * 1024
MAX_PROJECTION = 16 * 1024 * 1024
MAX_COUNT = 1_000_000
PAGE_FRAGMENTS = 50
PAGE_UNITS = 24_576
KINDS = frozenset({"user", "assistant", "reasoning", "tool", "plan", "summary", "attachment", "notice", "raw"})
STATES = frozenset({"complete", "failed", "cancelled", "incomplete", "unknown"})


def encoded(value: Any, limit: int = MAX_CONTEXT) -> bytes:
    """Bound actual canonical encoding, including escaped control characters."""
    chunks: list[bytes] = []
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    for chunk in encoder.iterencode(value):
        data = chunk.encode("utf-8")
        size += len(data)
        if size > limit:
            raise ValueError("History exceeds its semantic/projection byte budget")
        chunks.append(data)
    return b"".join(chunks)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text(value: Any, limit: int, *, empty: bool = True) -> str:
    if not isinstance(value, str) or (not empty and not value) or len(value.encode("utf-8")) > limit:
        raise ValueError("History contains invalid or oversized text metadata")
    return value


def _string(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else encoded(value).decode("utf-8")


def metadata(provider: str | None = None, **values: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"provider": provider, "thread_id": None, "turn_id": None,
                              "item_id": None, "tool_call_id": None, "ok": None,
                              "duration_ms": None, "attachment_kind": None}
    result.update(values)
    if set(result) != {"provider", "thread_id", "turn_id", "item_id", "tool_call_id", "ok", "duration_ms", "attachment_kind"}:
        raise ValueError("Unknown historical display metadata")
    if result["provider"] not in (None, "claude", "codex"):
        raise ValueError("Invalid historical provider")
    for key in ("thread_id", "turn_id", "item_id", "tool_call_id"):
        if result[key] is not None:
            text(result[key], 1024, empty=False)
    if result["ok"] is not None and type(result["ok"]) is not bool:
        raise ValueError("Invalid historical tool outcome")
    duration = result["duration_ms"]
    if duration is not None and (type(duration) is not int or not 0 <= duration <= 2_147_483_647):
        raise ValueError("Invalid historical duration")
    if result["attachment_kind"] not in (None, "embedded", "remote", "unknown"):
        raise ValueError("Invalid historical attachment kind")
    return result


@dataclass(frozen=True)
class Block:
    index: int
    message_index: int
    message_sha256: str
    kind: str
    role: str
    label: str
    state: str | None
    metadata_bytes: bytes
    content: str
    block_sha256: str
    block_id: str
    # Source-local index into the original message content. Never projected on the wire.
    content_index: int | None = None

    def fields(self) -> dict[str, Any]:
        return {"block_index": self.index, "block_id": self.block_id,
                "message_index": self.message_index, "message_sha256": self.message_sha256,
                "kind": self.kind, "role": self.role, "label": self.label, "state": self.state,
                "metadata": json.loads(self.metadata_bytes), "block_sha256": self.block_sha256}


#: A brand-new conversation holds only system context. Develop's host prompt message (app.
#: _host_prompt_message, seat_prompts) carries its provenance on message 0; those keys are still
#: system context, not history.
FRESH_SYSTEM_KEYS = frozenset({
    "role", "content", "litetui_prompt_owner", "litetui_prompt_version", "litetui_prompt_base",
    "litetui_prompt_ordinary_start", "litetui_ordinary_text", "litetui_seat_text",
})


@dataclass(frozen=True)
class Projection:
    context_sha256: str
    replay_sha256: str
    messages: int
    system: int
    fresh_system_context: bool
    blocks: tuple[Block, ...]

    def counts(self) -> dict[str, int]:
        return {"messages": self.messages, "system": self.system,
                "non_system": self.messages - self.system, "blocks": len(self.blocks)}


class _Builder:
    def __init__(self, messages: list[dict[str, Any]], summaries: dict[str, str]) -> None:
        self.messages = messages
        self.summaries = summaries
        self.blocks: list[Block] = []
        self.size = 0
        self.hash = hashlib.sha256()
        self.message_hashes = [digest(encoded(message)) for message in messages]

    def add(self, index: int, kind: str, body: Any, *, label: str = "", state: str | None = None,
            meta: dict[str, Any] | None = None, content_index: int | None = None) -> None:
        if kind not in KINDS or state is not None and state not in STATES:
            raise ValueError("Invalid historical display block")
        if len(self.blocks) >= MAX_COUNT:
            raise ValueError("History exceeds the display block count budget")
        role = text(self.messages[index]["role"], 32, empty=False)
        label = text(label, 1024)
        content = _string(body)
        raw = content.encode("utf-8")
        values: dict[str, Any] = {"block_index": len(self.blocks), "message_index": index,
                  "message_sha256": self.message_hashes[index], "kind": kind,
                  "role": role, "label": label, "state": state,
                  "metadata": meta or metadata(), "block_sha256": digest(raw)}
        header = encoded(values, 16 * 1024)
        self.size += len(header) + len(raw)
        if self.size > MAX_PROJECTION:
            raise ValueError("History exceeds the 16 MiB display projection budget")
        ident = digest(header)
        self.hash.update(len(header).to_bytes(8, "big"))
        self.hash.update(header)
        self.blocks.append(Block(len(self.blocks), index, self.message_hashes[index], kind, role,
                                 label, state, encoded(values["metadata"], 8192), content,
                                 values["block_sha256"], ident, content_index))

    def answer(self, index: int, body: str, *, meta: dict[str, Any] | None = None,
               state: str | None = "complete") -> None:
        shown, recap = split_recap(body, final=True)
        if shown:
            self.add(index, "assistant", shown, label="Assistant", state=state, meta=meta)
        key = hashlib.sha1(body.strip().encode("utf-8")).hexdigest()[:16]
        summary = recap or self.summaries.get(key)
        if summary:
            self.add(index, "summary", summary, label="Saved summary", meta=meta)

    def content(self, index: int, role: str, content: Any) -> None:
        if isinstance(content, str):
            if role == "assistant":
                self.answer(index, content)
            else:
                self.add(index, role if role in ("user", "tool") else "raw", content, label=role.title())
            return
        if content is None:
            return
        if not isinstance(content, list):
            self.add(index, "notice", "Unsupported saved content representation; retained in source context.", label="Saved content")
            return
        for content_index, item in enumerate(content):
            if not isinstance(item, dict):
                self.add(index, "notice", "Unsupported saved content block; retained in source context.", label="Saved content block")
            elif item.get("type") in ("text", "input_text") and isinstance(item.get("text"), str):
                self.content(index, role, item["text"])
            elif item.get("type") in ("image_url", "input_image"):
                from litetui.conversation_export import image_url
                url = image_url(item)
                attachment = ("embedded" if isinstance(url, str) and url.startswith("data:") else
                              "remote" if isinstance(url, str) and url.startswith(("https://", "http://")) else "unknown")
                self.add(index, "attachment", "Saved image attachment; bytes are not loaded in this view.",
                         label="Image attachment", meta=metadata(attachment_kind=attachment), content_index=content_index)
            else:
                self.add(index, "notice", "Unsupported saved content block; retained in source context.", label="Saved content block")


def _trace(message: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from litetui.codex_trace import records
    saved = message.get("provider_metadata")
    saved = saved if isinstance(saved, dict) else {}
    return saved, records(saved)


def _state(value: Any) -> str:
    if value in ("complete", "completed"):
        return "complete"
    if value in ("failed", "declined"):
        return "failed"
    if value in ("cancelled", "canceled"):
        return "cancelled"
    return "incomplete" if value in ("running", "inProgress", "interrupted") else "unknown"


def _claude_tools(builder: _Builder, index: int, activities: list[dict[str, Any]]) -> None:
    # The canonical widget keeps first tool position and updates its saved result.
    ordered: dict[str, list[dict[str, Any]]] = {}
    for number, activity in enumerate(activities):
        ident = activity.get("id")
        key = ident if isinstance(ident, str) and ident else f"missing:{number}"
        ordered.setdefault(key, []).append(activity)
    for rows in ordered.values():
        first = rows[0]
        ident = first.get("id")
        meta = metadata("claude", tool_call_id=ident if isinstance(ident, str) and ident else None)
        use = next((row for row in reversed(rows) if row.get("kind") == "tool_use"), None)
        result = next((row for row in reversed(rows) if row.get("kind") != "tool_use"), None)
        arguments = _string(use.get("input") or {}) if use else ""
        outcome = _string(result.get("result")) if result else "No completion was saved before this conversation closed."
        if result is not None:
            meta["ok"] = not bool(result.get("is_error"))
        builder.add(index, "tool", f"Arguments:\n{arguments}\n\nResult:\n{outcome}",
                    label="Claude - " + str(first.get("name") or ident or "tool"),
                    state="complete" if result is not None and meta["ok"] else "failed" if result is not None else "incomplete",
                    meta=meta)


def _latest_questions(messages: list[dict[str, Any]]) -> dict[tuple[str, str | None], tuple[int, dict[str, Any]]]:
    from litetui.codex_async_questions import question_rank

    latest: dict[tuple[str, str | None], tuple[int, dict[str, Any]]] = {}
    for index, message in enumerate(messages):
        saved = message.get("provider_metadata")
        entries = saved.get("async_questions", []) if isinstance(saved, dict) else []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            ident, thread = entry.get("id"), entry.get("threadId")
            if not isinstance(ident, str) or not ident or thread is not None and not isinstance(thread, str):
                continue
            prior = latest.get((ident, thread))
            if prior is None or question_rank(entry, index) >= question_rank(prior[1], prior[0]):
                latest[(ident, thread)] = (index, entry)
    return latest


def project(messages: list[dict[str, Any]], summaries: dict[str, str] | None = None) -> Projection:
    """Project an already-replayed semantic context; never mutate the input."""
    if not isinstance(messages, list) or len(messages) > MAX_COUNT:
        raise ValueError("Invalid history message list")
    context = encoded(messages)
    for message in messages:
        if not isinstance(message, dict):
            raise TypeError("History messages must be objects")
        text(message.get("role"), 32, empty=False)
    summaries = summaries or {}
    if not isinstance(summaries, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in summaries.items()):
        raise ValueError("Invalid saved card summaries")
    encoded(summaries, MAX_CONTEXT)
    builder = _Builder(messages, summaries)
    questions = _latest_questions(messages)
    seen: set[tuple[Any, ...]] = set()
    system = 0
    generic_tools = 0
    fresh = True
    for index, message in enumerate(messages):
        role = message["role"]
        fresh = fresh and role == "system" and set(message).issubset(FRESH_SYSTEM_KEYS)
        if role == "system":
            system += 1
            continue
        if role == "tool":
            generic_tools += 1
        saved, trace = _trace(message)
        content = message.get("content")
        claude = message.get("claude_native")
        claude = claude if isinstance(claude, dict) else None
        texts = None
        if claude is not None:
            from litetui.claude_turn import split_card_texts
            texts = split_card_texts(claude, content)
        reasoning = message.get("reasoning_content")
        if role == "assistant" and isinstance(reasoning, str) and reasoning:
            builder.add(index, "reasoning", reasoning, label="Saved reasoning", state="complete")
        traced = any(record.get("kind") == "agentMessage" and record.get("result") for record in trace)
        if role != "tool" and not (role == "assistant" and (traced or texts is not None)):
            builder.content(index, role, content)
        entries = saved.get("async_questions", [])
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            ident, thread = entry.get("id"), entry.get("threadId")
            if not isinstance(ident, str) or not ident or thread is not None and not isinstance(thread, str):
                continue
            latest = questions.get((ident, thread))
            titles = entry.get("questions")
            question_key = (saved.get("app_server_thread_id"), "async-question", ident)
            if (latest is None or latest[0] != index or latest[1] is not entry or question_key in seen
                    or entry.get("version") != 1 or entry.get("state") != "pending"
                    or not isinstance(titles, list)
                    or not all(isinstance(question, dict) and isinstance(question.get("title"), str) for question in titles)):
                continue
            seen.add(question_key)
            builder.add(index, "notice", "Saved pending question; read only, no answer action.\n" +
                        "\n".join(question["title"] for question in titles),
                        label="Saved question", state="incomplete",
                        meta=metadata("codex", thread_id=saved.get("app_server_thread_id"), item_id=ident))
        for record in trace:
            ident = record.get("id")
            if not isinstance(ident, str) or not ident:
                continue
            thread, turn = saved.get("app_server_thread_id"), record.get("turnId")
            key = (thread, turn, ident)
            if key in seen:
                continue
            seen.add(key)
            duration = record.get("durationMs")
            duration = int(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 <= duration <= 2_147_483_647 else None
            meta = metadata("codex", thread_id=thread, turn_id=turn, item_id=ident,
                            ok=record.get("ok") if type(record.get("ok")) is bool else None, duration_ms=duration)
            state = _state(record.get("state"))
            if record.get("kind") == "agentMessage":
                if record.get("result"):
                    builder.answer(index, _string(record["result"]), meta=meta, state=state)
            elif record.get("kind") == "plan":
                builder.add(index, "plan", record.get("result", ""), label=str(record.get("name") or "Codex plan"), state=state, meta=meta)
            else:
                result = ("No completion was saved before this conversation closed." if state == "incomplete" else _string(record.get("result", "")))
                builder.add(index, "tool", f"Arguments:\n{_string(record.get('args', ''))}\n\nResult:\n{result}",
                            label=str(record.get("name") or "Codex tool"), state=state, meta=meta)
        if claude is not None:
            activities = claude.get("activities", [])
            activities = [row for row in activities if isinstance(row, dict)] if isinstance(activities, list) else []
            if texts is None:
                _claude_tools(builder, index, activities)
            else:
                for card, body in enumerate(texts):
                    if body:
                        builder.answer(index, body, meta=metadata("claude"))
                    _claude_tools(builder, index, [row for row in activities if row.get("card") == card])
                _claude_tools(builder, index, [row for row in activities if row.get("card") not in range(len(texts))])
    if generic_tools:
        builder.add(len(messages) - 1, "notice", f"{generic_tools} tool result(s) restored to context but not redrawn", label="Saved context")
    return Projection(digest(context), builder.hash.hexdigest(), len(messages), system, fresh, tuple(builder.blocks))


def display_cost(value: str) -> int:
    """Conservative portable cost; the native renderer independently rechecks."""
    return sum(32 if ch == "\n" else 12 for ch in value)


def pages(projection: Projection) -> Iterator[tuple[tuple[int, int], tuple[int, int], list[dict[str, Any]]]]:
    """Deterministic complete partition, without retaining every page."""
    current: list[dict[str, Any]] = []
    cost = 0
    start = (0, 0)
    for block in projection.blocks:
        raw = block.content.encode("utf-8")
        base = block.fields()
        overhead = 320 + sum(display_cost(value) for value in (block.role, block.label, block.state or ""))
        overhead += sum(display_cost(value) for value in json.loads(block.metadata_bytes).values() if isinstance(value, str))
        if overhead + 32 > PAGE_UNITS:
            raise ValueError("History block labels exceed the display budget")
        offset = 0
        chars = iter(block.content)
        buffered: str | None = next(chars, None)
        while buffered is not None or not raw and offset == 0:
            if current and (len(current) >= PAGE_FRAGMENTS or cost + overhead + (32 if buffered == "\n" else 12) > PAGE_UNITS):
                yield start, (block.index, offset), current
                start, current, cost = (block.index, offset), [], 0
            remaining = PAGE_UNITS - cost - overhead
            piece: list[str] = []
            piece_cost = 0
            while buffered is not None:
                added = 32 if buffered == "\n" else 12
                if piece_cost + added > remaining:
                    break
                piece.append(buffered)
                piece_cost += added
                buffered = next(chars, None)
            shown = "".join(piece)
            data = shown.encode("utf-8")
            end = offset + len(data)
            current.append({**base, "part": {"utf8_start": offset, "utf8_end": end,
                                              "utf8_total": len(raw), "sha256": digest(data)}, "text": shown})
            cost += overhead + piece_cost
            offset = end
            if not raw:
                break
    if current or not projection.blocks:
        yield start, (len(projection.blocks), 0), current
