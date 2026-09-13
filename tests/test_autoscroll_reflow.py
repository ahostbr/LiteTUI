"""Regression arms for follow-lock rearming after layout shrink and real wheel input."""

import pytest
from textual import events

from litetui import app as app_mod
from litetui.widgets import ChatMessage, ToolMessage


def _app() -> app_mod.LiteTUI:
    app = app_mod.LiteTUI()
    app.available_models = ["a-model"]
    app.model_id = "a-model"
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.jobs[:] = []
    return app


async def _settle(pilot, frames: int = 6) -> None:
    for _ in range(frames):
        await pilot.pause()


async def _fill_and_park_at_tail(app, pilot):
    log = app.query_one("#chat-log")
    for i in range(25):
        log.mount(ChatMessage(f"filler line {i}"))
    await _settle(pilot)
    app._scroll_down(reader_acted=True)
    await _settle(pilot)
    assert log.scroll_y >= log.max_scroll_y - 2
    return log


async def _mount_tall_live_tool(app, pilot) -> ToolMessage:
    log = app.query_one("#chat-log")
    card = ToolMessage("bash")
    log.mount(card)
    await _settle(pilot)
    card.set_args("\n".join(f"argument line {i}" for i in range(40)))
    app._scroll_down()
    await _settle(pilot)
    assert card.expanded
    assert log.scroll_y >= log.max_scroll_y - 2
    return card


@pytest.mark.asyncio
async def test_collapsing_a_tool_does_not_break_follow_for_the_next_output() -> None:
    """Layout shrink is not reader intent; the next streamed growth must follow."""
    app = _app()
    async with app.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_park_at_tail(app, pilot)
        card = await _mount_tall_live_tool(app, pilot)
        expanded_bottom = log.max_scroll_y

        # Production order: result collapses the live card, then app._stream calls
        # _scroll_down. Textual applies both the reflow and scroll_end later.
        card.set_result("ok", True)
        app._scroll_down()
        await _settle(pilot)
        assert log.max_scroll_y < expanded_bottom
        assert log.scroll_y >= log.max_scroll_y - 2
        assert app._follow_anchor == log.scroll_y, (
            "the anchor recorded pre-collapse geometry instead of the settled end"
        )

        # A later stream event grows the log. It must not mistake the preceding
        # layout shrink for a human scrolling up.
        log.mount(ChatMessage("new output\n" * 8))
        app._scroll_down()
        await _settle(pilot)
        assert log.scroll_y >= log.max_scroll_y - 2, (
            f"follow lock died after collapse: y={log.scroll_y}, "
            f"max={log.max_scroll_y}, anchor={app._follow_anchor}"
        )


@pytest.mark.asyncio
async def test_wheel_back_to_bottom_rearms_follow_after_layout_shrink() -> None:
    """Ryan: wheel up unlocks; wheel back to bottom locks and keeps following."""
    app = _app()
    async with app.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_park_at_tail(app, pilot)
        card = await _mount_tall_live_tool(app, pilot)
        card.set_result("ok", True)
        app._scroll_down()
        await _settle(pilot)

        # Real wheel input, not scroll_to: deliberate upward reading must win.
        await pilot._post_mouse_events(
            [events.MouseScrollUp], widget=log, offset=(2, 2), times=3
        )
        up_y = log.scroll_y
        assert up_y < log.max_scroll_y - 2

        log.mount(ChatMessage("arrived while reader is up\n" * 3))
        app._scroll_down()
        await _settle(pilot)
        assert log.scroll_y < log.max_scroll_y - 2, "new output yanked the reader"

        # Real wheel back to the settled bottom must re-arm following even when
        # a prior collapse left the old app anchor above the new document end.
        await pilot._post_mouse_events(
            [events.MouseScrollDown], widget=log, offset=(2, 2), times=30
        )
        await _settle(pilot)
        assert log.scroll_y >= log.max_scroll_y - 2
        # Re-arm happens when the next output consults the user's settled
        # position; it does not require intercepting or replacing wheel input.

        log.mount(ChatMessage("subsequent live output\n" * 5))
        app._scroll_down()
        await _settle(pilot)
        assert log.scroll_y >= log.max_scroll_y - 2, (
            f"wheel return did not rearm: y={log.scroll_y}, "
            f"max={log.max_scroll_y}, anchor={app._follow_anchor}"
        )
