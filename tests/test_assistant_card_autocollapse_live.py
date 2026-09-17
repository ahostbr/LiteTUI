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
