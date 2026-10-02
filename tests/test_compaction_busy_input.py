"""Busy input admitted during awaited Claude summary retains a deliverable owner."""
import asyncio
from types import SimpleNamespace

import pytest
from test_claude_compact import app_for, summary_events

from litetui import claude_compact, claude_turn
from litetui.app import LiteTUI


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["typed", "rpc"])
@pytest.mark.parametrize("arrival", ["summary", "cleanup"])
@pytest.mark.parametrize("preflight", [False, True])
async def test_busy_submit_during_summary_remains_deliverable_by_idle_flush_and_continue(
        tmp_path, monkeypatch, source, arrival, preflight):
    app, _ = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    monkeypatch.setattr(claude_turn, "launch_workspace", lambda _: str(tmp_path))
    ledger = claude_turn.ledger_for(app)
    old = ledger.selected
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
    callbacks = []
    app.call_after_refresh = callbacks.append
    delivered = []

    def deliver(_, item):
        # Substitute only provider dispatch; real admission/queue/ledger paths
        # still run. A terminal delivery cannot be replayed by /continue.
        delivered.append(item)
        ledger.update_delivery(item["_claude_entry"]["id"], "submitted")
        ledger.update_delivery(item["_claude_entry"]["id"], "terminal", stop_reason="stop")

    monkeypatch.setattr("litetui.app.hook_host.start_prompt", deliver)
    app._flush_pending_input = lambda: LiteTUI._flush_pending_input(app)
    if preflight:
        entry = ledger.prepare(old["id"], "original unsent task", "strict", "typed")
        item = {"content": entry["content"], "_claude_entry": entry,
                "_claude_segment": old["id"], "_claude_conversation": app.convo_id}
        app._claude_active_input = item
        app._claude_compact_resume = (app.backend, app.convo_id, old["id"], item, item["content"])

    waiting, release = asyncio.Event(), asyncio.Event()

    async def events():
        for index, batch in enumerate(summary_events()):
            if index == 1 and arrival == "summary":
                waiting.set()
                await release.wait()
            yield batch

    session.events = events
    if arrival == "cleanup":
        original_close = app.backend.close

        async def close():
            waiting.set()
            await release.wait()
            await original_close()

        app.backend.close = close
    maintenance = asyncio.create_task(claude_compact.compact(app, auto=True))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        # Actually concurrent: summary/cleanup is suspended on an SDK boundary.
        LiteTUI._submit_text(app, "new instruction during summary", False, source=source)
        release.set()
        await asyncio.wait_for(maintenance, timeout=2)
    finally:
        release.set()
        if not maintenance.done():
            maintenance.cancel()
        await asyncio.gather(maintenance, return_exceptions=True)
    busy = False
    assert ledger.selected["id"] == old["id"], "summary must not strand newly admitted input"
    assert app._pending_input[0]["_claude_segment"] == old["id"]
    assert claude_turn.queue_ready(app, app._pending_input[0])
    app._flush_pending_input()
    assert [item["content"] for item in delivered] == ["new instruction during summary"]
    assert not app._pending_input and not callbacks, "no stranded FIFO or stale synthetic continuation"
    newer = delivered[0]["_claude_entry"]
    if source == "rpc":
        assert newer["operation_id"] == "new-rpc" and newer["segment_id"] == old["id"]
    claude_turn.command(app, "continue")
    expected = ["new instruction during summary"] + (["original unsent task"] if preflight else [])
    assert [item["content"] for item in delivered] == expected
    assert not ledger.pending(old["id"]), "all recovered work remains usable in the original owner"
    assert app.emitted[-1][0] == "failed"
    if arrival == "summary":
        assert session is app.backend.session and not app.backend.closes
    else:
        assert app.backend.session is None and app.backend.closes == 1
