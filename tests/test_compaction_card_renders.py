"""The compaction card has room to paint its children.

Ryan: "the compact display is still not live streamed to the app ... compact just
shows this purple C and Compaction in yellow."

It was never a streaming fault. CompactionCard is a Vertical, a Vertical defaults
to `height: 1fr`, and inside #chat-log (a VerticalScroll) that collapses it to a
minimal box -- so only the FIRST child, the title, ever painted. The plan line,
the folded prompt, the thinking block, the tool cards and the streaming summary
all rendered into children with no room to exist. Compaction itself worked the
whole time, which is exactly why it read as "not streamed" rather than "broken".

The give-away is that the PLAN is set in __init__, before a single token is
streamed, and it was invisible too. No stream can explain that.

Asserted here as GEOMETRY, because that is what failed. A test that only checked
the widgets exist would have passed against the broken build -- they did exist,
they just had nowhere to be drawn.
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


PLAN = "12 messages -> summary * keeping the last 4 verbatim * 48,000 chars before"


@pytest.mark.asyncio
async def test_compaction_card_is_taller_than_its_title() -> None:
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        log = a.query_one("#chat-log")
        # FILL THE LOG FIRST. This is the condition that actually breaks it, and
        # leaving it out made this test pass against the broken build: with
        # height: 1fr the card takes a SHARE of the container, so in an empty log
        # it expands to fill and looks fine. It collapses only once there are
        # siblings to divide the space with -- i.e. in any real conversation.
        for i in range(30):
            log.mount(m.ToolMessage(f"filler_{i}"))
        await pilot.pause()

        card = m.CompactionCard(plan=PLAN, prompt_text="the compaction prompt", auto=False)
        await log.mount(card)
        await pilot.pause()

        # The collapsed card was exactly its title. Anything that renders the
        # plan as well must be taller than one row.
        assert card.size.height > 1, (
            f"the compaction card is {card.size.height} row(s) tall -- it has collapsed "
            "to its title and every child below it is invisible"
        )

        # And specifically: the plan, which exists before any streaming at all.
        plan = card.query_one(".compaction-plan")
        assert plan.size.height > 0 and plan.size.width > 0, (
            "the plan line has no drawable area -- the card is not sizing to its children"
        )


@pytest.mark.asyncio
async def test_streamed_children_have_room() -> None:
    """A thinking block and a tool card mounted into the card are actually drawable."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        card = m.CompactionCard(plan=PLAN, prompt_text="p", auto=True)
        await a.query_one("#chat-log").mount(card)
        await pilot.pause()

        before = card.size.height
        card.think("reasoning about what to keep")
        await pilot.pause()
        card.add_tool(m.ToolMessage("write_file"))
        await pilot.pause()

        assert card.size.height > before, (
            "streaming a thinking block and a tool into the card did not grow it -- "
            "the children are being mounted into a box that cannot expand"
        )
        assert card.thinking is not None and card.thinking.size.height > 0, (
            "the thinking block inside the compaction card has no drawable area"
        )


@pytest.mark.asyncio
async def test_compaction_card_shows_an_elapsed_clock() -> None:
    """Ryan: "it just needs to display timer elapsed ... during compaction".

    Compaction is the longest single operation the app performs and it was the
    only long one with no clock on it. The card stamps t0 at construction --
    construction IS the start -- and the shared elapsed loop ticks it, the same
    way it drives tool timers and the thinking header.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        card = m.CompactionCard(plan=PLAN, prompt_text="p", auto=False)
        await a.query_one("#chat-log").mount(card)
        await pilot.pause()

        live = str(card._title.content)
        assert "Compaction" in live
        # The ellipsis is the "still running" signal, matching render_progress.
        assert "…" in live, f"no live clock on the compaction title: {live!r}"

        # The clock advances. Compared as a delta so no duration FORMAT is
        # baked into the test -- only that time is being reported at all.
        before = str(card._title.content)
        card._t0 -= 5
        card.tick()
        await pilot.pause()
        assert str(card._title.content) != before, "the elapsed clock is frozen while live"

        # And it settles: a finished compaction reports how long it took rather
        # than resetting or ticking forever.
        card.finish("118 -> 1 (-99%)")
        await pilot.pause()
        settled = str(card._title.content)
        assert card._took is not None
        assert "…" not in settled, f"still counting after finish(): {settled!r}"
