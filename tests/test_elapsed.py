"""COMMIT 1 (ELAPSED) — pure display logic, testable without a Textual app.

`fmt_dur`, `render_progress`, and `tool_display_parts` are all pure (no app,
no model, no Textual console), so they are tested directly. ToolMessage's
rendering (widget.content) needs a live app, so the display logic was extracted
into tool_display_parts(tool, ...) which takes a lightweight object with the
same state fields — see _FakeTool below.
"""
import time

from litetui.app import ToolMessage, render_progress, tool_display_parts
from litetui.fmt import fmt_dur


class _FakeTool:
    """Stands in for ToolMessage's state without instantiating a Textual widget."""

    def __init__(self, name="bash", args="", result=None, ok=True, t0=0.0, took=None):
        self.tool_name = name
        self._args = args
        self._result = result
        self._ok = ok
        self._t0 = t0
        self._took = took


def _text(parts) -> str:
    return "".join(p[0] for p in parts)


# --- fmt_dur ---------------------------------------------------------------
def testfmt_dur_short():
    assert fmt_dur(1.2) == "1.2s"
    assert fmt_dur(0) == "0.0s"
    assert fmt_dur(59.9) == "59.9s"


def testfmt_dur_minutes():
    out = fmt_dur(65.0)
    assert out.startswith("1m "), out
    assert "5.0s" in out, out


def testfmt_dur_negative_clamps():
    assert fmt_dur(-3) == "0.0s"


# --- render_progress --------------------------------------------------------
def test_render_progress_is_pure_and_elapsed():
    # Same elapsed (1.2s) regardless of the absolute clock -> pure.
    a = render_progress(100.0, 101.2)
    b = render_progress(200.0, 201.2)
    assert a == b, (a, b)
    assert "1.2s" in a, a
    assert "…" in a, a  # in-progress marker


def test_render_progress_grows():
    assert "0.5s" in render_progress(0, 0.5)
    assert "5.0s" in render_progress(0, 5.0)


# --- tool_display_parts -----------------------------------------------------
def test_running_shows_elapsed():
    parts = tool_display_parts(_FakeTool(t0=100.0))
    last = parts[-1][0]
    assert "…" in last, last  # in-progress marker
    assert "s" in last, last  # "X.Xs"


def test_done_shows_took_not_elapsed():
    parts = tool_display_parts(_FakeTool(result="done", took=2.5))
    last = parts[-1][0]
    assert "⏱" in last, last
    assert "2.5s" in last, last
    assert "…" not in last, last  # no longer running


def test_args_and_result_rendered():
    parts = tool_display_parts(
        _FakeTool(name="bash", args='{"cmd": "ls -la"}', result="all files", took=0.1)
    )
    text = _text(parts)
    assert "bash" in text, text
    assert "ls -la" in text, text
    assert "all files" in text, text


def test_result_truncation_notes_overflow():
    big = "line\n" * 50
    parts = tool_display_parts(_FakeTool(result=big, took=0.5), max_lines=12)
    assert "more lines" in _text(parts)


# --- set_result (the 'took X.Xs' settle) ------------------------------------
def test_set_result_settles_took():
    """set_result captures _took = now - t0, and only then.

    Drives the REAL ToolMessage.set_result (borrowed onto a light object, the
    test_footer.py pattern) with the app-dependent repaint stubbed out, so the
    timing capture is tested headlessly. _took must be None until the result
    lands, then >= the elapsed time (here: the 2s we pretend the call ran).
    """
    class _TM:
        set_result = ToolMessage.set_result  # the real method, bound at call time

        def _update_display(self):
            pass  # needs a live Textual app; not what we are testing here

        def set_expanded(self, value):
            self.expanded = value

        def __init__(self):
            self.tool_name = "bash"
            self._args = ""
            self._result = None
            self._ok = True
            self._t0 = time.monotonic() - 2.0  # pretend the call started 2s ago
            self._took = None

    m = _TM()
    assert m._took is None, "no figure before the result lands"
    m.set_result("all done", True)
    assert m._result == "all done"
    assert m._ok is True
    assert m._took is not None and 1.9 <= m._took < 3.0, m._took


def test_render_progress_shows_measured_prefill_percent():
    # A NInfer prefill readout (frac, processed, total) is the measured truth
    # and REPLACES the est ~ projection.
    line = render_progress(0, 1.0, prompt_tokens=999, learned_rate=999.0,
                           prefill=(0.5, 71000, 112000))
    assert "prefill 50%" in line, line
    assert "71k/112k" in line, line
    assert "est ~" not in line, line  # measurement wins over estimate


def test_render_progress_falls_back_to_estimate_without_prefill():
    line = render_progress(0, 1.0, prompt_tokens=4096, learned_rate=4096.0)
    assert "est ~" in line, line
