"""Pilot: a multi-round Claude turn reads top to bottom in stream order.

Ryan, 2026-09-25: the answer text rendered ABOVE the tool calls it came after.
Real LiteTUI app, real ChatLog, real AssistantMessage/ToolMessage widgets; only
the Claude SDK session is a fake that replays recorded frame shapes.
"""

from types import SimpleNamespace

import pytest
from test_claude_turn import FakeBackend, FakeSession

from litetui import app as app_mod
from litetui.claude_events import ClaudeEventStream
from litetui.claude_turn import prepare_input, stream_turn
from litetui.widgets import AssistantMessage, ToolMessage


def _stream(event):
    return {"type": "stream_event", "uuid": "u", "session_id": "sess-1", "event": event}


def _assistant(message_id, blocks):
    return {"type": "assistant", "uuid": "u-" + message_id, "session_id": "sess-1",
            "message": {"id": message_id, "model": "claude-opus-5-5", "content": blocks}}


def _tool_result(tool_id, text):
    return {"type": "user", "uuid": "u-r" + tool_id, "session_id": "sess-1",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}


def _round(message_id, text, tool_id):
    return [
        _stream({"type": "message_start", "message": {"id": message_id, "model": "m"}}),
        _stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}),
        _assistant(message_id, [{"type": "text", "text": text},
                                {"type": "tool_use", "id": tool_id, "name": "Read",
                                 "input": {"file_path": tool_id}}]),
        _stream({"type": "message_stop"}),
        _tool_result(tool_id, "contents of " + tool_id),
    ]


FRAMES = [
    *_round("msg_01", "Round one.", "t1"),
    *_round("msg_02", "Round two.", "t2"),
    _stream({"type": "message_start", "message": {"id": "msg_03", "model": "m"}}),
    _stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Final answer."}}),
    _assistant("msg_03", [{"type": "text", "text": "Final answer."}]),
    _stream({"type": "message_stop"}),
    {"type": "result", "uuid": "u-res", "session_id": "sess-1", "is_error": False,
     "subtype": "success", "result": "Final answer."},
]


@pytest.mark.asyncio
async def test_two_tool_rounds_render_in_stream_order(tmp_path) -> None:
    app = app_mod.LiteTUI()
    app.available_models = ["claude-opus-5-5"]
    app.model_id = "claude-opus-5-5"
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.jobs[:] = []
    async with app.run_test(size=(120, 40)) as pilot:
        app.convo_dir = tmp_path
        app.convo_id = "convo"
        app._materialise_convo = lambda: None
        item = prepare_input(app, "hello", "strict", "typed")
        app._claude_active_input = {"content": "hello", **item}
        backend = FakeBackend(FakeSession(FRAMES), item["_claude_segment"])
        backend._claude_tools = SimpleNamespace(
            cancelled=SimpleNamespace(clear=lambda: None, set=lambda: None, is_set=lambda: False),
            stop=lambda: None, sdk_options=dict)
        backend._claude_events = ClaudeEventStream(session_id="sess-1")
        app.backend = backend
        log = app.query_one("#chat-log")
        before = set(log.children)
        await stream_turn(app)
        for _ in range(6):
            await pilot.pause()

        shown = []
        for w in log.children:
            if w in before:
                continue
            if isinstance(w, AssistantMessage):
                shown.append("text:" + w.answer_text)
            elif isinstance(w, ToolMessage):
                shown.append("tool")
        assert shown == [
            "text:Round one.", "tool",
            "text:Round two.", "tool",
            "text:Final answer.",
        ], (shown, app.notices if hasattr(app, "notices") else None)
