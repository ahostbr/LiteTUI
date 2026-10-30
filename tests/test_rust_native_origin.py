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


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", [False, True])
async def test_cache_warning_refuses_scheduled_invocation_without_start_or_provider_send(tmp_path, monkeypatch, recovered):
    from test_claude_turn import effort_app

    from litetui import claude_cache, claude_turn
    from litetui.claude_persistence import ClaudeLedger

    actual_confirm = claude_cache.confirm_cold
    app, session, _ = effort_app(tmp_path, monkeypatch, answer=False)
    monkeypatch.setattr(claude_cache, "confirm_cold", actual_confirm)
    app._rpc = True
    app._gui_rpc_enabled = True
    app._gui_operation_id = "ambient"
    wire = io.StringIO()
    monkeypatch.setattr(rpc, "_real_stdout", wire)
    app._rpc_emit = partial(LiteTUI._rpc_emit, app)
    app._claude_active_input["_restored_claude_continuation"] = recovered
    entry = app._claude_active_input["_claude_entry"]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A cache refusal cannot start the provider or create an answer")
    app._assistant_bubble = forbidden
    session.query = forbidden
    origin = EventOrigin(session_id="convo", operation_id="fresh-invocation",
                         hook_id="saved-hook", source="harness").scheduled()
    await claude_turn.stream_turn(app, origin=origin)

    rows = [json.loads(line) for line in wire.getvalue().splitlines()]
    envelopes = [row for row in rows if row["type"] == "gui.event"]
    assert [row["event"]["type"] for row in envelopes] == ["cache_warning", "turn_refused"]
    refusal = envelopes[-1]
    assert {key: refusal[key] for key in origin.envelope_fields()} == origin.envelope_fields()
    assert rows[-2] == refusal["event"]
    assert refusal["event"]["provider"] == "claude" and refusal["event"]["reason"]
    assert session.queried == session.efforts == []
    assert app.appended == [] and app.backend.closes == 0
    segment = ClaudeLedger(tmp_path).segment(entry["segment_id"])
    assert segment is not None
    saved = segment["entries"][0]
    assert saved["id"] == entry["id"]
    assert saved["state"] == ("prepared" if recovered else "terminal")
    assert not any(row.get("type") in {"turn_start", "turn_end", "text_delta"} for row in rows)
    assert app._claude_cache_ack[0] == "effort"  # still requires a new explicit invocation


@pytest.mark.asyncio
async def test_refusal_cannot_hide_ambiguous_cache_warning_persistence(tmp_path, monkeypatch):
    from test_claude_turn import effort_app

    from litetui import claude_cache, claude_turn

    actual_confirm = claude_cache.confirm_cold
    app, _, _ = effort_app(tmp_path, monkeypatch, answer=False)
    monkeypatch.setattr(claude_cache, "confirm_cold", actual_confirm)
    app._rpc = app._gui_rpc_enabled = True
    app._gui_operation_id = "ambient"
    wire = io.StringIO()
    monkeypatch.setattr(rpc, "_real_stdout", wire)
    app._rpc_emit = partial(LiteTUI._rpc_emit, app)

    def fail(*_args, **_kwargs):
        raise OSError("Controlled persistence failure")
    monkeypatch.setattr(app._claude_ledger, "update_delivery", fail)
    with pytest.raises(OSError, match="Controlled persistence"):
        await claude_turn.stream_turn(app, origin=EventOrigin(
            session_id="convo", operation_id="attempt").scheduled())
    rows = [json.loads(line) for line in wire.getvalue().splitlines()]
    assert [row["type"] for row in rows] == ["cache_warning", "gui.event"]


@pytest.mark.asyncio
async def test_missing_admission_refuses_with_captured_origin_without_fake_completion(tmp_path, monkeypatch):
    from litetui import claude_turn

    app = turn_app(tmp_path)
    app._claude_active_input = None
    app._rpc = app._gui_rpc_enabled = True
    app._gui_operation_id = "other"
    wire = io.StringIO()
    monkeypatch.setattr(rpc, "_real_stdout", wire)
    app._rpc_emit = partial(LiteTUI._rpc_emit, app)
    origin = EventOrigin(session_id="convo", operation_id="invocation", source="rpc").scheduled()
    await claude_turn.stream_turn(app, origin=origin)
    raw, envelope = [json.loads(line) for line in wire.getvalue().splitlines()]
    assert raw["type"] == "turn_refused" and "without admission" in raw["reason"]
    assert envelope["event"] == raw
    assert {key: envelope[key] for key in origin.envelope_fields()} == origin.envelope_fields()
    assert app.backend.session.queried == []


@pytest.mark.asyncio
async def test_recovered_compaction_refusal_keeps_original_entry_without_migration(tmp_path, monkeypatch):
    from litetui import claude_turn

    app = turn_app(tmp_path)
    app._claude_active_input["_restored_claude_continuation"] = True
    assert app._claude_active_input["_claude_entry"].get("operation_id") is None
    app._autocompact_due = lambda: "controlled full context"
    await claude_turn.stream_turn(app)
    assert app.backend.session.queried == []
    assert app._claude_compact_resume is None
    assert app._claude_ledger.pending(app._claude_ledger.selected["id"])[0]["state"] == "prepared"
    assert app.emitted[-1] == {"turn_end": "cancelled"}
