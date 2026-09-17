"""Render the collapsible assistant card to SVG, for visual review.

Automated tests prove the state machine. They do not prove the card LOOKS
collapsed. This drives the real app through a pilot and exports what Textual
actually paints, so the fold can be reviewed without taking anyone's word for it.

    python scripts/card_collapse_screenshots.py

Writes artifacts/card-collapse/*.svg and prints the text extracted from each
render, so the same evidence is checkable in a terminal.

The four shots are chosen to show the whole contract, including the case that is
easy to mistake for a bug: a card straddling the top edge is STILL RENDERED, so
it stays open. Ryan's wording is "collapse when no longer rendered".
"""
from __future__ import annotations

import asyncio
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litetui import app as app_mod            # noqa: E402
from litetui.widgets import AssistantMessage  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "artifacts" / "card-collapse"
_TEXT = re.compile(r"<text[^>]*>(.*?)</text>", re.S)
_TAG = re.compile(r"<[^>]+>")
NL = chr(10)

ANSWER = (
    "Rewrote `captions.py` to fetch the timedtext URL directly instead of "
    "retrying the metadata call." + NL + NL
    + "- primary: metadata (-J) then GET the subtitle URL with urllib" + NL
    + "- fallback: the existing --write path" + NL + NL
    + "That removes the bot-gated retry loop entirely."
)


def rendered_text(svg: str) -> str:
    """The glyphs Textual actually painted, in order.

    Textual writes runs of spaces as &#160; (non-breaking) so the SVG keeps its
    column alignment. Unescaping and folding those back to ordinary spaces is
    what makes the result searchable -- without it every substring check fails
    against a render that is visibly correct, which cost one confused pass here.
    """
    parts = [_TAG.sub("", t) for t in _TEXT.findall(svg)]
    joined = html.unescape("".join(parts)).replace(chr(160), " ")
    return NL.join(p for p in joined.splitlines() if p.strip())


def filler(tag: str, n: int) -> str:
    return NL.join(f"{tag} line {i}" for i in range(n))


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    app = app_mod.LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None

    shots: list[tuple[str, str]] = []
    async with app.run_test(size=(100, 26)) as pilot:
        log = app.query_one("#chat-log")

        async def card(model: str, text: str) -> AssistantMessage:
            c = AssistantMessage()
            c.set_model_name(model)
            log.mount(c)
            await pilot.pause()
            c.set_answer(text)
            c.settled = True
            await pilot.pause()
            return c

        async def scroll() -> None:
            app._scroll_down(reader_acted=True)
            await pilot.pause()
            await pilot.pause()

        first = await card("qwen3-30b-a3b", ANSWER)
        shots.append(("01-expanded-model-name", app.export_screenshot()))

        first.set_summary("Rewrote captions fetch to use direct URL")
        await pilot.pause()
        shots.append(("02-expanded-with-summary", app.export_screenshot()))

        await card("qwen3-30b-a3b", filler("second card", 12))
        await scroll()
        shots.append(("03-partially-visible-stays-open", app.export_screenshot()))

        await card("qwen3-30b-a3b", filler("third card", 40))
        await scroll()
        shots.append(("04-scrolled-off-autocollapsed", app.export_screenshot()))
        folded_state = first.collapsed

        # Scroll back to where the first card lives. This is the view Ryan
        # described: the reader returns and finds a one-line header, which the
        # latch leaves folded until they open it themselves.
        log.scroll_to(y=0, animate=False, immediate=True)
        await pilot.pause()
        await pilot.pause()
        shots.append(("05-scrolled-back-shows-folded-header", app.export_screenshot()))
        still_folded = first.collapsed

    for name, svg in shots:
        (OUT / f"{name}.svg").write_text(svg, encoding="utf-8")

    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        if not cond:
            print("FAIL:", msg)
            ok = False

    expanded = rendered_text(shots[1][1])
    print("=== 02 expanded, summarised ===")
    print(NL.join(l for l in expanded.splitlines() if "Rewrote" in l or "primary" in l))
    check("Rewrote captions fetch to use direct URL" in expanded,
          "summary missing from the expanded header")
    check("qwen3-30b-a3b" in expanded, "model name missing from the header")

    partial = rendered_text(shots[2][1])
    check("bot-gated retry loop" in partial,
          "a partially visible card must still paint its body")

    check(folded_state, "card scrolled off the top did not auto-collapse")
    check(still_folded, "the latch must leave it folded when the reader scrolls back")

    back = rendered_text(shots[4][1])
    print(NL + "=== 05 scrolled back: one-line folded header ===")
    print(NL.join(l for l in back.splitlines() if "Rewrote" in l or "second card line 0" in l))
    check("Rewrote captions fetch to use direct URL" in back,
          "folded card does not show its summary in the header")
    check("bot-gated retry loop" not in back,
          "folded card is still painting its body")

    print(NL + "VISUAL CHECK:", "PASS" if ok else "FAIL")
    print("SVGs:", OUT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
