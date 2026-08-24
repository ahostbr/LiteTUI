"""The log keeps following while a thinking block fills.

Ryan, twice: "AS soon as a thinking block starts the main screen autoscroll needs
to tick" -- and, after the first attempt, "still not scrolling".

The first fix deferred the mount-time scroll so it measured before scrolling.
Necessary, but it only closed one of two ways to fall behind. The second is the
one that actually bites:

  .thinking-body is `max-height: 10`, so a block grows the outer log by ~12 rows
  in a burst as the first tokens land, then stops growing forever. That single
  burst is far more than _at_bottom's 2-line slack, so the follow check goes
  False -- and because the block never grows again, NOTHING pulls it back. The
  turn finishes with the viewport parked ten rows above the live trace.

The guard cannot tell "the reader scrolled up" (leave them alone) from "the
content outran us" (catch up), and those want opposite responses. Geometry alone
cannot answer it, because both look identical: scroll_y sits below max_scroll_y.

What distinguishes them: content growth moves max_scroll_y and leaves scroll_y
untouched. Only a human moves scroll_y. So the anchor is the app's OWN last
scroll position, not the bottom of the document.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.jobs[:] = []
    return a


async def _fill_and_stream(a, pilot, *, scroll_up_by: int = 0):
    """A crowded log, a thinking block, then a burst of reasoning."""
    log = a.query_one("#chat-log")
    for i in range(25):
        log.mount(m.ToolMessage(f"filler_{i}"))
    await pilot.pause()
    a._scroll_down()
    await pilot.pause()

    # Mirror _stream EXACTLY: the bubble mounts and scrolls (un-deferred, in
    # _assistant_bubble), then the block mounts INSIDE it on the first reasoning
    # token. Mounting a bare block into the log does not reproduce this -- the
    # docstring blames "the assistant bubble PLUS the block" in one frame.
    widget = a._assistant_bubble()
    await pilot.pause()
    block = m.ThinkingBlock()
    widget.thinking = block
    widget.mount(block, before=widget.body)
    a.call_after_refresh(a._scroll_down)
    await pilot.pause()

    if scroll_up_by:
        log.scroll_to(y=max(0, log.scroll_y - scroll_up_by), animate=False)
        await pilot.pause()

    # The burst: this is what takes the block past the 10-row cap.
    for i in range(40):
        block.append(f"a line of reasoning, number {i}\n")
        a._scroll_down(only_if_following=True)
        await pilot.pause()

    return log


@pytest.mark.asyncio
async def test_log_follows_a_filling_thinking_block() -> None:
    a = make_app()
    async with a.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_stream(a, pilot)
        assert log.scroll_y >= log.max_scroll_y - 2, (
            f"the log fell behind: scroll_y={log.scroll_y} vs "
            f"max_scroll_y={log.max_scroll_y}. The thinking block grew past the "
            "follow slack and following was never regained."
        )


@pytest.mark.asyncio
async def test_a_reader_who_scrolled_up_is_left_alone() -> None:
    """The other half of the contract, and the reason the slack existed at all.

    Following must NOT drag a reader back down. If this passes only because the
    app always scrolls, the fix has traded one bug for a worse one.
    """
    a = make_app()
    async with a.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_stream(a, pilot, scroll_up_by=8)
        assert log.scroll_y < log.max_scroll_y - 2, (
            "the app yanked a reader who had deliberately scrolled up back to the "
            "bottom -- following must yield to a human"
        )


@pytest.mark.asyncio
async def test_autoscroll_setting_still_gates_the_stream() -> None:
    a = make_app()
    a.settings.autoscroll = False
    async with a.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_stream(a, pilot)
        assert log.scroll_y < log.max_scroll_y - 2, (
            "autoscroll is off, but the stream path scrolled anyway"
        )
