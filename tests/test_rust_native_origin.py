"""Native Claude producer attribution through real RPC serialization; no provider."""
from __future__ import annotations

import asyncio
import io
import json
from functools import partial
from types import SimpleNamespace

import pytest
from test_claude_turn import FakeThinking, result_event, turn_app

from litetui import rpc, widgets
from litetui.app import LiteTUI
from litetui.claude_events import ClaudeEvent
from litetui.event_origin import EventOrigin


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["stop", "error", "cancelled", "inactive"])
async def test_native_output_keeps_scheduled_origin_after_ambient_state_changes(tmp_path, monkeypatch, ending):
    from litetui.claude_turn import stream_turn

    monkeypatch.setattr(widgets, "ThinkingBlock", FakeThinking)
    monkeypatch.setattr(widgets, "ToolMessage",
                        lambda title: SimpleNamespace(set_args=lambda text: None))
    app = turn_app(tmp_path, events=[
        [ClaudeEvent(kind="thinking_delta", text="thinking", message_id="m1"),
         ClaudeEvent(kind="text_delta", text="partial answer", message_id="m1"),
         ClaudeEvent(kind="tool_use", tool_id="tool1", tool_name="Read",
                     tool_input={}, complete=False),
         ClaudeEvent(kind="usage"),
         ClaudeEvent(kind="notice", detail="controlled notice")],
        [ClaudeEvent(kind="message", text="corrected answer", message_id="m1",
                     reconcile="replace"), result_event()],
    ])
    wire = io.StringIO()
    monkeypatch.setattr(rpc, "_real_stdout", wire)
    app._rpc = True
    app._gui_rpc_enabled = True
    app._paint_now = lambda: None
    app._rpc_emit = partial(LiteTUI._rpc_emit, app)
    app._emit_turn_end = partial(LiteTUI._emit_turn_end, app)
    app._gui_operation_id = "ambient-before"
    origin = EventOrigin(session_id="convo", operation_id="operation-A",
                         hook_id="hook-A", source="rpc").scheduled()

    async def events():
        app._gui_operation_id = "operation-B"
        app._hook_turn_id = "hook-B"
        yield object()
        if ending != "stop":
            app.convo_id = "later-session"
        if ending == "error":
            raise RuntimeError("controlled provider error")
        if ending == "cancelled":
            raise asyncio.CancelledError
        yield object()
        app.convo_id = "later-session"

    app.backend.session.events = events
    if ending == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await stream_turn(app, origin=origin)
    else:
        await stream_turn(app, origin=origin)
    rows = [json.loads(line) for line in wire.getvalue().splitlines()]
    envelopes = [row for row in rows if row.get("type") == "gui.event"]
    assert {"turn_start", "text_delta", "reasoning_delta", "native_activity",
            "native_usage", "native_event", "turn_end"} <= {
        row["event"]["type"] for row in envelopes}
    for row in envelopes:
        assert row["session_id"] == "convo"
        assert row["operation_id"] == "operation-A"
        assert row["hook_id"] == "hook-A"
        assert row["execution_id"] == origin.execution_id
        assert row["source"] == "rpc"
        index = rows.index(row)
        assert rows[index - 1] == row["event"]
        assert "execution_id" not in row["event"] and "origin" not in row["event"]
    expected = "error" if ending == "inactive" else ending
    assert envelopes[-1]["event"]["stopReason"] == expected
    if ending == "inactive":
        assert "inactive conversation" in envelopes[-1]["event"]["error"]
        assert not any(row["event"]["type"] == "native_text_snapshot" for row in envelopes)
    elif ending == "stop":
        assert any(row["event"]["type"] == "native_text_snapshot"
                   and row["event"]["text"] == "corrected answer" for row in envelopes)


@pytest.mark.asyncio
async def test_real_worker_passes_its_captured_origin_to_native_stream(tmp_path, monkeypatch):
    from litetui import claude_turn

    app = turn_app(tmp_path)
    async def maintenance():
        pass
    app._await_mcp_maintenance = maintenance
    seen = []
    async def capture(host, *, origin=None):
        assert host is app
        seen.append(origin)
    monkeypatch.setattr(claude_turn, "stream_turn", capture)
    origin = EventOrigin(session_id="source-session", operation_id="A").scheduled()
    await LiteTUI._stream_worker(app, origin)
    assert seen == [origin]
