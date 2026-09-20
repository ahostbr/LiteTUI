"""The `/browser` in-TUI browser — URL bar + controls over a page's text and links.

Two layers are pinned here.

1. The pure ``extract_page`` / ``_normalize`` logic — the part a TUI actually
   depends on to turn an HTML document into something readable. A page that
   leaks its <script>/<style> into the body, or resolves a relative href
   against nothing, would read as noise; these fail on the fixture, not on the
   first real URL.

2. The body reaching a real fetch and painting it — the "buttons reach the
   manager, not themselves" doctrine. The load is followed all the way to
   ``body._links`` and ``body._hist`` (what navigation actually did), and the
   httpx client is stubbed so the test never dials out.
"""
from __future__ import annotations

import httpx
import pytest

from litetui.app import LiteTUI
from litetui.side_panel import (
    SidePanel,
    _ModalHost,
    request_swap,
    show_dialog,
)
from litetui.webview import HOME_URL, BrowserBody, _normalize, extract_page
from textual.widgets import Input

try:
    from _settle import settle_until
except ModuleNotFoundError:  # pragma: no cover - depends on pytest rootdir
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _settle import settle_until


# ── the pure layer ────────────────────────────────────────────────────────
_HTML = """
<html>
  <head>
    <title>  Example  Domain </title>
    <style>body{color:red} .x{margin:0}</style>
    <script>var secret = "SHOULD NOT APPEAR";</script>
  </head>
  <body>
    <h1>Hello &amp; welcome</h1>
    <p>This is the   body text<br>across two lines.</p>
    <a href="/about">About us</a>
    <a href="https://site.test/x">External</a>
    <a href="mailto:x@y.z">Mail</a>
    <a href="javascript:void(0)">JS</a>
    <p>tail</p>
  </body>
</html>
"""


def test_extract_page_reads_title_body_and_links():
    page = extract_page(_HTML, "https://example.com/")
    assert page["title"] == "Example Domain"
    assert "SHOULD NOT APPEAR" not in page["body"], "script/style must not leak into the body"
    assert "Hello & welcome" in page["body"]
    assert "body text" in page["body"] and "tail" in page["body"]
    urls = [u for _, u in page["links"]]
    # relative resolved against the base, absolute kept, mailto/js dropped
    assert "https://example.com/about" in urls
    assert "https://site.test/x" in urls
    assert not any(u.startswith(("mailto:", "javascript:")) for u in urls)
    labels = {u: l for l, u in page["links"]}
    assert labels["https://example.com/about"] == "About us"


def test_extract_page_is_forgiving_of_bad_input():
    # A parser that raised on one bad page would turn a broken URL into a crash.
    page = extract_page("<p>ok</p><div><p>more</div>garbage</html>")
    assert "ok" in page["body"] and "more" in page["body"]


def test_normalize_adds_a_scheme_and_leaves_urls_alone():
    assert _normalize("example.com") == "https://example.com"
    assert _normalize("  ") == ""
    assert _normalize("http://a/b") == "http://a/b"
    assert _normalize("www.x.test") == "https://www.x.test"


# ── the body, reached through a real (stubbed) fetch ─────────────────────
_PAGE_A = "<title>Page A</title><body><p>alpha</p><a href='https://site.test/b'>to B</a></body>"
_PAGE_B = "<title>Page B</title><body><p>beta</p></body>"


class _FakeResp:
    def __init__(self, url, html):
        self.url = url
        self.status_code = 200
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self._html = html

    @property
    def text(self):
        return self._html

    @property
    def content(self):
        return self._html.encode("utf-8")


