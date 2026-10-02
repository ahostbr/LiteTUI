"""T0324: arbitrary status/tool text must not become Textual markup."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult

from litetui.now_card import NowCard

# Verbatim DATA from the retained command; never executed by this test.
FATAL_COMMAND = (Path(__file__).parent / "fixtures/t0324-fatal-command.txt").read_text(encoding="utf-8").strip()



class CardApp(App):
    def compose(self) -> ComposeResult:
        card = NowCard(id="now-card")
        card.styles.display = "block"
        yield card


@pytest.mark.parametrize("command,width", [
    ("$ powershell [Diagnostics.Process] $true; [broken=$true]", 67),
    ("$ powershell " + FATAL_COMMAND, 82),
])
@pytest.mark.asyncio
async def test_reported_powershell_last_row_repaints_as_literal_text(command, width):
    async with CardApp().run_test(size=(width, 12)) as pilot:
        card = pilot.app.query_one(NowCard)
        card.last = command
        card.repaint(elapsed=time.monotonic() - 41)
        await pilot.pause()
        assert "no card claimed" in card.content
        assert "now responding 41s" in card.content
        assert "[Diagnostics.Process" in card.content
        assert card.render().plain == card.content


@pytest.mark.asyncio
async def test_card_tool_and_wait_rows_cannot_inject_markup(monkeypatch):
    async with CardApp().run_test(size=(100, 12)) as pilot:
        card = pilot.app.query_one(NowCard)
        monkeypatch.setattr(card, "read_task", lambda _agent: None)
        card.card_label = "T0324 [bold]literal[/bold] [broken=$true]"
        card.last = "[red]literal[/red]"
        card.repaint(tool=SimpleNamespace(tool_name="powershell [broken=$true]", _t0=time.monotonic()))
        assert "[bold]literal[/bold]" in card.content
        assert "now powershell [broken=$true]" in card.content
        assert card.render().plain == card.content
        token = card.begin_wait("[leader]", "[broken=$true]")
        assert "[leader]" in card.content
        assert card.render().plain == card.content
        card.end_wait(token)
