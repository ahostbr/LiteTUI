"""The scroll lock against REAL input (T0152).

THE CONTRACT is Ryan's, 2026-10-22, answering "With the lock on, which should happen
when you scroll up yourself? 1. Scrolling up releases the lock ... 2. Only the button
releases it. While the lock is on you always follow the bottom, and you turn it off
before scrolling back through history." His answer: **"2"**. On the lock button itself he said: "yes i did ask for the scroll lock button".

  (a) lock ON  = always follow the bottom. The reader's own wheel/PageUp/Home does NOT
      release it, and being taken back to the bottom is correct, not a bug.
  (b) lock OFF = never auto-scroll; the reader's position holds while output streams.
  (c) ONLY the button toggles the lock; scrolling never changes it, in either state.
  (d) his 09-20 complaints bind inside that design (*"it will not scroll down sometimes
      after I've scrolled back up"*, *"it will just seem to like lose track of where it
      is and start scrolling up"*): with the lock ON, content growth, reflow, a tool card
      collapsing and a resized terminal must never lose the bottom.

(The 09-12 sentence "if i start scrolling up on my own it drags me back down no matter
what" describes the pre-lock design; the lock replaced it, and "2" confirmed that.)

Input is real Textual wheel/keyboard/click events, and output is mounted the way a stream
does it (ChatLog.watch_virtual_size asks for the follow itself). The two queued-follow arms
drive the follow seam directly: they call app._scroll_down() and hold log.call_after_refresh
to freeze the deferred scroll. Arms read app.settings.autoscroll and the lock glyph to check
that scrolling never toggles the lock.
"""

import pytest
from _owned_seat import owned_app
from textual import events

from litetui import app as app_mod
from litetui.widgets import ChatMessage, ToolMessage


def _app() -> app_mod.LiteTUI:
    app = owned_app()
    app.available_models = ["a-model"]
    app.model_id = "a-model"
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.jobs[:] = []
    return app


async def _settle(pilot, frames: int = 6) -> None:
    for _ in range(frames):
        await pilot.pause()


def _at_tail(log) -> bool:
    return log.scroll_y >= log.max_scroll_y - 2


def _lock_glyph(app) -> str:
    return str(app.query_one("#scroll-lock").render())


async def _wheel(pilot, log, direction, times: int) -> None:
    await pilot._post_mouse_events([direction], widget=log, offset=(2, 2), times=times)


async def _stream(pilot, log, count: int, tag: str) -> None:
    for i in range(count):
        log.mount(ChatMessage(f"{tag} {i}\nline2\nline3"))
        await _settle(pilot, 3)


async def _fill_and_park_at_tail(app, pilot):
    log = app.query_one("#chat-log")
    for i in range(25):
        log.mount(ChatMessage(f"filler line {i}"))
    await _settle(pilot)
    app._scroll_down(reader_acted=True)
    await _settle(pilot)
    assert _at_tail(log)
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
    assert _at_tail(log)
    return card


# ── (a) lock ON always follows ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wheel_up_with_the_lock_on_neither_releases_it_nor_holds_the_reader() -> None:
    app = _app()
    async with app.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_park_at_tail(app, pilot)
        locked = _lock_glyph(app)

        await _wheel(pilot, log, events.MouseScrollUp, 6)
        await _settle(pilot)
        assert not _at_tail(log), "the wheel did not move the view; the arm proves nothing"
        assert app.settings.autoscroll and _lock_glyph(app) == locked, "scrolling toggled the lock"

        await _stream(pilot, log, 5, "output while the lock is on")
        assert _at_tail(log), f"lock ON must always follow: y={log.scroll_y}, max={log.max_scroll_y}"


@pytest.mark.asyncio
async def test_scroll_keys_never_change_the_lock_and_output_still_lands_at_the_bottom() -> None:
    app = _app()
    async with app.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_park_at_tail(app, pilot)
        locked = _lock_glyph(app)
        log.focus()
        for key in ("pageup", "home", "pagedown", "end", "pageup"):
            await pilot.press(key)
            await _settle(pilot, 3)
            assert app.settings.autoscroll and _lock_glyph(app) == locked, f"{key} toggled the lock"

        assert not _at_tail(log)
        await _stream(pilot, log, 5, "output after paging away")
        assert _at_tail(log)


@pytest.mark.asyncio
async def test_a_follow_queued_before_the_wheel_still_lands_at_the_bottom() -> None:
    """Overlap real input with the first deferred scroll seam: the lock outranks the wheel."""
    app = _app()
    async with app.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_park_at_tail(app, pilot)

        held = []
        original_call_after_refresh = log.call_after_refresh
        log.call_after_refresh = lambda callback, *args, **kwargs: held.append(
            (callback, args, kwargs)
        )
        app._scroll_down()
        assert len(held) == 1
        log.call_after_refresh = original_call_after_refresh

        await _wheel(pilot, log, events.MouseScrollUp, 3)
        assert not _at_tail(log)

        callback, args, kwargs = held.pop()
        callback(*args, **kwargs)
        await _settle(pilot)
        assert _at_tail(log), "a lock-ON follow queued before the wheel did not land"


