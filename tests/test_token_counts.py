"""Token counts beside tok/s. T079.

Ryan: "one more thing we need next to toks is total tokens thinking that turn
or output in the case of the response please, even if we calc it ourself" —
he is looking at LM Studio's own "17 GEN 2,775 tok" readout and wants the
equivalent on our two surfaces:

    thinking header  ->  tokens in the REASONING trace this turn
    footer           ->  tokens in the OUTPUT

🔴 THE CONTROL THAT MATTERS IS `test_the_two_surfaces_show_DIFFERENT_halves`.
`TpsState.n` already existed and is the obvious thing to render. Wiring BOTH
surfaces to it would look completely correct — two numbers, both plausible,
both updating — and every other test in this file would still pass. The whole
feature is that they are different halves of the same count.

⚠️ ONE COUNTING SITE, PARTITIONED — never two tallies. `reasoning + content ==
n` always holds, because a second counter of the same deltas is the class this
codebase has been bitten by repeatedly, and here it would produce a header and
a footer that disagree about a turn nobody could then reconcile.

📌 It is OUR delta count, not `usage.completion_tokens`. The server reports one
figure covering reasoning AND output together, so it cannot answer either
question on its own. Ryan sanctioned the approximation: "even if we calc it
ourself".
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from rich.text import Text

from litetui import appsvc
from litetui.textfmt import thinking_header_text, token_count_text
from litetui.turnstats import TpsState


def _turn(reasoning: int, content: int) -> TpsState:
    """A turn with a known split. The first tick only starts the clock."""
    s = TpsState()
    s.tick()
    for _ in range(reasoning):
        s.tick(reasoning=True)
    for _ in range(content):
        s.tick()
    return s


# ── the partition ──────────────────────────────────────────────────────────

def test_the_split_always_sums_to_the_total():
    s = _turn(5, 3)
    assert (s.reasoning, s.content, s.n) == (5, 3, 8)
    assert s.reasoning + s.content == s.n


def test_the_split_survives_interleaving():
    """Real turns alternate: think, emit, think again."""
    s = TpsState()
    s.tick()
    for kind in (True, False, True, True, False, False, True):
        s.tick(reasoning=kind)
    assert s.reasoning == 4 and s.content == 3
    assert s.reasoning + s.content == s.n


def test_a_new_turn_forgets_both_halves():
    s = _turn(9, 4)
    s.start()
    assert (s.n, s.reasoning, s.content) == (0, 0, 0)


def test_content_is_the_default_so_existing_callers_still_count():
    """`tick()` with no argument is the content branch — the signature is
    additive, so the call site that streams prose did not have to change."""
    s = TpsState()
    s.tick()
    s.tick()
    assert s.content == 1 and s.reasoning == 0


# ── the format ─────────────────────────────────────────────────────────────

def test_the_count_is_comma_grouped_like_the_readout_it_sits_beside():
    """LM Studio prints "2,775 tok"; a bare 2775 beside it reads as a
    different quantity."""
    assert token_count_text(2775) == "2,775 tok"
    assert token_count_text(7) == "7 tok"
    assert token_count_text(1234567) == "1,234,567 tok"


# ── the thinking header ────────────────────────────────────────────────────

def test_the_header_shows_the_count_between_elapsed_and_rate():
    out = thinking_header_text("v", 0.0, 12.3, 24.1, 2775)
    assert "2,775 tok" in out and "24.1 tok/s" in out
    assert out.index("2,775 tok") < out.index("24.1 tok/s"), (
        "quantity should read before speed"
    )


def test_the_header_renders_absence_as_absence():
    """Same rule tps_text already follows: a rendered 0 is a claim about a
    number that has not been produced."""
    for absent in (0, None):
        out = thinking_header_text("v", 0.0, 12.3, 24.1, absent)
        assert "tok/s" in out, "CONTROL: the rate is still there"
        assert "0 tok" not in out and " tok " not in out.replace(" tok/s", "")


def test_the_header_still_works_for_callers_that_pass_no_count():
    """Additive parameter: every existing caller and double is untouched."""
    assert thinking_header_text("v", 0.0, 12.3, 24.1) == thinking_header_text(
        "v", 0.0, 12.3, 24.1, None
    )


# ── the footer ─────────────────────────────────────────────────────────────

def _footer(tps, stats):
    t = Text()
    appsvc.append_tps_into(SimpleNamespace(tps=tps, _tps=stats), t, " · ")
    return t.plain


def test_the_footer_shows_the_output_count_beside_the_rate():
    out = _footer(24.1, _turn(100, 2775))
    assert "2,775 tok" in out and "24.1 tok/s" in out


def test_the_footer_renders_a_zero_output_as_absence():
    """A turn that produced only a tool call generated no prose. Painting 0
    claims a measured nothing where there is simply nothing to say."""
    out = _footer(24.1, _turn(40, 0))
    assert "tok/s" in out, "CONTROL: the rate is still shown"
    assert "0 tok" not in out


def test_the_footer_says_nothing_at_all_before_the_first_rate():
    assert _footer(None, _turn(5, 5)) == ""


def test_the_footer_survives_a_host_with_no_stats_yet():
    """Boot, or any double that never made a TpsState. It must render the rate
    rather than raising — this runs on the footer's repaint path."""
    t = Text()
    appsvc.append_tps_into(SimpleNamespace(tps=12.0, _tps=None), t, " · ")
    assert "12.0 tok/s" in t.plain


