"""Main transcript relock after content shrinks below a previous follow anchor."""
from types import SimpleNamespace
from litetui.app import LiteTUI


def test_reader_at_new_bottom_relocks_after_content_shrink():
    app = SimpleNamespace(_follow_anchor=120)
    log = SimpleNamespace(scroll_y=75, max_scroll_y=75)
    assert LiteTUI._still_following(app, log)


def test_reader_above_new_bottom_is_not_yanked():
    app = SimpleNamespace(_follow_anchor=120)
    log = SimpleNamespace(scroll_y=40, max_scroll_y=75)
    assert not LiteTUI._still_following(app, log)


def test_growing_content_does_not_disable_following():
    app = SimpleNamespace(_follow_anchor=75)
    log = SimpleNamespace(scroll_y=75, max_scroll_y=120)
    assert LiteTUI._still_following(app, log)


def test_relocked_anchor_survives_next_content_growth():
    app = SimpleNamespace(_follow_anchor=120)
    log = SimpleNamespace(scroll_y=75, max_scroll_y=75)
    assert LiteTUI._still_following(app, log)
    log.max_scroll_y = 100
    assert LiteTUI._still_following(app, log)


import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Static


class ScrollHost(App):
    _still_following = LiteTUI._still_following
    _next_follow_generation = LiteTUI._next_follow_generation
    _scroll_down = LiteTUI._scroll_down
    def __init__(self):
        super().__init__()
        self.settings = SimpleNamespace(autoscroll=True)
        self._follow_anchor = None
        self._follow_generation = 0
    def compose(self):
        with VerticalScroll(id='chat-log'):
            yield Static('\n'.join(['row'] * 150), id='content')
    def _autocollapse_offscreen(self, log): pass


@pytest.mark.asyncio
async def test_real_viewport_relocks_after_shrink_and_follows_next_output():
    app = ScrollHost()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        log = app.query_one('#chat-log')
        log.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        app._follow_anchor = log.scroll_y
        old_anchor = app._follow_anchor
        app.query_one('#content', Static).update('\n'.join(['row'] * 75))
        await pilot.pause()
        log.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        assert log.max_scroll_y < old_anchor
        app._scroll_down()
        await pilot.pause()
        app.query_one('#content', Static).update('\n'.join(['row'] * 100))
        await pilot.pause()
        app._scroll_down()
        await pilot.pause()
        assert log.scroll_y >= log.max_scroll_y - 2
