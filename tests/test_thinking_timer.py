"""THINKING TIMER — pure header logic + the stop control, no live app or model.

While the reasoning trace streams, the thinking block's header shows elapsed +
tok/s, exactly like the tool and answer bubbles. The string is pure
(thinking_header_text / tps_text), the tok/s number is the app's own `tps`
reactive (one number for the fact — the footer renders the same tps_text),
and the elapsed part is render_progress (no ETA: a trace has no token count
until it ends, and a confidently wrong number is worse than none).

The "timer stops at the right moment" control is on _thinking_done: after it,
_thinking_live is None and the _elapsed_repaint gate (that very attribute) can
never repaint the header again — asserted twice, for idempotency.
"""
import time

from litetui.app import (
    LiteTUI,
    ThinkingBlock,
    render_progress,
    thinking_header_text,
    tps_text,
)

MARKER_OPEN = "\u25be"
MARKER_COLLAPSED = "\u25b8"


# --- tps_text: one format for every surface --------------------------------
def test_tps_text_format():
    assert tps_text(24.13) == "24.1 tok/s"
    assert tps_text(0.4) == "0.4 tok/s"
    assert tps_text(123.96) == "124.0 tok/s"


# --- thinking_header_text: the string --------------------------------------
def test_header_with_tps():
    out = thinking_header_text(MARKER_OPEN, 0.0, 12.3, 24.1)
    expected = (f"{MARKER_OPEN} Thinking \u00b7 "
                f"{render_progress(0.0, 12.3)} \u00b7 24.1 tok/s")
    assert out == expected, (out, expected)


def test_header_without_tps_degrades_to_elapsed_only():
    """The required control: no tps -> no tok/s field at all, and never
    a rendered "0.0 tok/s" (a lie about a number that does not exist). A
    genuine 0.0s ELAPSED is legitimate - the answer bubble shows the same
    at turn start - so the assertion is on the field, not the digit."""
    out = thinking_header_text(MARKER_OPEN, 0.0, 3.0, None)
    assert "tok/s" not in out, out
    assert "0.0 tok/s" not in out, out
    assert "3.0s" in out and "\u2026" in out, out


def test_header_collapsed_marker_kept():
    out = thinking_header_text(MARKER_COLLAPSED, 0.0, 5.0, 3.0)
    assert out.startswith(f"{MARKER_COLLAPSED} Thinking"), out


def test_header_has_no_eta():
    """Structurally there is no rate/token input at all, so no ETA can
    appear — assert it anyway, because the absence is the contract."""
    out = thinking_header_text(MARKER_OPEN, 0.0, 9.0, 10.0)
    assert "est" not in out, out


def test_header_pure_and_grows():
    a = thinking_header_text(MARKER_OPEN, 100.0, 101.2, 20.0)
    b = thinking_header_text(MARKER_OPEN, 200.0, 201.2, 20.0)
    assert a == b, (a, b)
    assert "1.0s" in thinking_header_text(MARKER_OPEN, 0.0, 1.0, None)
    assert "5.0s" in thinking_header_text(MARKER_OPEN, 0.0, 5.0, None)


# --- the widget side, on bare __new__ doubles (no app) ----------------------
class _FakeHeader:
    def __init__(self):
        self.content = ""


def _bare_block():
    """Simulates a CONSTRUCTED block: __init__ stamps t0 (a block is
    only ever built on the first reasoning token), so the double starts
    with a live timer."""
    b = ThinkingBlock.__new__(ThinkingBlock)
    b._t0 = time.monotonic()
    b._marker = MARKER_OPEN
    return b


def test_repaint_header_renders_elapsed_and_tps():
    b = _bare_block()
    hdr = _FakeHeader()
    b.query_one = lambda w: hdr
    b.repaint_header(20.5)
    assert hdr.content.startswith(f"{MARKER_OPEN} Thinking \u00b7 0."), hdr.content
    assert "20.5 tok/s" in hdr.content, hdr.content


def test_repaint_header_with_unstamped_block_is_a_noop():
    b = _bare_block()
    b._t0 = None  # the guard: a block that never stamped t0 renders nothing
    hdr = _FakeHeader()
    b.query_one = lambda w: hdr
    b.repaint_header(20.5)
    assert hdr.content == "", "no t0 - nothing rendered"


def test_repaint_header_degrades_without_tps():
    b = _bare_block()
    hdr = _FakeHeader()
    b.query_one = lambda w: hdr
    b.repaint_header(None)
    assert "tok/s" not in hdr.content, hdr.content
    # the elapsed part may genuinely be 0.0s (it just started) - what is
    # forbidden is a fabricated rate, i.e. the field itself
    assert "0.0 tok/s" not in hdr.content, hdr.content


# --- the stop control ---------------------------------------------------------
class _FakeThinking:
    def __init__(self):
        self.finalized = False
        self.header_reset = False

    def finalize(self):
        self.finalized = True

    def reset_header(self):
        self.header_reset = True


class _FakeApp:
    _thinking_done = LiteTUI._thinking_done  # the real method, borrowed

    def __init__(self, live):
        self._thinking_live = live


def test_thinking_done_stops_the_timer():
    """The required control: after _thinking_done the repaint gate is False —
    with _thinking_live None, _elapsed_repaint can never repaint the header
    again, so the timer stops at the right moment instead of counting
    forever."""
    t = _FakeThinking()
    a = _FakeApp(t)
    assert a._thinking_live is t
    a._thinking_done()
    assert a._thinking_live is None, "the gate must be off"
    assert (a._thinking_live is not None) is False
    assert t.finalized and t.header_reset


def test_thinking_done_is_idempotent():
    """Every exit path (content, tool call, turn end, both error paths) calls
    it; the second call must be a clean no-op."""
    t = _FakeThinking()
    a = _FakeApp(t)
    a._thinking_done()
    a._thinking_done()
    assert a._thinking_live is None
    assert t.finalized and t.header_reset


def test_thinking_done_without_a_live_block_is_a_noop():
    a = _FakeApp(None)
    a._thinking_done()  # must not raise
    assert a._thinking_live is None