class _FakeClient:
    def __init__(self, *a, **k):
        self.pages = {
            HOME_URL: _PAGE_A,
            "https://site.test/b": _PAGE_B,
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return _FakeResp(str(url), self.pages.get(str(url), "<title>? </title><body>?</body>"))


@pytest.fixture
def fake_httpx(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


def _make_app():
    app = LiteTUI()
    app._connect = lambda: None
    return app


async def _open(app, pilot, url=""):
    app.run_worker(show_dialog(app, lambda: BrowserBody(url)), group="webview")
    for _ in range(30):
        await pilot.pause()
        if app.screen.query("BrowserBody"):
            break
    assert app.screen.query("BrowserBody"), "the browser never mounted"
    return app.screen.query_one(BrowserBody)


async def _settle(pilot, n: int = 20):
    for _ in range(n):
        await pilot.pause()


@pytest.mark.asyncio
async def test_browser_opens_and_paints_a_page(monkeypatch, fake_httpx):
    app = _make_app()
    async with app.run_test(size=(100, 40)) as pilot:
        body = await _open(app, pilot)
        await _settle(pilot)
        # the initial load is HOME_URL -> Page A
        assert body._cur == HOME_URL
        assert body._hist == [HOME_URL]
        assert [u for _, u in body._links] == ["https://site.test/b"]
        assert "Page A" in str(body.query_one("#bv-title").render())


@pytest.mark.asyncio
async def test_browser_follows_a_link_and_back_rewinds(monkeypatch, fake_httpx):
    app = _make_app()
    async with app.run_test(size=(100, 40)) as pilot:
        body = await _open(app, pilot)
        await _settle(pilot)

        # follow link [1] (https://site.test/b) through the number input
        body.query_one("#bv-link", Input).value = "1"
        assert body._follow_number("1")
        await _settle(pilot)
        assert body._cur == "https://site.test/b"
        assert body._hist == [HOME_URL, "https://site.test/b"]
        assert "Page B" in str(body.query_one("#bv-title").render())

        # back rewinds without a fresh fetch entry
        await body._back()
        await _settle(pilot)
        assert body._idx == 0
        assert body._cur == HOME_URL
        assert "Page A" in str(body.query_one("#bv-title").render())


@pytest.mark.asyncio
async def test_browser_command_is_registered():
    app = _make_app()
    async with app.run_test(size=(100, 40)):
        assert "/browser" in app.plugins.commands
        assert "/web" in app.plugins.commands


@pytest.mark.asyncio
async def test_browser_carry_state_survives_a_swap(monkeypatch, fake_httpx):
    """get_state/set_state carry the loaded page so a host swap does not
    re-fetch or lose it."""
    app = _make_app()
    async with app.run_test(size=(100, 40)) as pilot:
        body = await _open(app, pilot)
        await _settle(pilot)
        state = body.get_state()

    fresh = BrowserBody()
    fresh.set_state(state)
    assert fresh._rendered is not None
    title, _, links = fresh._rendered
    assert title == "Page A"
    assert [u for _, u in links] == ["https://site.test/b"]


@pytest.mark.asyncio
async def test_ctrl_b_docks_to_the_sidebar_even_when_the_setting_says_modal(fake_httpx):
    """Ryan's ask, pinned: the browser ALWAYS docks to the sidebar.

    With `dialog_style="modal"` (the default) a dialog that follows the setting
    would open as a modal. `action_open_browser` forces `style="sidebar"`, so it
    must still mount a SidePanel — and the SwapButton must pop it back out to a
    modal. Drives the real ctrl+b entry point, not a hand-built controller.
    """
    app = _make_app()
    app.settings.dialog_style = "modal"
    async with app.run_test(size=(120, 34)) as pilot:
        # let the app settle so its startup focus doesn't race the panel
        for _ in range(10):
            await pilot.pause()
            if app.screen.focused is not None:
                break
        app.action_open_browser()
        await settle_until(pilot, lambda: bool(app.screen.query(SidePanel)))
        assert app.screen.query(SidePanel), (
            "ctrl+b opened a modal despite the forced sidebar — the browser "
            "must dock to the side panel"
        )
        assert not app.screen.query(_ModalHost), "forced sidebar also pushed a modal"

        # the Swap button pops it out of the sidebar again. In modal mode the
        # _ModalHost is PUSHED as a screen (it IS app.screen), whereas the
        # sidebar is MOUNTED into the main screen — so check isinstance, not a
        # descendant query.
        body = app.screen.query_one(BrowserBody)
        request_swap(body)
        # wait for the positive arrival: the modal host becoming the active
        # screen (the SidePanel is torn down just before it is pushed).
        await settle_until(pilot, lambda: isinstance(app.screen, _ModalHost))
        assert not app.screen.query(SidePanel), (
            "the swap did not move the browser out of the sidebar"
        )
        assert isinstance(app.screen, _ModalHost), (
            f"the swap did not land on a modal host, got {type(app.screen).__name__}"
        )
