"""Pilot: a think:high Claude turn with tool calls before the answer (T1031).

THE USER (card T1031) asked BY NAME for Opus 5.5 on HIGH thinking, so turning
thinking off is not a fix. The crash: "Unable to find relative location of
AnswerBody(id='answer-body') because it has no parent".

The shape that kills it: the first thinking delta of round 2. The tool card of
round 1 means round 2 opens a NEW AssistantMessage (claude_turn.card_for ->
app._assistant_bubble, whose mount is not awaited), and the ThinkingBlock was
mounted `before=widget.body` in the same tick, before that card had composed.

Real app, real ChatLog and widgets; only the Claude SDK session is a fake.
"""

import asyncio
from types import SimpleNamespace

import pytest
from test_claude_turn import FakeBackend, FakeSession
from test_claude_turn_order_pilot import _app, _assistant, _stream, _tool_result

from litetui.claude_events import ClaudeEventStream
from litetui.claude_turn import prepare_input, stream_turn
from litetui.widgets import AssistantMessage, ThinkingBlock, ToolMessage


def _thinking_round(message_id, thought, tool_id):
    return [
        _stream({"type": "message_start", "message": {"id": message_id, "model": "m"}}),
        _stream({"type": "content_block_delta",
                 "delta": {"type": "thinking_delta", "thinking": thought}}),
        _assistant(message_id, [{"type": "thinking", "thinking": thought},
                                {"type": "tool_use", "id": tool_id, "name": "Read",
                                 "input": {"file_path": tool_id}}]),
        _stream({"type": "message_stop"}),
        _tool_result(tool_id, "contents of " + tool_id),
    ]


FRAMES = [
    *_thinking_round("msg_01", "Plan: read t1.", "t1"),
    *_thinking_round("msg_02", "Then read t2.", "t2"),
    _stream({"type": "message_start", "message": {"id": "msg_03", "model": "m"}}),
    _stream({"type": "content_block_delta",
             "delta": {"type": "thinking_delta", "thinking": "Now answer."}}),
    _stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Final answer."}}),
    _assistant("msg_03", [{"type": "thinking", "thinking": "Now answer."},
                          {"type": "text", "text": "Final answer."}]),
    _stream({"type": "message_stop"}),
    {"type": "result", "uuid": "u-res", "session_id": "sess-1", "is_error": False,
     "subtype": "success", "result": "Final answer."},
]


def _cards(log, skip=()):
    shown = []
    for w in log.children:
        if w in skip:
            continue
        if isinstance(w, AssistantMessage):
            blocks = list(w.query(ThinkingBlock))
            assert len(blocks) <= 1, f"{len(blocks)} thinking blocks in one card"
            thought = blocks[0].source if blocks else ""
            # The block must sit ABOVE the answer inside its card.
            if blocks:
                assert w.children.index(blocks[0]) < w.children.index(w.body)
            shown.append(f"think:{thought}|text:{w.answer_text}")
        elif isinstance(w, ToolMessage):
            shown.append("tool")
    return shown


IN_STREAM_ORDER = [
    "think:Plan: read t1.|text:", "tool",
    "think:Then read t2.|text:", "tool",
    "think:Now answer.|text:Final answer.",
]


async def _run_turn(app, tmp_path, frames):
    """test_claude_turn_order_pilot._run_turn, with the frames as an argument."""
    app.convo_dir = tmp_path
    app.convo_id = "convo"
    app._materialise_convo = lambda: None
    item = prepare_input(app, "hello", "strict", "typed")
    app._claude_active_input = {"content": "hello", **item}
    backend = FakeBackend(_PausingSession(frames), item["_claude_segment"])
    backend._claude_tools = SimpleNamespace(
        cancelled=SimpleNamespace(clear=lambda: None, set=lambda: None, is_set=lambda: False),
        stop=lambda: None, sdk_options=dict)
    backend._claude_events = ClaudeEventStream(session_id="sess-1")
    app.backend = backend
    await stream_turn(app)


async def _turn(app, pilot, tmp_path, frames=FRAMES):
    app.thinking_level = "high"
    log = app.query_one("#chat-log")
    before = set(log.children)
    said, system = [], app._system
    app._system = lambda text, *a, **k: (said.append(str(text)), system(text, *a, **k))[1]
    try:
        await _run_turn(app, tmp_path, frames)
        for _ in range(6):
            await pilot.pause()
    finally:
        app._system = system
    return log, before, [line for line in said if line.startswith("Claude:")]


@pytest.mark.asyncio
async def test_thinking_after_a_tool_call_mounts_in_its_own_card(tmp_path) -> None:
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        log, before, errors = await _turn(app, pilot, tmp_path)
        assert errors == []
        assert _cards(log, before) == IN_STREAM_ORDER


#: Not a frame: the fake session sleeps here, so Textual composes what is
#: mounted. Without it every frame lands in one tick and no card ever composes.
PAUSE = "pause"


class _PausingSession(FakeSession):
    async def events(self):
        for message in self._messages:
            if message == PAUSE:
                await asyncio.sleep(0.2)
                continue
            yield message


def _diverged_round(message_id, draft, trace, blocks, pause=False):
    """A thinking delta the snapshot then CONTRADICTS: reconcile="replace"."""
    return [
        _stream({"type": "message_start", "message": {"id": message_id, "model": "m"}}),
        _stream({"type": "content_block_delta",
                 "delta": {"type": "thinking_delta", "thinking": draft}}),
        *([PAUSE] if pause else []),
        _assistant(message_id, [{"type": "thinking", "thinking": trace}, *blocks]),
        _stream({"type": "message_stop"}),
    ]


DIVERGED = [
    # Round 1, after a pause: the card has composed -> mount the new block,
    # then remove the old one.
    *_diverged_round("msg_01", "Draft one.", "Plan: read t1.",
                     [{"type": "tool_use", "id": "t1", "name": "Read",
                       "input": {"file_path": "t1"}}], pause=True),
    _tool_result("t1", "contents of t1"),
    # Round 2: a card opened this tick, not composed -> the old block never mounted.
    *_diverged_round("msg_02", "Draft two.", "Final trace.",
                     [{"type": "text", "text": "Answer."}]),
    {"type": "result", "uuid": "u-res", "session_id": "sess-1", "is_error": False,
     "subtype": "success", "result": "Answer."},
]


@pytest.mark.asyncio
async def test_a_diverged_thinking_snapshot_leaves_one_block_per_card(tmp_path) -> None:
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        log, before, errors = await _turn(app, pilot, tmp_path, DIVERGED)
        assert errors == []
        assert _cards(log, before) == [
            "think:Plan: read t1.|text:", "tool", "think:Final trace.|text:Answer."]


@pytest.mark.asyncio
async def test_a_resumed_conversation_survives_a_think_high_turn(tmp_path) -> None:
    """Reopen a saved think:high turn, then run another one on top of it."""
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _turn(app, pilot, tmp_path)
        saved = [row for row in app.conversation if row.get("role") == "assistant"]
        app.conversation = [{"role": "user", "content": "hello"}, *saved]
        app._render_resumed(tmp_path / "convo.jsonl")
        for _ in range(6):
            await pilot.pause()
        log, before, errors = await _turn(app, pilot, tmp_path)
        assert errors == []
        assert _cards(log, before) == IN_STREAM_ORDER
