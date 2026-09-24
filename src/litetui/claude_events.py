"""Normalize claude-agent-sdk frames into LiteTUI display events.

Claude owns its agent loop. This module turns what the SDK reports into
display facts and nothing else: a native `tool_use` here is an activity card,
never a host tool request the dispatcher may execute. That one-way direction
is the whole point of the module's existence (plan 3.4, "native tool
starts/results are display activity").

Three properties the callers depend on:

1. **No SDK import.** The module reads duck-typed objects *or* the raw wire
   dicts the CLI emits, so the reader can hand over either and the tests run
   against JSON fixtures with or without the optional extra installed. The
   shape is normalized ONCE (`_as_frame`) and projected once; there is no
   parallel object/dict handler pair to drift apart.
2. **`push` never raises.** A reader that dies on a malformed frame takes the
   session with it. Anything unrecognized becomes a bounded, redacted
   `diagnostic` event instead — never a crash, never an invented success.
3. **Nothing is inferred.** A counter the frame omits stays `None`. No turn
   delta is derived from cumulative totals, because whether
   `ResultMessage.usage` is per-turn or session-cumulative is not settled for
   the pinned pair (OpenBolt, 2026-09-23: "do NOT infer result per-turn
   semantics yet"). `total_cost_usd` and `model_usage` ARE documented
   cumulative — `ConversationResetMessage`'s own docstring says a reset
   "zeroes the running totals reported on subsequent ResultMessage objects
   (e.g. total_cost_usd)" — so they are carried separately and labelled.

Contract pinned against claude-agent-sdk **0.2.159** (`pyproject.toml:57`,
landed as 24ba146): `types.py` for the dataclasses, `_internal/message_parser.py`
for the wire key names. 0.1.19 — still what a bare system interpreter has — is
NOT this contract: its `AssistantMessage` carries no `message_id` and no
`usage`, which would silently reduce reconciliation to guesswork. Test against
the worktree `.venv`, never the system Python.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from litetui import sanitize

#: Diagnostics describe a frame; they never reproduce it. The limit matches
#: the existing native-error precedent (codex_app_server.py:958, `[:500]`).
DIAGNOSTIC_LIMIT = 500

#: Stream sub-events that carry no display fact. Everything else unknown gets
#: a diagnostic — these would make that signal pure noise.
_BENIGN_STREAM_EVENTS = frozenset({"ping", "content_block_stop"})  # message_stop ends a message (_stream)

#: The API reports usage in snake_case; the CLI's `modelUsage` passthrough uses
#: camelCase (types.py:1314 says so outright). Both spellings reach this layer,
#: so both are read rather than one being assumed.
_USAGE_ALIASES = {
    "input_tokens": ("input_tokens", "inputTokens"),
    "output_tokens": ("output_tokens", "outputTokens"),
    "cache_read_tokens": ("cache_read_input_tokens", "cacheReadInputTokens"),
    "cache_creation_tokens": (
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
    ),
}


def _clean(text: str) -> str:
    """Escape-strip and secret-redact, in the order the rest of the app uses.

    Native tool output is a NEW path into the TUI: it does not pass through
    `app._execute_tool`, so the dispatch-loop sanitizing at app.py:7573 cannot
    cover it. Applying it here — the one place every native result crosses —
    is the same fix the codex native path already made (codex_tool_ui.py:19,
    codex_native_policy.py:137), not a second policy.
    """
    return sanitize.redact_secrets(sanitize.strip_escapes(text))


def _bounded(value: Any) -> str:
    """A redacted, truncated description of something untrusted."""
    text = value if isinstance(value, str) else repr(value)
    text = _clean(text)
    if len(text) > DIAGNOSTIC_LIMIT:
        text = text[:DIAGNOSTIC_LIMIT] + "…"
    return text


def _count(source: Any, names: tuple[str, ...]) -> int | None:
    """First non-negative int under any spelling of *names*, else unknown."""
    if not isinstance(source, dict):
        return None
    for name in names:
        value = source.get(name)
        if type(value) is int and value >= 0:
            return value
    return None


@dataclass(frozen=True)
class ClaudeUsage:
    """One usage observation, labelled by where it came from.

    `source="message"` is a single API request — that, and only that, is
    current context occupancy. `source="result"` is the turn terminal, whose
    `cost_usd` and `model_usage` are session running totals. Summing across
    requests would over-count; the two are kept apart on purpose (plan 3.4).
    """

    source: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    #: Occupied context after this request: input + cache reads + cache writes.
    #: `None` unless the frame supplied enough to say — unknown stays unknown.
    context_tokens: int | None = None
    max_context_tokens: int | None = None
    #: Session running total, not this turn's spend.
    cost_usd: float | None = None
    model_usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClaudeEvent:
    """One display fact. `kind` is the discriminator; see `KINDS`."""

    kind: str
    session_id: str | None = None
    #: Native assistant message id (`msg_…`) where the frame carries one,
    #: otherwise the SDK's per-message uuid.
    message_id: str | None = None
    uuid: str | None = None
    #: Set on work a nested agent or background task produced, so attribution
    #: survives (plan 3.4).
    parent_tool_use_id: str | None = None
    text: str = ""
    #: How the consumer must apply `text` on a snapshot event: `"append"`
    #: (text is the part the deltas did not deliver, often empty) or
    #: `"replace"` (render `text` as the whole thing). See `_reconcile`.
    reconcile: str = "replace"
    tool_id: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result: Any = None
    is_error: bool | None = None
    #: `tool_use`: the input is final rather than still streaming.
    complete: bool = False
    model: str | None = None
    usage: ClaudeUsage | None = None
    data: dict[str, Any] = field(default_factory=dict)
    #: `diagnostic` and `error` only: bounded, redacted, safe to render.
    detail: str = ""


#: Every kind this module can emit, and what produces it.
KINDS = {
    "session": "system/init — native session is ready",
    "text_delta": "streamed assistant text",
    "thinking_delta": "streamed reasoning text",
    "thinking": "reasoning snapshot, reconciled against the deltas",
    "message": "assistant snapshot, reconciled against the deltas",
    "user_text": "text content of a user-role frame (injected or replayed)",
    "tool_use": "native tool activity — DISPLAY ONLY, never dispatchable",
    "tool_result": "native tool result, joined by tool_use id",
    "usage": "a usage observation; see ClaudeUsage.source",
    "compaction": "system/compact_boundary — Claude compacted its own context",
    "task": "background task lifecycle (started/progress/notification/updated)",
    "notice": "a known system frame with no dedicated kind",
    "rate_limit": "rate limit status transition",
    "reset": "conversation_reset — running totals restart after this",
    "result": "turn terminal state",
    "error": "reader failure, or an assistant frame reporting an error",
    "diagnostic": "unknown or malformed frame, bounded and redacted",
}


def _reconcile(streamed: str, full: str) -> tuple[str, str]:
    """Decide what of *full* the consumer still has to render.

    A boolean "it was streamed, suppress it" is wrong and this is the reason:
    if any delta was dropped the consumer is left holding a truncated message
    forever, and the snapshot — the one frame that could repair it — is exactly
    what the boolean throws away. So compare, and hand back the missing tail.

    Divergence (the snapshot does not continue what was streamed) cannot be
    spliced safely — a delta was lost from the middle, not the end — so it
    falls back to a full replace rather than corrupting the text with a
    guessed join.
    """
    if not streamed:
        return "replace", full
    if full.startswith(streamed):
        return "append", full[len(streamed) :]
    return "replace", full


def _as_block(block: Any) -> dict[str, Any]:
    """Wire-shape one content block, from a dict or an SDK dataclass."""
    if isinstance(block, dict):
        return block
    name = type(block).__name__
    if hasattr(block, "thinking"):
        return {
            "type": "thinking",
            "thinking": block.thinking,
            "signature": getattr(block, "signature", ""),
        }
    if hasattr(block, "tool_use_id"):
        # ServerToolResultBlock has no is_error field; ToolResultBlock does.
        # That absence is the only structural difference between them.
        if hasattr(block, "is_error"):
            return {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
                "is_error": block.is_error,
            }
        return {
            "type": "advisor_tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
        }
    if hasattr(block, "input"):
        # ToolUseBlock and ServerToolUseBlock are field-identical (id, name,
        # input) — types.py:971 vs :1001. Only the class name separates them,
        # so only the class name can.
        kind = "server_tool_use" if name.startswith("Server") else "tool_use"
        return {"type": kind, "id": block.id, "name": block.name, "input": block.input}
    if hasattr(block, "text"):
        return {"type": "text", "text": block.text}
    raise TypeError(f"unrecognized content block {name}")


def _as_frame(message: Any) -> dict[str, Any]:
    """Wire-shape any SDK message so there is exactly one projection path.

    Dispatch is on attributes, not class names: the SDK's own subclasses
    (TaskStartedMessage and friends) are SystemMessages, and a name check
    would have to enumerate them and then miss the next one added.
    """
    if isinstance(message, dict):
        return message

    def get(name: str, default: Any = None) -> Any:
        return getattr(message, name, default)

    if hasattr(message, "event"):  # StreamEvent
        return {
            "type": "stream_event",
            "uuid": get("uuid"),
            "session_id": get("session_id"),
            "event": get("event") or {},
            "parent_tool_use_id": get("parent_tool_use_id"),
        }
    if hasattr(message, "rate_limit_info"):
        info = get("rate_limit_info")
        return {
            "type": "rate_limit_event",
            "uuid": get("uuid"),
            "session_id": get("session_id"),
            "rate_limit_info": getattr(info, "raw", None) or {},
        }
    if hasattr(message, "new_conversation_id"):
        return {
            "type": "conversation_reset",
            "new_conversation_id": get("new_conversation_id"),
            "uuid": get("uuid"),
            "session_id": get("session_id"),
        }
    if hasattr(message, "duration_ms"):  # ResultMessage
        frame = {
            "type": "result",
            "modelUsage": get("model_usage") or {},
            "deferred_tool_use": get("deferred_tool_use"),
        }
        for name in (
            "subtype",
            "duration_ms",
            "duration_api_ms",
            "is_error",
            "num_turns",
            "session_id",
            "stop_reason",
            "total_cost_usd",
            "usage",
            "result",
            "permission_denials",
            "errors",
            "api_error_status",
            "uuid",
            "terminal_reason",
            "origin",
        ):
            frame[name] = get(name)
        return frame
    if hasattr(message, "subtype"):  # SystemMessage and its subclasses
        # Every subclass keeps the raw payload in `data` (types.py:1195 and
        # each sibling say so), so `data` already IS the wire frame. The
        # overlay only covers an SDK-synthesized message with a thin `data`.
        frame = dict(get("data") or {})
        frame["type"] = "system"
        frame.setdefault("subtype", get("subtype"))
        for name in (
            "task_id",
            "description",
            "status",
            "summary",
            "output_file",
            "patch",
            "tool_use_id",
            "task_type",
            "usage",
            "hook_event_name",
            "error",
            "session_id",
            "uuid",
        ):
            value = get(name)
            if value is not None:
                frame.setdefault(name, value)
        return frame
    if hasattr(message, "content"):
        content = get("content")
        blocks = (
            [_as_block(block) for block in content]
            if isinstance(content, list)
            else content
        )
        if get("model") is not None:  # AssistantMessage
            return {
                "type": "assistant",
                "uuid": get("uuid"),
                "session_id": get("session_id"),
                "parent_tool_use_id": get("parent_tool_use_id"),
                "error": get("error"),
                "message": {
                    "id": get("message_id"),
                    "content": blocks,
                    "model": get("model"),
                    "usage": get("usage"),
                    "stop_reason": get("stop_reason"),
                },
            }
        return {
            "type": "user",
            "uuid": get("uuid"),
            "parent_tool_use_id": get("parent_tool_use_id"),
            "origin": get("origin"),
            "tool_use_result": get("tool_use_result"),
            "message": {"content": blocks},
        }
    raise TypeError(f"unrecognized SDK message {type(message).__name__}")


class ClaudeEventStream:
    """Stateful normalizer — one per live session, driven by its one reader.

    It holds only what reconciliation needs: the text already streamed for the
    in-flight message, and the tool names needed to label results. Both are
    dropped at every turn terminal, so a long session does not accumulate.
    """

    def __init__(self, *, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.usage: ClaudeUsage | None = None
        self._streamed: dict[str, dict[str, str]] = {}
        self._active: str | None = None
        self._tools: dict[str, str] = {}

    # -- public --------------------------------------------------------

    def push(self, message: Any) -> list[ClaudeEvent]:
        """Project one SDK message. Returns zero or more events; never raises."""
        try:
            frame = _as_frame(message)
            if not isinstance(frame, dict):
                raise TypeError(f"frame is {type(frame).__name__}, not a mapping")
            session = frame.get("session_id")
            if isinstance(session, str) and session:
                self.session_id = session
            return self._project(frame)
        except Exception as exc:  # noqa: BLE001 - a reader must survive any frame
            return [self._diagnostic(message, exc)]

    def failure(self, exc: BaseException) -> ClaudeEvent:
        """Turn a reader-level exception into a normalized, renderable error."""
        return ClaudeEvent(
            kind="error",
            session_id=self.session_id,
            is_error=True,
            detail=_bounded(f"{type(exc).__name__}: {exc}"),
            data={"scope": "reader"},
        )

    def reset_turn(self) -> None:
        """Drop per-turn reconciliation state — after an interrupt, say."""
        self._streamed.clear()
        self._active = None
        self._tools.clear()

    # -- projection ----------------------------------------------------

    def _project(self, frame: dict[str, Any]) -> list[ClaudeEvent]:
        kind = frame.get("type")
        handler = {
            "assistant": self._assistant,
            "user": self._user,
            "system": self._system,
            "result": self._result,
            "stream_event": self._stream,
            "rate_limit_event": self._rate_limit,
            "conversation_reset": self._reset,
        }.get(kind if isinstance(kind, str) else "")
        if handler is None:
            return [self._diagnostic(frame, ValueError(f"unknown frame type {kind!r}"))]
        return handler(frame)

    def _diagnostic(self, source: Any, exc: BaseException) -> ClaudeEvent:
        """Describe an unusable frame. Bounded and redacted — never the frame.

        Keys are listed without their values: a key name tells a reader which
        frame shape arrived, while the values are the part that could carry a
        token or a secret.
        """
        keys = sorted(source)[:20] if isinstance(source, dict) else []
        return ClaudeEvent(
            kind="diagnostic",
            session_id=self.session_id,
            detail=_bounded(f"{type(exc).__name__}: {exc}"),
            data={
                "frame_type": (
                    source.get("type") if isinstance(source, dict) else None
                ),
                "python_type": type(source).__name__,
                "keys": keys,
            },
        )

    def _assistant(self, frame: dict[str, Any]) -> list[ClaudeEvent]:
        message = frame.get("message") or {}
        message_id = message.get("id") or frame.get("uuid")
        blocks = message.get("content")
        blocks = blocks if isinstance(blocks, list) else []
        parent = frame.get("parent_tool_use_id")
        model = message.get("model")
        common: dict[str, Any] = {
            "session_id": self.session_id,
            "message_id": message_id,
            "uuid": frame.get("uuid"),
            "parent_tool_use_id": parent,
        }

        # One unusable block must not cost the whole message. The joins below
        # read only the mappings; `_blocks` still receives the full list and
        # diagnoses what it cannot use, so the good siblings survive.
        usable = [block for block in blocks if isinstance(block, dict)]

        # 🔴 A SNAPSHOT IS NOT THE END OF ITS MESSAGE. With thinking on, the CLI sends
        # one snapshot per content block, under the message's id, and the thinking
        # block's arrives BEFORE the text streams (live, CLI 2.1.281, 2026-09-24).
        # Popping the stream state and clearing `_active` here sent the text deltas
        # out with message_id=None and made the final text snapshot a full replace
        # under the real id: the turn held the answer twice. The stream's own
        # `message_stop` ends the message; a snapshot only consumes what it carries.
        slot = self._streamed.get(message_id) if message_id else None
        streamed = dict(slot) if slot else {}

        events: list[ClaudeEvent] = []
        usage = self._usage(message.get("usage"), source="message")
        if usage is not None:
            self.usage = usage
            events.append(ClaudeEvent(kind="usage", usage=usage, model=model, **common))

        def consume(kind: str, full: str) -> tuple[str, str]:
            # Reconcile against what streamed, then drop what this snapshot covered,
            # so a later snapshot of the same message reconciles against the rest.
            mode, payload = _reconcile(streamed.get(kind, ""), full)
            if slot is not None:
                done = slot.get(kind, "")
                slot[kind] = done[len(full):] if done.startswith(full) else ""
            return mode, payload

        has = {b.get("type") for b in usable}
        thinking = "".join(
            str(b.get("thinking") or "") for b in usable if b.get("type") == "thinking"
        )
        if "thinking" in has:
            mode, payload = consume("thinking", thinking)
            events.append(
                ClaudeEvent(
                    kind="thinking",
                    text=payload,
                    reconcile=mode,
                    complete=True,
                    model=model,
                    **common,
                )
            )

        text = "".join(
            str(b.get("text") or "") for b in usable if b.get("type") == "text"
        )
        # A snapshot with no text block (a thinking or tool block's own) says nothing
        # about the text: a no-op, never a replace with "" that wipes what streamed.
        mode, payload = consume("text", text) if "text" in has else ("append", "")
        events.append(
            ClaudeEvent(
                kind="message",
                text=payload,
                reconcile=mode,
                complete=True,
                model=model,
                data={
                    # Verbatim and ordered, so a consumer rendering the
                    # no-deltas fallback can restore text/tool interleaving
                    # that the concatenation above flattens.
                    "blocks": blocks,
                    "full_text": text,
                    "stop_reason": message.get("stop_reason"),
                },
                **common,
            )
        )
        events.extend(self._blocks(blocks, common, model))

        error = frame.get("error")
        if error:
            events.append(
                ClaudeEvent(
                    kind="error",
                    is_error=True,
                    model=model,
                    detail=_bounded(error),
                    data={"scope": "assistant"},
                    **common,
                )
            )
        return events

    def _user(self, frame: dict[str, Any]) -> list[ClaudeEvent]:
        message = frame.get("message") or {}
        content = message.get("content")
        common: dict[str, Any] = {
            "session_id": self.session_id,
            "message_id": frame.get("uuid"),
            "uuid": frame.get("uuid"),
            "parent_tool_use_id": frame.get("parent_tool_use_id"),
        }
        data = {"origin": frame.get("origin")}
        if isinstance(content, str):
            return [
                ClaudeEvent(kind="user_text", text=content, data=data, **common)
            ]
        blocks = content if isinstance(content, list) else []
        events = [
            ClaudeEvent(
                kind="user_text", text=str(b.get("text") or ""), data=data, **common
            )
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        events.extend(
            self._blocks(blocks, common, None, extra=frame.get("tool_use_result"))
        )
        return events

    def _blocks(
        self,
        blocks: list[Any],
        common: dict[str, Any],
        model: str | None,
        extra: Any = None,
    ) -> list[ClaudeEvent]:
        """Project the tool blocks of a message, in wire order."""
        events: list[ClaudeEvent] = []
        for block in blocks:
            if not isinstance(block, dict):
                events.append(
                    self._diagnostic(block, TypeError("content block is not a mapping"))
                )
                continue
            kind = block.get("type")
            if kind in ("tool_use", "server_tool_use"):
                tool_id, name = block.get("id"), block.get("name")
                if isinstance(tool_id, str) and isinstance(name, str):
                    self._tools[tool_id] = name
                events.append(
                    ClaudeEvent(
                        kind="tool_use",
                        tool_id=tool_id,
                        tool_name=name,
                        tool_input=block.get("input"),
                        complete=True,
                        model=model,
                        data={"server": kind == "server_tool_use"},
                        **common,
                    )
                )
            elif kind in ("tool_result", "advisor_tool_result"):
                tool_id = block.get("tool_use_id")
                events.append(
                    ClaudeEvent(
                        kind="tool_result",
                        tool_id=tool_id,
                        tool_name=self._tools.get(tool_id) if tool_id else None,
                        tool_result=self._result_content(block.get("content")),
                        is_error=block.get("is_error"),
                        complete=True,
                        model=model,
                        data={
                            "server": kind == "advisor_tool_result",
                            "tool_use_result": extra,
                        },
                        **common,
                    )
                )
        return events

    @staticmethod
    def _result_content(content: Any) -> Any:
        """Sanitize rendered tool text; leave every other shape verbatim."""
        if isinstance(content, str):
            return _clean(content)
        if isinstance(content, list):
            return [
                {**part, "text": _clean(part["text"])}
                if isinstance(part, dict) and isinstance(part.get("text"), str)
                else part
                for part in content
            ]
        return content

    def _system(self, frame: dict[str, Any]) -> list[ClaudeEvent]:
        subtype = frame.get("subtype")
        common: dict[str, Any] = {
            "session_id": self.session_id,
            "uuid": frame.get("uuid"),
            "parent_tool_use_id": frame.get("parent_tool_use_id"),
        }
        if subtype == "init":
            return [
                ClaudeEvent(
                    kind="session",
                    model=frame.get("model"),
                    data=frame,
                    **common,
                )
            ]
        if subtype == "compact_boundary":
            # Observed, never acted on: Claude compacted its own context and
            # the host display transcript stays exactly as it is (plan 3.4).
            return [ClaudeEvent(kind="compaction", data=frame, **common)]
        if isinstance(subtype, str) and subtype.startswith("task_"):
            return [
                ClaudeEvent(
                    kind="task",
                    tool_id=frame.get("tool_use_id"),
                    data={
                        "phase": subtype,
                        "task_id": frame.get("task_id"),
                        "status": frame.get("status")
                        or (frame.get("patch") or {}).get("status"),
                        "description": frame.get("description"),
                        "summary": frame.get("summary"),
                        "output_file": frame.get("output_file"),
                        "usage": frame.get("usage"),
                        "patch": frame.get("patch"),
                    },
                    **common,
                )
            ]
        return [
            ClaudeEvent(
                kind="notice",
                detail=_bounded(subtype),
                data={"subtype": subtype, "keys": sorted(frame)[:20]},
                **common,
            )
        ]

    def _result(self, frame: dict[str, Any]) -> list[ClaudeEvent]:
        model_usage = frame.get("modelUsage")
        model_usage = model_usage if isinstance(model_usage, dict) else {}
        usage = self._usage(
            frame.get("usage"),
            source="result",
            cost_usd=frame.get("total_cost_usd"),
            model_usage=model_usage,
        )
        events: list[ClaudeEvent] = []
        if usage is not None:
            self.usage = usage
            events.append(
                ClaudeEvent(kind="usage", usage=usage, session_id=self.session_id)
            )

        # 🔴 `subtype` IS NOT THE VERDICT. The CLI emits subtype "success"
        # alongside is_error=True — types.py:1364 documents api_error_status as
        # the HTTP status "when is_error is True and subtype is 'success'".
        # Reading the subtype alone reports a failed turn as a good one.
        is_error = bool(frame.get("is_error"))
        events.append(
            ClaudeEvent(
                kind="result",
                session_id=self.session_id,
                uuid=frame.get("uuid"),
                is_error=is_error,
                text=frame.get("result") or "",
                detail=_bounded(frame.get("errors") or "") if frame.get("errors") else "",
                data={
                    "subtype": frame.get("subtype"),
                    "terminal_reason": frame.get("terminal_reason"),
                    "stop_reason": frame.get("stop_reason"),
                    "api_error_status": frame.get("api_error_status"),
                    "duration_ms": frame.get("duration_ms"),
                    "duration_api_ms": frame.get("duration_api_ms"),
                    "num_turns": frame.get("num_turns"),
                    "permission_denials": frame.get("permission_denials") or [],
                    "deferred_tool_use": frame.get("deferred_tool_use"),
                    "origin": frame.get("origin"),
                },
            )
        )
        self.reset_turn()
        return events

    def _rate_limit(self, frame: dict[str, Any]) -> list[ClaudeEvent]:
        info = frame.get("rate_limit_info")
        info = info if isinstance(info, dict) else {}
        return [
            ClaudeEvent(
                kind="rate_limit",
                session_id=self.session_id,
                uuid=frame.get("uuid"),
                data=info,
            )
        ]

    def _reset(self, frame: dict[str, Any]) -> list[ClaudeEvent]:
        # Cumulative totals restart here, so the last observation must not be
        # carried across the boundary as if it still described this session.
        self.reset_turn()
        self.usage = None
        return [
            ClaudeEvent(
                kind="reset",
                session_id=self.session_id,
                uuid=frame.get("uuid"),
                data={"new_conversation_id": frame.get("new_conversation_id")},
            )
        ]

    def _stream(self, frame: dict[str, Any]) -> list[ClaudeEvent]:
        event = frame.get("event")
        event = event if isinstance(event, dict) else {}
        kind = event.get("type")
        common: dict[str, Any] = {
            "session_id": self.session_id,
            "uuid": frame.get("uuid"),
            "parent_tool_use_id": frame.get("parent_tool_use_id"),
        }

        if kind == "message_start":
            message = event.get("message") or {}
            message_id = message.get("id") or frame.get("uuid")
            self._active = message_id
            self._streamed.setdefault(message_id or "", {"text": "", "thinking": ""})
            usage = self._usage(message.get("usage"), source="message")
            if usage is None:
                return []
            self.usage = usage
            return [
                ClaudeEvent(
                    kind="usage",
                    message_id=message_id,
                    model=message.get("model"),
                    usage=usage,
                    **common,
                )
            ]

        if kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") in ("tool_use", "server_tool_use"):
                tool_id, name = block.get("id"), block.get("name")
                if isinstance(tool_id, str) and isinstance(name, str):
                    self._tools[tool_id] = name
                # complete=False: the card can open now, but the input is still
                # arriving as input_json_delta. Input completion is not
                # execution completion either way (plan 3.4).
                return [
                    ClaudeEvent(
                        kind="tool_use",
                        message_id=self._active,
                        tool_id=tool_id,
                        tool_name=name,
                        tool_input=block.get("input") or {},
                        complete=False,
                        data={"server": block.get("type") == "server_tool_use"},
                        **common,
                    )
                ]
            return []

        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            slot = self._streamed.setdefault(
                self._active or "", {"text": "", "thinking": ""}
            )
            if delta.get("type") == "text_delta":
                text = str(delta.get("text") or "")
                slot["text"] += text
                return [
                    ClaudeEvent(
                        kind="text_delta",
                        message_id=self._active,
                        text=text,
                        **common,
                    )
                ]
            if delta.get("type") == "thinking_delta":
                text = str(delta.get("thinking") or "")
                slot["thinking"] += text
                return [
                    ClaudeEvent(
                        kind="thinking_delta",
                        message_id=self._active,
                        text=text,
                        **common,
                    )
                ]
            # input_json_delta is a partial tool input; the snapshot carries
            # the parsed whole, so there is nothing here worth half-parsing.
            return []

        if kind == "message_stop":
            # The end of the message, and so of its reconciliation (see _assistant).
            self._streamed.pop(self._active or "", None)
            self._active = None
            return []
        if kind in _BENIGN_STREAM_EVENTS or kind == "message_delta":
            return []
        return [
            self._diagnostic(event, ValueError(f"unknown stream event {kind!r}"))
        ]

    def _usage(
        self,
        raw: Any,
        *,
        source: str,
        cost_usd: Any = None,
        model_usage: dict[str, Any] | None = None,
    ) -> ClaudeUsage | None:
        model_usage = model_usage or {}
        if not isinstance(raw, dict) and not model_usage and cost_usd is None:
            return None
        counts = {
            field_name: _count(raw, names)
            for field_name, names in _USAGE_ALIASES.items()
        }
        occupancy = [
            counts["input_tokens"],
            counts["cache_read_tokens"],
            counts["cache_creation_tokens"],
        ]
        windows = [
            entry["contextWindow"]
            for entry in model_usage.values()
            if isinstance(entry, dict)
            and type(entry.get("contextWindow")) is int
            and entry["contextWindow"] > 0
        ]
        return ClaudeUsage(
            source=source,
            **counts,
            # Occupancy is a property of ONE request. A result frame aggregates
            # a turn's requests, so computing it there would report a number
            # larger than any context that ever existed.
            context_tokens=(
                sum(value for value in occupancy if value is not None)
                if source == "message" and any(v is not None for v in occupancy)
                else None
            ),
            max_context_tokens=max(windows) if windows else None,
            cost_usd=cost_usd if isinstance(cost_usd, (int, float)) else None,
            model_usage=model_usage,
            raw=raw if isinstance(raw, dict) else {},
        )
