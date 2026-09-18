"""Behaviour test for the same-frame auto-collapse, against a live app.

RYAN, 2026-09-16: *"it should auto colapse in the same frame that the autoscroll
would have naturally occured anyways to make it seemless."*

The unit tests next door cover the header strings and the latch on a bare widget.
What they cannot reach is the part that only exists once there is a real layout:
the geometry decision, the follow lock it inherits, and whether the folded card
actually stops painting its body. That is what this file drives with a pilot.
"""

import pytest

from litetui import app as app_mod
from litetui.widgets import AssistantMessage


def _app():
    app = app_mod.LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    return app


async def _card(app, pilot, text: str, *, model: str, settled: bool = True):
    card = AssistantMessage()
    card.set_model_name(model)
    app.query_one("#chat-log").mount(card)
    await pilot.pause()
    card.body.update(text)
    card.settled = settled
    await pilot.pause()
    return card


@pytest.mark.asyncio
async def test_header_shows_the_model_not_the_word_AI() -> None:
    app = _app()
    async with app.run_test(size=(100, 20)) as pilot:
        card = await _card(app, pilot, "hello", model="qwen3-30b", settled=False)
        assert "qwen3-30b" in card.border_title


@pytest.mark.asyncio
async def test_card_on_screen_stays_expanded() -> None:
    """A short conversation fits, so scroll_y is 0 and nothing is above the fold.

    This is the case that would regress first if the geometry were replaced by
    "fold every card except the last".
    """
    app = _app()
    async with app.run_test(size=(100, 40)) as pilot:
        first = await _card(app, pilot, "short one", model="m1")
        await _card(app, pilot, "short two", model="m2")
        app._scroll_down(reader_acted=True)
        await pilot.pause()
        await pilot.pause()
        assert not first.collapsed


@pytest.mark.asyncio
async def test_card_scrolled_above_the_fold_collapses_itself() -> None:
    app = _app()
    async with app.run_test(size=(100, 14)) as pilot:
        first = await _card(app, pilot, "FIRST\n" + "\n".join(f"line {i}" for i in range(40)),
                            model="qwen3-30b")
        await _card(app, pilot, "SECOND\n" + "\n".join(f"line {i}" for i in range(40)),
                    model="qwen3-30b")
        app._scroll_down(reader_acted=True)
        await pilot.pause()
        await pilot.pause()

        assert first.collapsed, "a settled card pushed above the viewport should fold"
        # The fold has to actually stop the body painting, not merely set a class.
        assert first.body.display is False
        # …and the header must survive, or the card cannot be re-opened.
        assert "qwen3-30b" in first.border_title


@pytest.mark.asyncio
async def test_streaming_card_is_never_folded() -> None:
    """An unsettled card is the one being read, however far it has scrolled."""
    app = _app()
    async with app.run_test(size=(100, 14)) as pilot:
        first = await _card(app, pilot, "\n".join(f"line {i}" for i in range(40)),
                            model="m1", settled=False)
        await _card(app, pilot, "\n".join(f"line {i}" for i in range(40)), model="m2")
        app._scroll_down(reader_acted=True)
        await pilot.pause()
        await pilot.pause()
        assert not first.collapsed


@pytest.mark.asyncio
async def test_reader_scrolled_up_is_never_folded_under(monkeypatch) -> None:
    """THE FOLLOW LOCK. Auto-collapse lives inside _scroll_down's deferred
    action precisely so it cannot run while the reader is reading history:
    _scroll_down has already returned by then. If the fold ever moves to a
    timer or a scroll event, this is the test that fails."""
    app = _app()
    async with app.run_test(size=(100, 14)) as pilot:
        first = await _card(app, pilot, "\n".join(f"line {i}" for i in range(40)), model="m1")
        await _card(app, pilot, "\n".join(f"line {i}" for i in range(40)), model="m2")

        # The reader has wheeled up: not following any more.
        monkeypatch.setattr(type(app), "_still_following", lambda self, log: False)
        app._scroll_down()
        await pilot.pause()
        await pilot.pause()
        assert not first.collapsed, "a card must never fold while the reader is reading"