@pytest.mark.asyncio
async def test_the_lock_survives_a_collapsing_tool_and_the_next_output_follows() -> None:
    """Layout shrink must not lose the bottom (09-20: "lose track of where it is")."""
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
        assert _at_tail(log), "collapse left the locked view above the settled end"

        log.mount(ChatMessage("new output\n" * 8))
        await _settle(pilot)
        assert _at_tail(log), (
            f"follow lock died after collapse: y={log.scroll_y}, max={log.max_scroll_y}"
        )


@pytest.mark.asyncio
async def test_wheel_noise_after_a_layout_shrink_never_loses_the_bottom() -> None:
    """09-20: "it will not scroll down sometimes after I've scrolled back up"."""
    app = _app()
    async with app.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_park_at_tail(app, pilot)
        card = await _mount_tall_live_tool(app, pilot)
        card.set_result("ok", True)
        app._scroll_down()
        await _settle(pilot)

        await _wheel(pilot, log, events.MouseScrollUp, 3)
        assert not _at_tail(log)
        log.mount(ChatMessage("arrived while the reader was wheeled up\n" * 3))
        await _settle(pilot)
        assert _at_tail(log), "new output did not land at the bottom after a wheel-up"

        await _wheel(pilot, log, events.MouseScrollDown, 30)
        await _settle(pilot)
        log.mount(ChatMessage("subsequent live output\n" * 5))
        await _settle(pilot)
        assert _at_tail(log), f"y={log.scroll_y}, max={log.max_scroll_y}"


@pytest.mark.asyncio
async def test_resize_and_repeated_card_reflow_keep_the_locked_view_at_the_bottom() -> None:
    app = _app()
    async with app.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_park_at_tail(app, pilot)
        for round_ in range(3):
            card = await _mount_tall_live_tool(app, pilot)
            card.set_result("ok", True)
            await _settle(pilot)
            assert _at_tail(log), f"round {round_}: collapse lost the bottom"
            await pilot.resize_terminal(100, 12)
            await _settle(pilot)
            assert _at_tail(log), f"round {round_}: shrinking the terminal lost the bottom"
            await pilot.resize_terminal(100, 24)
            await _settle(pilot)
            assert _at_tail(log), f"round {round_}: growing the terminal lost the bottom"
            await _stream(pilot, log, 2, f"round {round_}")
            assert _at_tail(log)


# ── (b) lock OFF never auto-scrolls; (c) only the button toggles ───────────


@pytest.mark.asyncio
async def test_lock_off_holds_the_readers_position_and_scrolling_never_re_locks() -> None:
    app = _app()
    async with app.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_park_at_tail(app, pilot)
        await pilot.click("#scroll-lock")
        await _settle(pilot)
        assert not app.settings.autoscroll
        unlocked = _lock_glyph(app)

        await _wheel(pilot, log, events.MouseScrollUp, 6)
        await _settle(pilot)
        read_y = log.scroll_y
        assert read_y < log.max_scroll_y - 2
        await _stream(pilot, log, 5, "output while unlocked")
        assert log.scroll_y == read_y, "an unlocked view moved while output streamed"

        # Back to the bottom by hand does not turn the lock on: only the button does.
        await _wheel(pilot, log, events.MouseScrollDown, 80)
        await _settle(pilot)
        assert _at_tail(log)
        assert not app.settings.autoscroll and _lock_glyph(app) == unlocked
        before = log.scroll_y
        await _stream(pilot, log, 5, "output after returning by hand")
        assert log.scroll_y == before, "reaching the bottom by hand re-locked the view"


@pytest.mark.asyncio
async def test_unlocking_by_the_button_cancels_a_follow_already_queued() -> None:
    app = _app()
    async with app.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_park_at_tail(app, pilot)
        await _wheel(pilot, log, events.MouseScrollUp, 6)
        await _settle(pilot)
        read_y = log.scroll_y

        held = []
        original_call_after_refresh = log.call_after_refresh
        log.call_after_refresh = lambda callback, *args, **kwargs: held.append(
            (callback, args, kwargs)
        )
        app._scroll_down()
        assert len(held) == 1
        log.call_after_refresh = original_call_after_refresh

        await pilot.click("#scroll-lock")
        await _settle(pilot)
        assert not app.settings.autoscroll

        callback, args, kwargs = held.pop()
        callback(*args, **kwargs)
        await _settle(pilot)
        assert log.scroll_y == read_y, "a stale follow yanked a reader who had just unlocked"


@pytest.mark.asyncio
async def test_locking_again_after_reading_history_catches_up_and_keeps_following() -> None:
    app = _app()
    async with app.run_test(size=(100, 24)) as pilot:
        log = await _fill_and_park_at_tail(app, pilot)
        await pilot.click("#scroll-lock")
        await _wheel(pilot, log, events.MouseScrollUp, 6)
        await _stream(pilot, log, 3, "read while unlocked")
        assert not _at_tail(log)

        await pilot.click("#scroll-lock")
        await _settle(pilot)
        assert app.settings.autoscroll
        assert _at_tail(log), "locking must catch the view up to the tail"
        await _stream(pilot, log, 3, "after relock")
        assert _at_tail(log)