# ── THE CONTROL ────────────────────────────────────────────────────────────

def test_the_two_surfaces_show_DIFFERENT_halves():
    """Wiring both surfaces to `n` would look entirely correct — two plausible
    numbers, both updating — and every other test here would still pass.

    A turn that thought a lot and said little must show a LARGE number while
    thinking and a SMALL one in the footer.
    """
    s = _turn(2775, 42)

    header = thinking_header_text("v", 0.0, 12.3, 24.1, s.reasoning)
    footer = _footer(24.1, s)

    assert "2,775 tok" in header, "the header is not showing the reasoning half"
    assert "42 tok" in footer, "the footer is not showing the output half"
    assert "2,775 tok" not in footer, "the footer is showing the TOTAL, not output"
    assert "42 tok" not in header, "the header is showing output, not reasoning"
    # and neither is the combined figure
    assert token_count_text(s.n) not in header
    assert token_count_text(s.n) not in footer


# ── THE WIRING, which the unit tests above cannot see ──────────────────────

class _Delta:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        self.reasoning_content = reasoning
        self.reasoning = None
        self.tool_calls = None


class _Chunk:
    def __init__(self, **kw):
        self.choices = [type("C", (), {"delta": _Delta(**kw)})()]
        self.usage = None


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_THE_APP_actually_tags_its_reasoning_deltas():
    """🔴 THE ONE THAT CAUGHT A GREEN-THAT-CANNOT-RUN.

    Every test above drives `tick(reasoning=True)` DIRECTLY, so they all passed
    while the app called plain `tick()` on both branches — the split was always
    0/everything in production and the thinking header would have rendered no
    count at all. The mechanism was right and nothing was wired to it.

    This drives the real `_stream` with a real reasoning chunk instead.
    """
    import tempfile
    from pathlib import Path

    from litetui import app as app_mod
    from litetui import paths
    from litetui.settings import Settings

    paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-tokcount-"))
    a = app_mod.LiteTUI()
    # show_thinking=False DELIBERATELY, and this is a SCOPE LIMIT not a
    # convenience: with it on, this minimal harness stops consuming the stream
    # after the first reasoning delta — no exception, no worker error, just a
    # short read. It is NOT a product fault (thinking traces stream fine in the
    # real app, and the whole thinking-header repaint loop depends on it), and
    # nothing in the suite drives `_stream` with reasoning AND show_thinking on,
    # so there is no working harness to copy. Reported as discovered work.
    #
    # ⚠️ WHAT THIS COSTS: the tagging is covered, the ThinkingBlock mount path
    # is not. The tag is applied BEFORE the mount, so this still catches the
    # regression it was written for — the app calling plain `tick()`.
    a.settings = Settings(autocompact_enabled=False, wake_after_compact=False,
                          clear_screen_after_compact=False, show_thinking=False)
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None

    chunks = [_Chunk(reasoning="think "), _Chunk(reasoning="more "),
              _Chunk(reasoning="still "), _Chunk(content="a"), _Chunk(content="b")]

    async def create(**kw):
        return _Stream(chunks)

    async with a.run_test(size=(120, 40)) as pilot:
        a.client.chat.completions.create = create
        a._append({"role": "user", "content": "hi"})
        a._stream()
        for _ in range(120):
            await pilot.pause()
            if not a._chat_running():
                break

    # the FIRST delta only starts the clock, so 3 reasoning deltas -> 2 counted
    assert a._tps.reasoning == 2, (
        f"the app is not tagging reasoning deltas: reasoning={a._tps.reasoning}, "
        f"content={a._tps.content}"
    )
    assert a._tps.content == 2, f"content={a._tps.content}"
    assert a._tps.reasoning + a._tps.content == a._tps.n
