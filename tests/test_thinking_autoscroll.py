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


async def _settled_at_the_bottom(a, pilot, log, n: int = 25) -> bool:
    """Wait until the app's OWN deferred scroll has landed on a settled layout.

    🔴 THE RACE THIS CLOSES, TRACED RATHER THAN GUESSED. `_fill_and_stream`
    mounts the assistant bubble plus a ThinkingBlock and then schedules
    `call_after_refresh(a._scroll_down)`. One bare `pilot.pause()` is not enough
    for that deferred call under load, and the test's next act — the READER
    scrolling up — then races it. Traced in-process at 32 busy workers,
    10 of 16 iterations reproduced, in TWO signatures with ONE cause:

        A (late override)   after block+deferred  scroll_y=17  max=26
                            after scroll_up to 9  scroll_y=28  max=28
            the deferred `_scroll_down` landed AFTER `scroll_to` and undid it.

        B (stale anchor)    after block+deferred  scroll_y=23  max=28
                            after scroll_up to 15 scroll_y=15  max=28
                            after burst           scroll_y=37  max=37
            the deferred call had NOT run, so `_follow_anchor` was still 17
            from the previous scroll; `_still_following` asks
            `scroll_y >= anchor - 2`, and 15 >= 15 is TRUE, so the burst
            followed a reader who had genuinely moved away.

    Both are the same fact: the test acted on a frame where the app had not
    finished its own scroll. So wait for two things together — `max_scroll_y`
    has STOPPED MOVING (the layout settled) and the app's anchor is AT that
    bottom (its deferred scroll ran after the growth, not before).

    ⚠️ THIS MUST NOT MAKE THE ASSERTION UNFAILABLE — `_settle.settle_until`'s
    rule. The wait is BOUNDED and its result is not asserted: if the product
    genuinely yanks the reader, the loop still exits and the caller's original
    assertion fires with its original message. Waiting changes WHEN the test
    acts, never WHETHER the app has to be right.
    """
    last = None
    for _ in range(n):
        await pilot.pause()
        now = log.max_scroll_y
        anchored = (a._follow_anchor is not None
                    and a._follow_anchor >= now - 2)
        if anchored and now == last:
            return True
        last = now
    return False


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
    # NOT `await pilot.pause()`: one frame is a guess about a deferred call and
    # a still-growing layout, and it loses 10 times in 16 under load. See
    # `_settled_at_the_bottom` for the two traced signatures.
    await _settled_at_the_bottom(a, pilot, log)

    if scroll_up_by:
        # Computed from the SETTLED scroll_y, for the same reason: a target
        # derived from a stale position aims at the wrong row.
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
