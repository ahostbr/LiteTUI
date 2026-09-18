"""A user bubble is as tall as its text, not as tall as the screen.

RYAN, 2026-09-18: *"queue message broken ............"* — a `You · queued`
bubble rendered as a full-screen slab of background with the text stranded at
the top, or, once scrolled, as an empty coloured box.

ROOT CAUSE: `UserMessage` is a `Vertical`, and Textual's `Vertical` defaults to
`height: 1fr`. `.assistant-msg` has always carried `height: auto`; `.user-msg`
never did. Measured on a 100x40 pilot before the fix: one line of "hey buddy"
laid out **22 rows** with `styles.height == 1fr`. After: 5, the same as the
assistant card beside it.

WHY ONLY THE QUEUED ONE LOOKED *EMPTY*: `1fr` claims the free rows of the
container. A queued bubble mounts while the log still has rows to give away,
so it took the most — which is exactly when a user is watching it.

THIS IS A LAYOUT FACT, SO IT NEEDS A LAID-OUT WIDGET. A unit test on the class
cannot see it: the height comes from the App's CSS, not from `UserMessage`.
"""

import pytest

from litetui import app as app_mod
from litetui.widgets import AssistantMessage, UserMessage


def _app():
    app = app_mod.LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    return app


@pytest.mark.asyncio
async def test_a_one_line_bubble_does_not_claim_the_viewport() -> None:
    app = _app()
    async with app.run_test(size=(100, 40)) as pilot:
        log = app.query_one("#chat-log")
        bubble = UserMessage("hey buddy")
        log.mount(bubble)
        for _ in range(4):
            await pilot.pause()

        assert bubble.styles.height is not None
        assert str(bubble.styles.height) == "auto", (
            "a Vertical without an explicit height is 1fr and eats the screen")
        assert bubble.outer_size.height < log.size.height / 2, (
            f"one line of text took {bubble.outer_size.height} of "
            f"{log.size.height} rows")


@pytest.mark.asyncio
async def test_a_queued_bubble_is_the_same_height_as_a_delivered_one() -> None:
    """`queued` changes the TITLE, and must change nothing about the box."""
    app = _app()
    async with app.run_test(size=(100, 40)) as pilot:
        log = app.query_one("#chat-log")
        queued = UserMessage("hello", queued=True)
        delivered = UserMessage("hello")
        log.mount(queued)
        log.mount(delivered)
        for _ in range(4):
            await pilot.pause()

        assert "queued" in queued.border_title
        assert queued.outer_size.height == delivered.outer_size.height


@pytest.mark.asyncio
async def test_the_user_bubble_matches_the_assistant_card_beside_it() -> None:
    """The two sit in one column and hold the same one line; a reader seeing
    them differ by fifteen rows reads it as the app being broken, which is
    precisely how it was reported."""
    app = _app()
    async with app.run_test(size=(100, 40)) as pilot:
        log = app.query_one("#chat-log")
        user = UserMessage("hey buddy")
        card = AssistantMessage()
        log.mount(user)
        log.mount(card)
        for _ in range(4):
            await pilot.pause()
        card.body.update("hey buddy")
        for _ in range(4):
            await pilot.pause()

        assert user.outer_size.height == card.outer_size.height


@pytest.mark.asyncio
async def test_a_long_bubble_still_grows(pilot_size=(100, 40)) -> None:
    """`auto` must not become a cap — a pasted paragraph still gets its rows."""
    app = _app()
    async with app.run_test(size=(100, 40)) as pilot:
        log = app.query_one("#chat-log")
        short = UserMessage("one line")
        tall = UserMessage("\n".join(f"line {i}" for i in range(12)))
        log.mount(short)
        log.mount(tall)
        for _ in range(4):
            await pilot.pause()

        assert tall.outer_size.height > short.outer_size.height
