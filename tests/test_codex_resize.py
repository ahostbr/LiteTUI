"""Native lifecycle presentation in the real host layout, without inference."""

import pytest
from textual.widgets import Static

from litetui.app import LiteTUI
from litetui.codex_app_server import AppServer
from litetui.codex_tool_ui import CodexToolUI
from litetui.widgets import ToolMessage


@pytest.mark.asyncio
async def test_native_output_resize_retains_reader_position_and_fold(monkeypatch):
    async def forbid_start(*args, **kwargs):
        raise AssertionError("UI acceptance must not start inference")

    monkeypatch.setattr(AppServer, "start", forbid_start)
    app = LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.settings.autoscroll = False
    async with app.run_test(size=(110, 40)) as pilot:
        log = app.query_one("#chat-log")
        await log.mount(Static("Earlier history\n" * 60))
        ui = CodexToolUI(app, thread_id="synthetic", turn_id="resize")
        command = {"type": "commandExecution", "id": "command", "command": "synthetic long output"}
        await ui.item(command)
        card = list(app.query(ToolMessage))[-1]
        ui.progress({"itemId": "command", "delta": "\x1b[31mline\x1b[0m\n" * 5000})
        ui.flush_progress("command")
        await pilot.pause()
        assert len(ui.output["command"]) < 33000
        assert "Earlier live output omitted" in card.body.content.plain
        assert "\x1b" not in card.body.content.plain
        app.settings.autoscroll = True
        app._scroll_down(reader_acted=True)
        await pilot.pause()
        assert log.scroll_y > 5
        log.scroll_to(y=5, animate=False, force=True)
        await pilot.pause()
        assert log.scroll_y == 5
        final = "Authoritative result\n" + "unchanged full line\n" * 5000
        await ui.item({**command, "status": "failed", "exitCode": 1,
                       "aggregatedOutput": final, "durationMs": 1250}, True)
        await pilot.pause()
        assert not card.expanded and not card._ok
        assert final in card.body.content.plain
        assert "Earlier live output omitted" not in card.body.content.plain
        for width in (48, 110, 55):
            await pilot.resize_terminal(width, 40)
            await pilot.pause()
            assert not card.expanded
            assert log.scroll_y == 5
            assert card.region.right <= log.region.right
            assert "1.2s" in card.header.content.plain
        card.set_expanded(True)
        await pilot.pause()
        assert card.scroll.styles.display == "block"
        assert final in card.body.content.plain
        await pilot.resize_terminal(110, 40)
        await pilot.pause()
        assert card.expanded and log.scroll_y == 5
        ui.finish()
