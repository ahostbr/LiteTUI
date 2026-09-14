import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from litetui.codex_tool_ui import CodexToolUI
from litetui.widgets import ToolMessage


class Host(App):
    def __init__(self):
        super().__init__()
        self.events = []
        self.running = []

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="chat-log")

    def _rpc_emit(self, event):
        self.events.append(event)

    def _tool_begin(self, card):
        self.running.append(card)

    def _tool_end(self, card):
        self.running.remove(card)

    def _scroll_down(self):
        pass

    def _assistant_bubble(self):
        from litetui.widgets import AssistantMessage

        card = AssistantMessage()
        self.query_one("#chat-log").mount(card)
        return card


@pytest.mark.asyncio
async def test_mcp_attachments_do_not_enter_cards_rpc_or_saved_display_trace():
    from copy import deepcopy

    app = Host()
    app._rpc = True
    saved = []
    ui = CodexToolUI(app, on_record=saved.append, thread_id="thread", turn_id="turn")
    item = {"id": "attachments", "type": "mcpToolCall", "tool": "inspect", "server": "synthetic",
            "arguments": {}, "status": "completed", "result": {"content": [
                {"type": "text", "text": "Visible explanation"},
                {"type": "image", "data": "PRIVATE_IMAGE_BYTES", "mimeType": "image/png"},
                {"type": "audio", "data": "PRIVATE_AUDIO_BYTES", "mimeType": "audio/wav"},
                {"type": "resource", "resource": {"uri": "synthetic://asset", "blob": "PRIVATE_BLOB_BYTES",
                                                    "mimeType": "application/octet-stream"}},
                {"type": "resource", "resource": {"uri": "synthetic://text", "text": "Visible resource"}},
            ], "structuredContent": {"count": 3}}}
    before = deepcopy(item)
    await ui.item(item, True)
    assert item == before  # Presentation never changes the native/model result.
    for text in (saved[-1]["result"], app.events[-1]["result"]):
        assert "PRIVATE" not in text
        assert "Image returned to Codex" in text and "Audio returned to Codex" in text
        assert "Binary resource returned to Codex" in text
        assert "Visible explanation" in text and "Visible resource" in text
        assert "synthetic://asset" in text and '"count": 3' in text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["interrupted", "cancelled", "declined", "failed"])