@pytest.mark.asyncio
async def test_autoscroll_disabled_never_folds(monkeypatch) -> None:
    app = _app()
    async with app.run_test(size=(100, 14)) as pilot:
        first = await _card(app, pilot, "\n".join(f"line {i}" for i in range(40)), model="m1")
        await _card(app, pilot, "\n".join(f"line {i}" for i in range(40)), model="m2")
        monkeypatch.setattr(app.settings, "autoscroll", False)
        app._scroll_down()
        await pilot.pause()
        await pilot.pause()
        assert not first.collapsed


@pytest.mark.asyncio
async def test_fold_does_not_cascade_or_oscillate() -> None:
    """Repeated frames must reach a fixed point: the latch means a second pass
    changes nothing, so a height change can never feed another collapse."""
    app = _app()
    async with app.run_test(size=(100, 14)) as pilot:
        cards = []
        for n in range(4):
            cards.append(await _card(app, pilot, "\n".join(f"c{n} line {i}" for i in range(30)),
                                     model=f"m{n}"))
        app._scroll_down(reader_acted=True)
        await pilot.pause()
        await pilot.pause()
        first_pass = [c.collapsed for c in cards]

        for _ in range(3):
            app._scroll_down(reader_acted=True)
            await pilot.pause()
        assert [c.collapsed for c in cards] == first_pass, "fold must be a fixed point"
        assert cards[-1].collapsed is False, "the newest card is the one being read"


# ── the round boundary ───────────────────────────────────────────────────
#
# 🔴 EVERY TEST ABOVE SETS `settled` ITSELF (`_card(..., settled=True)`), so
# none of them can see whether the APP ever sets it. It did not, for any card
# but a turn's last: `_settle_turn_stop_line` is guarded by
# `_turn_stop_line_settled`, reset once per TURN, while an agentic turn mounts
# one card per ROUND. Measured on Ryan's screen 2026-09-18 — an 11m 17s
# autonomous turn, a dozen expanded cards — with the whole suite green.
#
# ⚠️ SETTLING ONLY. `_assistant_bubble` deliberately does NOT ask for a card
# summary: `test_real_stream_rejected_completion_never_finalizes` pins one
# summary per TURN, and a summary is a model call. Collapse is what needs
# `settled`, and collapse is what was missing.


@pytest.mark.asyncio
async def test_a_new_bubble_settles_the_card_before_it() -> None:
    app = _app()
    async with app.run_test(size=(100, 20)) as pilot:
        first = app._assistant_bubble()
        await pilot.pause()
        first.body.update("round one answer")
        assert not first.settled, "a card is not settled while it is being written"

        second = app._assistant_bubble()   # the next round begins
        await pilot.pause()

        assert first.settled, "mounting the next card must settle the one before it"
        assert not second.settled, "the live card is never settled by its own mount"


@pytest.mark.asyncio
async def test_settling_at_the_round_boundary_costs_no_model_call() -> None:
    """The guard on the narrowed fix: a dozen rounds must not bill a dozen
    summaries."""
    app = _app()
    async with app.run_test(size=(100, 20)) as pilot:
        kicked: list = []
        app._kick_card_summary = kicked.append

        for _ in range(3):
            app._assistant_bubble()
            await pilot.pause()

        assert kicked == [], "the round boundary must not ask for a summary"


@pytest.mark.asyncio
async def test_a_mid_turn_card_can_now_actually_fold() -> None:
    """The whole point: cards from earlier ROUNDS fold once they scroll away.
    Before this, only a turn's last card ever could."""
    app = _app()
    async with app.run_test(size=(100, 14)) as pilot:
        first = app._assistant_bubble()
        await pilot.pause()
        first.set_model_name("qwen3-30b")
        first.body.update("FIRST\n" + "\n".join(f"line {i}" for i in range(40)))
        second = app._assistant_bubble()
        await pilot.pause()
        second.body.update("SECOND\n" + "\n".join(f"line {i}" for i in range(40)))

        app._scroll_down(reader_acted=True)
        await pilot.pause()
        await pilot.pause()

        assert first.collapsed, "a mid-turn card pushed above the viewport must fold"
