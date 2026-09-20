"""An in-TUI browser: a URL bar + browser controls at the top, and a
rendered view of the page beneath.

Ryan, 2026-09-19: "we already have the sidebar in litetui. add that webview
theres with a url bar and browser controls at the top. i want a in tui
browser."

🔴 A TUI HAS NO WEBVIEW. There is no browser engine to embed, so "webview"
here means what the terminal can actually do: fetch a URL, reduce its HTML to
readable text plus a list of its links, and show both in the sidebar. The
controls are the real browser controls — Back, Forward, Reload, Home — over a
real history, and the links are real and followable, so it behaves like a
browser even though it paints no pixels.

🔴 NO NEW DEPENDENCIES. ``httpx`` is already a declared dependency; the HTML
pass uses only the stdlib ``html.parser``, because a full parser
(bs4/trafilatura/lxml) is not in the dependency set and pulling one in for a
text browser would be the wrong weight. What this means: the extraction is
heuristic. It is good enough to read a page and to give you its links; it is
NOT a faithful rendering, and it does not run the page's JavaScript. A page
that is all JS (a SPA with no server-rendered body) will come back thin — that
is an honest limit of the approach, not a bug to "fix" by adding a headless
browser.

📌 THE BODY WORKS IN EITHER HOST. It is a ``Widget`` with no ModalScreen
assumptions — the same contract as ``MCPListBody`` — so it opens in the
sidebar (the ask) or as a popup (the setting), and the ``SwapButton`` moves it
between them without losing what it has loaded.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Input, Static

from litetui.side_panel import SwapButton, close_dialog

#: Where "Home" goes and where an empty URL bar starts. Harmless and always up.
HOME_URL = "https://example.com"

#: Ceiling on what a page may hand us, so a hostile or enormous document cannot
#: dump megabytes into the terminal.
_MAX_BODY_CHARS = 60_000
_MAX_LINKS = 400
_MAX_RAW_CHARS = 200_000

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) LiteTUI/0.24 (in-tui browser; +readable-text)"
)

#: Tags whose text is never rendered. Balanced (open and close), so a depth
#: counter can ignore everything inside them, including a page's own CSS/JS.
_SKIP_TAGS = {
    "script", "style", "noscript", "template", "svg", "iframe",
    "object", "audio", "video", "canvas", "source",
}

#: A closing tag in this set pushes a newline: it is the seam between blocks.
_BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "ul", "ol",
    "section", "article", "header", "footer", "nav", "main", "table",
    "thead", "tbody", "blockquote", "pre", "dl", "dt", "dd", "figcaption",
    "summary", "details", "form", "fieldset", "address", "figure",
}

#: A closing cell becomes spacing, not a line break.
_CELL_TAGS = {"td", "th"}


class _Extractor(HTMLParser):
    """Fold an HTML document into ``{title, body, links}``.

    Deliberately forgiving: ``HTMLParser`` does not raise on malformed input the
    way a strict parser would, which is exactly the tolerance a browser needs
    when the input is the open web.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._buf: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0            # depth inside a non-rendered subtree
        self._in_title = False
        self._a_href: str | None = None
        self._a_buf: list[str] = []

    # ── text ─────────────────────────────────────────────────────────────
    def handle_data(self, data: str) -> None:
        if self._in_title:
            if not self.title:
                self.title = data
            return
        if self._skip:
            return
        if self._a_href is not None:
            self._a_buf.append(data)
        self._buf.append(data)

    # ── tags ─────────────────────────────────────────────────────────────
    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag == "br":
            self._buf.append("\n")
            return
        if self._skip:
            return
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._a_href = href
                self._a_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "a" and self._a_href is not None:
            self._finish_link(self._a_href)
            self._a_href = None
            return
        if tag in _BLOCK_TAGS:
            self._buf.append("\n")
        elif tag in _CELL_TAGS:
            self._buf.append("    ")

    def _finish_link(self, href: str) -> None:
        if len(self.links) >= _MAX_LINKS:
            return
        base = self.links_base  # set by extract_page
        url = urljoin(base, href.strip())
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return                       # javascript:, mailto:, #fragment, …
        if parts.netloc in ("", "#"):
            return
        label = _collapse(" ".join(self._a_buf))
        self.links.append((label or url, url))

    # set by extract_page before feed()
    links_base: str = ""

    def result(self) -> dict:
        body = _collapse_lines("".join(self._buf))
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS].rstrip() + "\n\n… (truncated)"
        return {"title": _collapse(self.title), "body": body, "links": self.links}


