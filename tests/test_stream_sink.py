"""StreamSink: a streamed answer costs one render and one scroll per frame, not per token.

Baseline measured on the per-token path it replaces (2026-09-25, 500 deltas at
20ms): 580 scroll_end calls and 575 body repaints in 8.2s.
"""

import asyncio

import pytest

from litetui import app as app_mod
from litetui.stream_sink import MIN_INTERVAL, StreamSink
from litetui.widgets import AssistantMessage, ChatMessage


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


async def _card_at_tail(app, pilot):
    log = app.query_one("#chat-log")
    for i in range(20):
        log.mount(ChatMessage(f"filler {i}"))
    card = AssistantMessage()
    log.mount(card)
    await _settle(pilot)
    return log, card


def _count(obj, name, counts):
    original = getattr(obj, name)

    def counted(*a, **k):
        counts[name] += 1
        return original(*a, **k)

    setattr(obj, name, counted)


@pytest.mark.asyncio
async def test_a_long_stream_renders_and_scrolls_per_frame_not_per_token() -> None:
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        log, card = await _card_at_tail(app, pilot)
        counts = {"scroll_end": 0, "set_markdown": 0}
        _count(log, "scroll_end", counts)
        _count(card.body, "set_markdown", counts)
        sink = StreamSink(app, card)
        loop = asyncio.get_running_loop()
        started = loop.time()
        text = ""
        for i in range(500):
            text += f"word{i} " if i % 12 else f"word{i}\n\n"
            sink.show(text)
            await asyncio.sleep(0.004)
        await _settle(pilot)
        elapsed = loop.time() - started
        frames = elapsed / MIN_INTERVAL + 2
        assert counts["set_markdown"] <= frames, (counts, elapsed)
        assert counts["set_markdown"] < 500 / 3, counts
        assert counts["scroll_end"] <= counts["set_markdown"] + 2, counts
        sink.finish()
        await _settle(pilot)
        assert card.answer_text == text
        assert log.scroll_y >= log.max_scroll_y - 1, "the stream stopped following"


@pytest.mark.asyncio
async def test_finishing_does_not_reflow_what_the_stream_already_showed() -> None:
    """The end-of-turn swap from plain Text to Markdown was a visible jump."""
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        _, card = await _card_at_tail(app, pilot)
        sink = StreamSink(app, card)
        answer = "## Title\n\n- one\n- two\n\n```python\nx = 1\n```\n"
        sink.show(answer)
        await pilot.pause(MIN_INTERVAL * 3)
        await _settle(pilot)
        streamed = str(card.body.content)
        sink.finish(answer)
        assert str(card.body.content) == streamed
        assert "## Title" not in streamed, "the stream showed raw markdown"


@pytest.mark.asyncio
async def test_cancel_keeps_an_error_written_after_it() -> None:
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        _, card = await _card_at_tail(app, pilot)
        sink = StreamSink(app, card)
        sink.show("partial answer")
        assert sink.pending
        sink.cancel()
        card.body.content = "backend died"
        await pilot.pause(MIN_INTERVAL * 3)
        await _settle(pilot)
        assert str(card.body.content) == "backend died"


@pytest.mark.asyncio
async def test_streamed_brackets_render_literally() -> None:
    """Model text is never Textual markup: a tag-shaped fragment shows as typed."""
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        _, card = await _card_at_tail(app, pilot)
        sink = StreamSink(app, card)
        sink.show("see [bold]this[/bold] and [inbox from ba736bd4 · normal]")
        await pilot.pause(MIN_INTERVAL * 3)
        await _settle(pilot)
        shown = str(card.body.content)
        assert "[bold]this[/bold]" in shown, shown
        assert "[inbox from ba736bd4 · normal]" in shown, shown


@pytest.mark.asyncio
async def test_many_scroll_requests_in_one_frame_scroll_once() -> None:
    """ChatLog.watch_virtual_size and the stream both ask; one frame, one scroll."""
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        log, _ = await _card_at_tail(app, pilot)
        # Count queued scroll ACTIONS, not scroll_end: a layout that is still
        # settling legitimately asks again in a LATER frame (watch_virtual_size).
        counts = {"call_after_refresh": 0}
        _count(log, "call_after_refresh", counts)
        for _ in range(5):
            app._scroll_down()
        # At most one: 0 when the settling layout had already queued its own,
        # which these requests then share.
        assert counts["call_after_refresh"] <= 1, counts
        await _settle(pilot)
        assert app._scroll_queued is None, "the queued scroll ran and released the frame"
        before = counts["call_after_refresh"]
        app._scroll_down()
        assert counts["call_after_refresh"] == before + 1, "a later frame still scrolls"
