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
        assert not app.events  # _execute_tool owns host RPC events
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