def extract_page(html: str, base_url: str = "") -> dict:
    """Parse ``html`` into ``{"title": str, "body": str, "links": [(label,url)]}``."""
    ex = _Extractor()
    ex.links_base = base_url
    try:
        ex.feed(html)
        ex.close()
    except Exception:
        # A parser that dies on one bad page would turn a broken URL into a
        # crash instead of a readable error. Return whatever it managed.
        pass
    return ex.result()


_WS = re.compile(r"[ \t\r\f\v]+")


def _collapse(s: str) -> str:
    """Run of spaces/tabs to one space; strip the ends."""
    return _WS.sub(" ", s).strip()


def _collapse_lines(s: str) -> str:
    """Line-oriented clean: collapse each line, drop empties, keep one blank
    between groups so paragraphs stay separated."""
    out: list[str] = []
    prev_blank = True
    for raw in s.split("\n"):
        line = _collapse(raw)
        if not line:
            if not prev_blank:
                out.append("")
            prev_blank = True
        else:
            out.append(line)
            prev_blank = False
    return "\n".join(out).strip("\n")


def _normalize(url: str) -> str:
    """Give a bare host a scheme so the fetch does not fail on it."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        return "https://" + url
    return url


class BrowserBody(Widget):
    """A browser that fits in a sidebar: URL bar + controls on top, page below.

    No ModalScreen assumptions — the same contract as every other dialog body,
    so it swaps between the sidebar and a popup carrying what it loaded.
    """

    DEFAULT_CSS = """
    BrowserBody { height: 100%; layout: vertical; }

    #bv-urlrow { height: auto; margin: 0 0 1 0; }
    #bv-urlrow Input { width: 100%; }

    #bv-nav, #bv-nav2 { height: auto; margin: 0 0 1 0; }
    #bv-nav Button, #bv-nav2 Button { min-width: 5; margin: 0 1 0 0; }

    #bv-status { height: auto; color: $text-muted; margin: 0 0 1 0;
                overflow: hidden; text-overflow: ellipsis; }

    #bv-scroll { height: 1fr; padding: 0 1; border: solid $foreground 15%; }
    #bv-title { text-style: bold; color: $accent; margin: 0 0 1 0; }
    .bv-sec { text-style: bold; margin: 1 0; }

    #bv-golink { height: auto; margin: 1 0 1 0; }
    #bv-golink Input { width: 1fr; margin: 0 1 0 0; }
    #bv-golink Button { min-width: 8; }

    #bv-close { height: auto; margin: 1 0 1 0; }
    #bv-close Button { width: 1fr; }
    """

    def __init__(self, url: str = "") -> None:
        super().__init__()
        self._cur = _normalize(url) or HOME_URL
        self._hist: list[str] = []
        self._idx = -1
        self._links: list[tuple[str, str]] = []
        # What the last successful load painted. Carried across a host swap so a
        # "Sidebar / popup" toggle shows the SAME page without re-fetching.
        self._rendered: tuple[str, str, list[tuple[str, str]]] | None = None
        self._scroll_y = 0

    # ── composition ──────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        # URL on its own full-width row, so it never fights the controls for
        # space in a narrow sidebar.
        with Horizontal(id="bv-urlrow"):
            yield Input(placeholder="https://…   (Enter to go)", id="bv-url")
        # Nav as a 2x2 grid: Back/Fwd then Reload/Home. Four buttons in one
        # row clips the last one at ~30 cols.
        with Horizontal(id="bv-nav"):
            yield Button("Back", id="bv-back")
            yield Button("Fwd", id="bv-fwd")
        with Horizontal(id="bv-nav2"):
            yield Button("Reload", id="bv-reload")
            yield Button("Home", id="bv-home")
        yield Static("", id="bv-status", markup=False)
        with VerticalScroll(id="bv-scroll"):
            yield Static("Type a URL above and press Enter, or pick a link number below.",
                         markup=False)
        with Horizontal(id="bv-golink"):
            yield Input(placeholder="link #", id="bv-link")
            yield Button("Follow", id="bv-follow")
        with Horizontal(id="bv-close"):
            yield Button("Close", variant="primary", id="bv-close")
        # Own full-width row (not .inline): the "Open as modal" label is wider
        # than a 30-col sidebar minus a Close button, so it gets the row to itself.
        yield SwapButton()

    def on_mount(self) -> None:
        try:
            self.query_one("#bv-url", Input).value = self._cur
            self.query_one("#bv-url", Input).focus()
        except Exception:
            pass
        self.app.run_worker(self._startup(), group="webview")

    # ── the swap contract ────────────────────────────────────────────────
    def get_state(self) -> dict:
        state: dict = {"url": self._cur}
        if self._rendered is not None:
            title, body, links = self._rendered
            state.update(title=title, body=body, links=links)
        try:
            state["scroll_y"] = self.query_one("#bv-scroll", VerticalScroll).scroll_offset.y
        except Exception:
            pass
        return state

    def set_state(self, state: dict) -> None:
        self._cur = _normalize(state.get("url") or self._cur)
        self._scroll_y = int(state.get("scroll_y") or 0)
        if state.get("title") is not None:
            self._rendered = (
                state.get("title") or "",
                state.get("body") or "",
                list(state.get("links") or []),
            )
            self._links = self._rendered[2]

    # ── startup / rendering ──────────────────────────────────────────────
    async def _startup(self) -> None:
        if self._rendered is not None:
            # Restored after a host swap: paint what we already have.
            await self._paint(*self._rendered)
            return
        await self._load(self._cur)

    async def _paint(self, title: str, body: str, links: list[tuple[str, str]]) -> None:
        self._rendered = (title, body, links)
        self._links = links
        try:
            scroll = self.query_one("#bv-scroll", VerticalScroll)
            await scroll.remove_children()
            children = []
            if title:
                children.append(Static(title, id="bv-title", markup=False))
            children.append(Static(body or "(no text on this page)", markup=False))
            children.append(Static("", markup=False))
            if links:
                children.append(Static(f"Links ({len(links)})", classes="bv-sec", markup=False))
                numbered = "\n".join(
                    f"[{i}] {label}   {url}"
                    for i, (label, url) in enumerate(links[:200], 1)
                )
                children.append(Static(numbered, markup=False))
            else:
                children.append(Static("No links found on this page.", classes="bv-sec",
                                       markup=False))
            await scroll.mount(*children)
            if self._scroll_y:
                try:
                    scroll.scroll_to(y=self._scroll_y, animate=False)
                except Exception:
                    pass
        except Exception:
            pass

    def _say(self, text: str) -> None:
        try:
            self.query_one("#bv-status", Static).update(text)
        except Exception:
            pass

    # ── fetching ─────────────────────────────────────────────────────────
    async def _load(self, url: str, record: bool = True) -> None:
        url = _normalize(url)
        if not url:
            self._say("nothing to load — type a URL first")
            return
        self._cur = url
        self._say(f"→ {url}")
        import httpx  # local: keep module import light even when unused

        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=25.0,
                headers={"User-Agent": _UA},
            ) as client:
                resp = await client.get(url)
        except httpx.TimeoutException:
            self._say(f"timeout: {url}")
            return
        except Exception as e:  # noqa: BLE001 — the open web raises many things
            self._say(f"error: {type(e).__name__}: {e}")
            return

        final_url = str(resp.url)
        ctype = resp.headers.get("content-type", "").lower()
        raw = resp.text

        if "html" in ctype or raw.lstrip()[:1] == "<":
            page = extract_page(raw, final_url)
            body, title, links = page["body"], page["title"], page["links"]
            kind = "html"
        elif "text/" in ctype or "json" in ctype or "xml" in ctype:
            body = raw[:_MAX_RAW_CHARS]
            title = page_title_from(final_url)
            links = []
            kind = ctype.split(";")[0].strip() or "text"
        else:
            body = (
                f"(binary content: {kind_or_ctype(ctype)} — {len(resp.content)} bytes)\n"
                f"This is a text browser, so binary types are shown, not rendered."
            )
            title = page_title_from(final_url)
            links = []
            kind = "binary"

        if record:
            self._remember(final_url)
        self._cur = final_url
        await self._paint(title, body, links)
        self._say(
            f"{resp.status_code} · {kind} · {len(resp.content)} bytes · {len(links)} link(s)"
            + (f" · {final_url}" if final_url != url else "")
        )

    def _remember(self, url: str) -> None:
        """Truncate the forward tail and append a fresh entry."""
        self._hist = self._hist[: self._idx + 1] + [url]
        self._idx = len(self._hist) - 1

    # ── navigation ───────────────────────────────────────────────────────
    async def _goto(self, url: str) -> None:
        url = _normalize(url)
        if url:
            self.query_one("#bv-url", Input).value = url
            await self._load(url)

    async def _back(self) -> None:
        if self._idx <= 0:
            self._say("no earlier page")
            return
        self._idx -= 1
        url = self._hist[self._idx]
        self.query_one("#bv-url", Input).value = url
        await self._load(url, record=False)

    async def _forward(self) -> None:
        if self._idx >= len(self._hist) - 1:
            self._say("no later page")
            return
        self._idx += 1
        url = self._hist[self._idx]
        self.query_one("#bv-url", Input).value = url
        await self._load(url, record=False)

    def _follow_number(self, raw: str) -> bool:
        """If ``raw`` is a link number, follow it. True if it was handled."""
        raw = (raw or "").strip()
        if not raw.isdigit():
            return False
        n = int(raw)
        if 1 <= n <= len(self._links):
            self.query_one("#bv-url", Input).value = self._links[n - 1][1]
            self.app.run_worker(self._load(self._links[n - 1][1]), group="webview")
            return True
        return False

    # ── events ───────────────────────────────────────────────────────────
    @on(Input.Submitted, "#bv-url")
    async def _url_submitted(self) -> None:
        val = self.query_one("#bv-url", Input).value
        if self._follow_number(val):
            return
        await self._goto(val)

    @on(Input.Submitted, "#bv-link")
    def _link_submitted(self) -> None:
        val = self.query_one("#bv-link", Input).value
        if self._follow_number(val):
            self.query_one("#bv-link", Input).value = ""

    @on(Button.Pressed, "#bv-back")
    async def _on_back(self) -> None:
        await self._back()

    @on(Button.Pressed, "#bv-fwd")
    async def _on_forward(self) -> None:
        await self._forward()

    @on(Button.Pressed, "#bv-reload")
    async def _on_reload(self) -> None:
        await self._load(self._cur, record=False)

    @on(Button.Pressed, "#bv-home")
    async def _on_home(self) -> None:
        self._idx = -1
        await self._goto(HOME_URL)

    @on(Button.Pressed, "#bv-follow")
    def _on_follow(self) -> None:
        val = self.query_one("#bv-link", Input).value
        if not self._follow_number(val):
            self._say(f"enter a link number 1–{len(self._links)}, or paste a URL above")

    @on(Button.Pressed, "#bv-close")
    def _on_close(self) -> None:
        close_dialog(self, None)


def page_title_from(url: str) -> str:
    """A title for a non-HTML page: the host, since there is no <title>."""
    try:
        return urlsplit(url).netloc or url
    except Exception:
        return url


def kind_or_ctype(ctype: str) -> str:
    return ctype.split(";")[0].strip() or "unknown type"