async def test_native_terminal_outcome_survives_storage_and_rpc(status):
    app = Host()
    app._rpc = True
    saved = []
    ui = CodexToolUI(app, on_record=saved.append, thread_id="thread", turn_id="turn")
    item = {"type": "commandExecution", "id": "call", "command": "echo",
            "status": status, "durationMs": 0}
    await ui.item(item, completed=True)
    await ui.item(item, completed=True)
    ui.finish()
    assert saved[-1]["state"] == status
    results = [event for event in app.events if event["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["status"] == status
    assert results[0]["ok"] is False
    assert results[0]["durationMs"] == 0


@pytest.mark.asyncio
async def test_host_calls_pair_by_scoped_id_with_late_and_duplicate_notifications():
    app = Host()
    async with app.run_test():
        ui = CodexToolUI(app, thread_id="thread", turn_id="turn")
        first = {
            "type": "dynamicToolCall",
            "id": "a",
            "tool": "litetui_echo",
            "arguments": {"text": "a"},
        }
        second = {**first, "id": "b", "arguments": {"text": "b"}}
        await ui.item(first)
        # Completion arriving before start must have unknown measured duration.
        await ui.item(
            {
                **second,
                "success": True,
                "contentItems": [{"type": "inputText", "text": "b"}],
            },
            True,
        )
        await ui.item(second)
        await ui.item({**first, "success": True, "durationMs": 500}, True)
        await ui.item({**first, "success": True, "durationMs": 500}, True)
        ui.finish()
        results = [e for e in app.events if e["type"] == "tool_result"]
        starts = [e for e in app.events if e["type"] == "tool_call"]
        assert [e["id"] for e in starts] == ["a", "b"]
        assert [e["id"] for e in results] == ["b", "a"]
        assert results[0]["durationMs"] is None and results[1]["durationMs"] == 500
        assert all(
            e["threadId"] == "thread"
            and e["turnId"] == "turn"
            and e["eventVersion"] == 1
            for e in app.events
        )


@pytest.mark.asyncio
async def test_output_bursts_are_coalesced_and_completion_cancels_pending_render():
    import asyncio

    app = Host()
    async with app.run_test():
        ui = CodexToolUI(app)
        item = {"type": "commandExecution", "id": "burst", "command": "echo"}
        await ui.item(item)
        for _ in range(100):
            ui.progress({"itemId": "burst", "delta": "x" * 1000})
        assert len([e for e in app.events if e["type"] == "tool_progress"]) == 1
        assert len(ui.output["burst"]) < 33000
        await asyncio.sleep(0.12)
        assert len([e for e in app.events if e["type"] == "tool_progress"]) == 2
        ui.progress({"itemId": "burst", "delta": "late"})
        await ui.item(
            {**item, "status": "completed", "aggregatedOutput": "final"}, True
        )
        count = len(app.events)
        await asyncio.sleep(0.12)
        assert len(app.events) == count
        assert not ui.progress_timers


@pytest.mark.asyncio
async def test_display_trace_replays_once_without_dispatch_or_fake_duration():
    from litetui.codex_trace import replay

    app = Host()
    async with app.run_test() as pilot:
        metadata = {
            "app_server_thread_id": "native-thread",
            "display_trace": {
                "version": 1,
                "items": [
                    {
                        "id": "a",
                        "turnId": "1",
                        "kind": "agentMessage",
                        "result": "Checking.",
                    },
                    {
                        "id": "b",
                        "turnId": "1",
                        "kind": "commandExecution",
                        "name": "command",
                        "args": "echo hi",
                        "state": "running",
                    },
                    {
                        "id": "c",
                        "turnId": "1",
                        "kind": "commandExecution",
                        "name": "command",
                        "args": "echo bye",
                        "state": "completed",
                        "ok": True,
                        "result": "bye",
                        "durationMs": 1234,
                    },
                ],
            },
        }
        seen = set()
        assert replay(app, metadata, seen) == 2
        assert replay(app, metadata, seen) == 0
        await pilot.pause()
        cards = list(app.query(ToolMessage))
        assert len(cards) == 2
        assert cards[0]._took is None and not cards[0]._ok
        assert "duration unknown" in cards[0].header.content.plain
        assert cards[1]._took == 1.234 and cards[1]._ok
        assert not app.events and not app.running


@pytest.mark.asyncio
async def test_recorded_completion_is_idempotent_and_preserves_partial_text():
    app = Host()
    recorded = {}
    async with app.run_test():
        ui = CodexToolUI(app, lambda record: recorded.update({record["id"]: record}))
        await ui.item({"type": "agentMessage", "id": "a", "text": ""})
        ui.agent_delta({"itemId": "a", "delta": "Partial reply"})
        item = {"type": "commandExecution", "id": "b", "command": "echo hi"}
        await ui.item(item)
        completed = {
            **item,
            "status": "completed",
            "aggregatedOutput": "hi",
            "exitCode": 0,
        }
        await ui.item(completed, True)
        await ui.item(completed, True)
        ui.finish()
        assert len(app.query(ToolMessage)) == 1
        assert recorded["a"]["result"] == "Partial reply"
        assert recorded["a"]["state"] == "interrupted"
        assert recorded["b"]["result"].endswith("hi")


@pytest.mark.asyncio
async def test_native_progress_plan_and_compaction_use_shared_widgets():
    from litetui.widgets import CompactionCard, FoldBlock

    app = Host()
    async with app.run_test(size=(55, 35)) as pilot:
        ui = CodexToolUI(app)
        item = {"type": "commandExecution", "id": "stream", "command": "echo"}
        await ui.item(item)
        ui.progress({"itemId": "stream", "delta": "first\nsecond\n"})
        card = next(iter(app.query(ToolMessage)))
        assert "Output (running)" in card.body.content.plain
        assert "first\nsecond" in card.body.content.plain
        await ui.item(
            {
                **item,
                "status": "completed",
                "aggregatedOutput": "final only",
                "exitCode": 0,
            },
            True,
        )
        assert "final only" in card.body.content.plain
        assert "first\nsecond" not in card.body.content.plain
        await ui.plan({"plan": [{"step": "Check", "status": "inProgress"}]})
        await ui.plan({"plan": [{"step": "Check", "status": "completed"}]})
        assert isinstance(ui.plan_card, FoldBlock)
        assert ui.plan_card.body.content.plain == "completed: Check"
        await ui.item({"type": "contextCompaction", "id": "compact"})
        assert len(app.query(CompactionCard)) == 1
        await ui.item({"type": "contextCompaction", "id": "compact"}, True)
        await pilot.pause()
        assert not ui.compactions and not ui.calls
        assert [e["type"] for e in app.events][-2:] == [
            "compaction_start",
            "compaction_end",
        ]


@pytest.mark.asyncio
async def test_concurrent_cards_complete_by_id_and_expand_at_narrow_width():
    app = Host()
    async with app.run_test(size=(55, 35)) as pilot:
        ui = CodexToolUI(app)
        first = {
            "type": "commandExecution",
            "id": "1",
            "command": "echo first",
            "status": "inProgress",
        }
        second = {
            "type": "commandExecution",
            "id": "2",
            "command": "echo second",
            "status": "inProgress",
        }
        await ui.item(first)
        await ui.item(second)
        cards = list(app.query(ToolMessage))
        assert len(cards) == 2 and all(card.expanded for card in cards)
        await ui.item(
            dict(
                second,
                status="completed",
                aggregatedOutput="second output",
                exitCode=0,
                durationMs=2500,
            ),
            True,
        )
        await ui.item(
            dict(
                first,
                status="completed",
                aggregatedOutput="first failed",
                exitCode=1,
                durationMs=1000,
            ),
            True,
        )
        await pilot.pause()
        assert not app.running
        assert cards[0]._ok is False and cards[1]._ok is True
        assert cards[1]._took == 2.5 and not cards[1].expanded
        cards[1].header.on_click()
        assert cards[1].expanded
        assert "second output" in cards[1].body.content.plain
        assert "echo second" in cards[1].body.content.plain
        assert [e["id"] for e in app.events if e["type"] == "tool_result"] == ["2", "1"]


@pytest.mark.asyncio
async def test_host_tool_card_and_interrupted_cleanup():
    app = Host()
    async with app.run_test() as pilot:
        ui = CodexToolUI(app)
        item = {
            "type": "dynamicToolCall",
            "id": "host",
            "tool": "litetui_read",
            "arguments": {"path": "test.txt"},
        }
        await ui.item(item)
        await ui.item(
            dict(
                item,
                status="completed",
                success=False,
                contentItems=[{"type": "inputText", "text": "missing file"}],
            ),
            True,
        )
        card = next(iter(app.query(ToolMessage)))
        assert card.tool_name == "read" and not card._ok
        assert "missing file" in card._result
        assert [e["type"] for e in app.events] == ["tool_call", "tool_result"]
        assert all(e["id"] == "host" for e in app.events)
        await ui.item(
            {
                "type": "commandExecution",
                "id": "pending",
                "command": "sleep",
                "status": "inProgress",
            }
        )
        ui.finish()
        await pilot.pause()
        assert not app.running and not ui.calls
        assert all(not card.expanded for card in app.query(ToolMessage))
        assert app.events[-1]["id"] == "pending"
        assert app.events[-1]["status"] == "interrupted"
