"""Busy input admitted during Claude summary must retain a deliverable owner."""
from types import SimpleNamespace

import pytest
from test_claude_compact import app_for, summary_events

from litetui import claude_compact, claude_turn
from litetui.app import LiteTUI


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["typed", "rpc"])
@pytest.mark.parametrize("arrival", ["summary", "cleanup"])
async def test_busy_submit_during_summary_remains_deliverable_by_idle_flush_and_continue(tmp_path, monkeypatch, source, arrival):
    app, _ = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    monkeypatch.setattr(claude_turn, "launch_workspace", lambda _: str(tmp_path))
    old = claude_turn.ledger_for(app).selected
    session = app.backend.session
    busy = True
    app._chat_running = lambda: busy
    app.pending_image = None
    app.chosen_tool_profile = "autonomous"
    app.tools_enabled = True
    app._pending_input = []
    app._mcp_maintenance = False
    app.settings.enter_interrupts = False
    app._split_image_path = lambda text: (None, text)
    app._looks_like_image_path = lambda text: False
    app._oversize_refusal = lambda content: None
    app._user_bubble = lambda *args, **kwargs: SimpleNamespace(border_title="You queued")
    app.notify = lambda *args, **kwargs: None
    app._gui_next_operation_id = "new-rpc" if source == "rpc" else None
    app.call_after_refresh = lambda callback: None
    delivered = []
    monkeypatch.setattr("litetui.app.hook_host.start_prompt", lambda _, item: delivered.append(item))
    app._flush_pending_input = lambda: LiteTUI._flush_pending_input(app)

    async def events():
        for index, batch in enumerate(summary_events()):
            if index == 1 and arrival == "summary":
                LiteTUI._submit_text(app, "new instruction during summary", False, source=source)
            yield batch

    session.events = events
    if arrival == "cleanup":
        original_close = app.backend.close

        async def close():
            LiteTUI._submit_text(app, "new instruction during summary", False, source=source)
            await original_close()

        app.backend.close = close
    await claude_compact.compact(app, auto=True)
    busy = False
    assert claude_turn.ledger_for(app).selected["id"] == old["id"], "summary must not strand newly admitted input"
    assert app._pending_input[0]["_claude_segment"] == old["id"]
    assert claude_turn.queue_ready(app, app._pending_input[0])
    app._flush_pending_input()
    assert [item["content"] for item in delivered] == ["new instruction during summary"]
    # Delivery fake leaves it prepared: /continue must still find the same owner.
    delivered.clear()
    claude_turn.command(app, "continue")
    assert [item["content"] for item in delivered] == ["new instruction during summary"]
    assert app.emitted[-1][0] == "failed"
    if arrival == "summary":
        assert session is app.backend.session and not app.backend.closes
    else:
        assert app.backend.session is None and app.backend.closes == 1
    if source == "rpc":
        assert delivered[0]["_claude_entry"]["operation_id"] == "new-rpc"
        assert delivered[0]["_claude_entry"]["segment_id"] == old["id"]
