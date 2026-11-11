"""Messages wire adaptation and validated SSE for the API-key backend.

Only complete, well-formed tool calls leave this boundary. An interrupted stream
is a failure, not a partially executable assistant message. Unknown SSE events
are ignored as required by Anthropic's versioning policy.
"""
from __future__ import annotations

import json

from litetui.model_transport import (
    ProviderError,
    ResponseStream,
    _call,
    _chunk,
    _stream_failure,
    _usage,
    claude_request,
)


def request(kwargs):
    try:
        body = claude_request(kwargs, provider="anthropic")
        # Repair interrupted turns only on the outgoing copy. Missing output is
        # explicitly UNKNOWN, never an assertion that the tool did not execute.
        messages = body["messages"]
        seen = set()
        for index, message in enumerate(messages):
            blocks = message["content"]
            if message["role"] == "user":
                results = [b for b in blocks if b["type"] == "tool_result"]
                if len({b["tool_use_id"] for b in results}) != len(results):
                    raise ValueError("duplicate results")
                previous = messages[index - 1]["content"] if index else []
                expected = {b["id"] for b in previous if b["type"] == "tool_use"}
                if any(b["tool_use_id"] not in expected for b in results):
                    raise ValueError("orphan results")
                # The API requires tool_result before any ordinary user text.
                message["content"] = results + [b for b in blocks if b["type"] != "tool_result"]
                continue
            calls = [b for b in blocks if b["type"] == "tool_use"]
            if not calls:
                continue
            for call in calls:
                if (not isinstance(call["input"], dict) or not isinstance(call["id"], str)
                        or not call["id"] or call["id"] in seen):
                    raise ValueError("invalid tool call")
                seen.add(call["id"])
            if index + 1 == len(messages) or messages[index + 1]["role"] != "user":
                messages.insert(index + 1, {"role": "user", "content": []})
            following = messages[index + 1]["content"]
            present = {b["tool_use_id"] for b in following if b["type"] == "tool_result"}
            following[:0] = [{"type": "tool_result", "tool_use_id": call["id"], "is_error": True,
                              "content": "[litetui transport] no tool output was recorded for this call; "
                                         "outcome UNKNOWN. Verify any side effects before retrying."}
                             for call in calls if call["id"] not in present]
        if isinstance(body.get("stop_sequences"), str):
            body["stop_sequences"] = [body["stop_sequences"]]
        if not body.get("tools"):
            body.pop("tool_choice", None)
        return body
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ProviderError("Anthropic cannot replay malformed message/tool history. Start a new conversation or repair the saved history; no tools were replayed.") from None


def _object(value):
    if not isinstance(value, dict):
        raise TypeError("expected object")
    return value


def _string(value):
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


class MessageStream(ResponseStream):
    async def _iter_response(self):
        blocks, arguments, usage = {}, {}, {}
        open_blocks, tool_ids = set(), set()
        names = {tool["function"]["name"] for tool in self.tools}
        started, reason = False, None
        async for line in self.response.aiter_lines():
            if not line.startswith("data:"):
                continue
            try:
                event = _object(json.loads(line[5:]))
                kind = event.get("type")
                if kind == "error":
                    raise _stream_failure("anthropic", event, self._secrets)
                if kind == "message_start":
                    if started:
                        raise ValueError("duplicate start")
                    started = True
                    usage.update(_object(_object(event["message"]).get("usage", {})))
                elif kind == "content_block_start":
                    index, block = event["index"], _object(event["content_block"])
                    if not started or type(index) is not int or index < 0 or index in blocks:
                        raise ValueError("invalid block start")
                    blocks[index] = dict(block)
                    open_blocks.add(index)
                    if block["type"] == "tool_use":
                        call_id, name = _string(block["id"]), _string(block["name"])
                        if not call_id or call_id in tool_ids or name not in names:
                            raise ValueError("invalid tool identity")
                        tool_ids.add(call_id)
                        initial = _object(block["input"])
                        arguments[index] = json.dumps(initial) if initial else ""
                    elif block["type"] == "text":
                        if text := _string(block["text"]):
                            yield _chunk(text=text)
                    elif block["type"] == "thinking":
                        _string(block["signature"])
                        if thinking := _string(block["thinking"]):
                            yield _chunk(reasoning=thinking)
                elif kind == "content_block_delta":
                    index, delta = event["index"], _object(event["delta"])
                    if index not in open_blocks:
                        raise ValueError("delta without open block")
                    block = blocks[index]
                    expected = {"text_delta": "text", "input_json_delta": "tool_use",
                                "thinking_delta": "thinking", "signature_delta": "thinking"}
                    if block["type"] == "tool_use" and delta["type"] != "input_json_delta":
                        raise ValueError("unsupported tool input delta")
                    if delta["type"] in expected and block["type"] != expected[delta["type"]]:
                        raise ValueError("delta type mismatch")
                    if delta["type"] == "text_delta":
                        yield _chunk(text=_string(delta["text"]))
                    elif delta["type"] == "input_json_delta":
                        arguments[index] += _string(delta["partial_json"])
                    elif delta["type"] == "thinking_delta":
                        text = _string(delta["thinking"])
                        block["thinking"] += text
                        yield _chunk(reasoning=text)
                    elif delta["type"] == "signature_delta":
                        block["signature"] += _string(delta["signature"])
                elif kind == "content_block_stop":
                    index = event["index"]
                    if index not in open_blocks:
                        raise ValueError("stop without open block")
                    open_blocks.remove(index)
                    if blocks[index]["type"] == "tool_use":
                        arguments[index] = json.dumps(_object(json.loads(arguments[index] or "{}")))
                elif kind == "message_delta":
                    usage.update(_object(event.get("usage", {})))
                    reason = _object(event["delta"]).get("stop_reason") or reason
                elif kind == "message_stop":
                    if not started or open_blocks or reason not in (
                        "end_turn", "tool_use", "stop_sequence", "refusal", "max_tokens", "model_context_window_exceeded"
                    ):
                        raise ValueError("incomplete message")
                    if reason in ("max_tokens", "model_context_window_exceeded"):
                        # The host discards failed rounds before appending history.
                        # Never execute even a valid prefix of a truncated tool batch.
                        raise ProviderError("Anthropic reached its response or context limit. Adjust the output budget or compact the conversation. No tools from this response were executed.")
                    if bool(tool_ids) != (reason == "tool_use"):
                        raise ValueError("inconsistent tool stop")
                    final = _chunk(usage=_usage(usage, "anthropic"), metadata={
                        "provider": "anthropic", "model": self.model,
                        "items": [b for b in blocks.values() if b["type"] in ("thinking", "redacted_thinking")],
                    })
                    self._completed = True
                    for index, block in blocks.items():
                        if block["type"] == "tool_use":
                            yield _chunk(call=_call(index, block["id"], block["name"], arguments[index]))
                    yield final
                    return
            except (ValueError, KeyError, TypeError):
                raise ProviderError("Anthropic returned an unreadable or incomplete stream. No tools from this response were executed.") from None
        raise ProviderError("Anthropic stream ended before completion. No tools from this response were executed.")

    def __init__(self, response, client, model, tools, *, secrets=()):
        super().__init__(response, client, "anthropic", model, tools, secrets=secrets, hold_calls=True)
        self.tools = tools
