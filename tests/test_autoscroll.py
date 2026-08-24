"""Autoscroll: follow the tail, but never yank a reader who scrolled up.

The bug Ryan reported was two bugs. The conversation did not scroll during a
stream -- and the reason it LOOKED like "only when the message comes through" is
that the sole scroll in the whole loop was the final render. And the thinking
block never scrolled the VerticalScroll it owns, so the trace grew below the
fold with the viewport pinned at the top.

The fix has an obvious wrong version: scroll_end unconditionally. That reads as
fixed and is worse, because Ryan reads long traces and would be yanked to the
bottom mid-sentence on every token. So the tests that matter here are the
NOT-following ones -- an unconditional implementation passes everything else.

🔴 THIS FILE'S FAKE ONCE STOPPED IMPLEMENTING THE HOST SEAM, AND THAT IS WHY IT
WAS RED. `_scroll_down` moved from asking `_at_bottom(log)` (geometry only) to
asking `self._still_following(log)`, which reads `self._follow_anchor` -- the
position the app itself last scrolled to. FakeApp carried no such attribute, so
the call raised AttributeError before any assertion ran. A fake that lags the
seam does not report a weaker result; it reports no result at all.

The anchor model matters to what these tests mean. Growing content raises
max_scroll_y and leaves scroll_y alone, so only a HUMAN moves scroll_y away
from the anchor. `_follow_anchor = 100.0` below therefore reads as "we last
scrolled to the tail", which is the state every one of these assertions is
actually about.
"""

from pathlib import Path
import ast
import re
import sys

import pytest

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as app_mod  # noqa: E402
from litetui import settings as settings_mod  # noqa: E402


class FakeScroll:
    """Minimal stand-in exposing only the geometry the predicates read."""

    def __init__(self, scroll_y, max_scroll_y):
        self.scroll_y = scroll_y
        self.max_scroll_y = max_scroll_y
        self.scrolled = False

    def scroll_end(self, animate=False):
        self.scrolled = True


class FakeApp:
    """Minimal host for _scroll_down.

    Carries `settings` because _scroll_down gates the STREAM path on the
    autoscroll preference; defaulting it ON keeps every assertion testing the
    follow/discrete distinction rather than passing because the feature is off.

    Carries `_follow_anchor` because _still_following reads it and _scroll_down
    writes it back. Defaulting to the tail (100.0) expresses "the app last
    scrolled to the bottom", which is the precondition every following/not-
    following assertion here depends on. Leaving it None would make
    _still_following return True unconditionally -- trivially following -- and
    the not-following tests, the only ones that can catch an unconditional
    implementation, would silently invert.
    """

    # Bind the REAL predicate rather than reimplementing it. A hand-written
    # stand-in would be a second copy of the follow rule, free to agree with a
    # stale idea of it -- which is the exact failure this file is recovering
    # from. Borrowing the method means _follow_anchor below is fed to the
    # shipping logic, so these assertions test the product, not a model of it.
    _still_following = app_mod.LiteTUI._still_following

    def __init__(self, log, autoscroll=True, follow_anchor=100.0):
        self._log = log
        self.settings = settings_mod.Settings(autoscroll=autoscroll)
        self._follow_anchor = follow_anchor

    def query_one(self, sel):
        return self._log


# ── _at_bottom: the predicate the whole fix rests on ─────────────────────────

@pytest.mark.parametrize(
    "scroll_y, max_scroll_y, expected, label",
    [
        (100.0, 100.0, True, "parked exactly at the end -> following"),
        (98.5, 100.0, True, "within the slack (float scroll lands fractionally)"),
        (40.0, 100.0, False, "🔴 scrolled up to read -> NOT following"),
        (97.0, 100.0, False, "one line above the slack -> NOT following"),
        (0.0, 0.0, True, "nothing to scroll yet (max 0) -> following"),
    ],
)
def test_at_bottom_geometry(scroll_y, max_scroll_y, expected, label):
    assert app_mod._at_bottom(FakeScroll(scroll_y, max_scroll_y)) is expected, label


def test_at_bottom_fails_open_without_geometry():
    """An over-eager scroll is a nit; a dead autoscroll is the bug being fixed."""

    class NoGeometry:
        pass

    assert app_mod._at_bottom(NoGeometry())


# ── _scroll_down honours the flag ────────────────────────────────────────────

def test_streaming_does_not_scroll_a_reader_who_scrolled_up():
    log = FakeScroll(40.0, 100.0)  # reader moved away from the 100.0 anchor
    app_mod.LiteTUI._scroll_down(FakeApp(log), only_if_following=True)
    assert log.scrolled is False, "🔴 streaming + reader scrolled up -> DOES NOT scroll"


def test_a_discrete_event_scrolls_even_when_scrolled_up():
    log = FakeScroll(40.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log))
    assert log.scrolled is True, "new bubble / tool / final render jumps to the end"


def test_streaming_follows_a_reader_at_the_tail():
    log = FakeScroll(100.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log), only_if_following=True)
    assert log.scrolled is True


def test_unset_anchor_is_trivially_following():
    """Nothing has been scrolled yet, so there is no reader movement to respect."""
    log = FakeScroll(40.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log, follow_anchor=None), only_if_following=True)
    assert log.scrolled is True


# ── the autoscroll SETTING gates the stream, not discrete events ─────────────

def test_setting_off_stops_the_stream_from_scrolling():
    log = FakeScroll(100.0, 100.0)  # reader AT the tail, so following
    app_mod.LiteTUI._scroll_down(FakeApp(log, autoscroll=False), only_if_following=True)
    assert log.scrolled is False


def test_setting_on_scrolls_when_following():
    """The discriminating half of the pair above."""
    log = FakeScroll(100.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log, autoscroll=True), only_if_following=True)
    assert log.scrolled is True


def test_setting_off_does_not_disable_discrete_scrolls():
    log = FakeScroll(40.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log, autoscroll=False))
    assert log.scrolled is True, "a new bubble still jumps"


# ── the stream loop actually CALLS it -- the original defect ─────────────────

def test_stream_branches_scroll():
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    m = re.search(r"if token:.*?thinking\.append\(token\)(.{0,400})", src, re.S)
    assert m and "only_if_following=True" in m.group(1), "reasoning-delta branch scrolls"
    # ⚠️ THE WINDOW IS A PROXIMITY PROXY, NOT A SPECIFICATION. It asserts the
    # branch scrolls; the character count is only how far it looks. Widened
    # 400 -> 600 at T070 O4-c, when publishing the tps rate added two lines to
    # this branch and pushed a scroll call that was still there out of range.
    # Verified before widening: there is exactly ONE `only_if_following=True`
    # in the following 1,200 chars, so a wider window cannot pass by matching
    # the next branch's call instead of this one.
    m = re.search(r"if delta\.content:(.{0,600})", src, re.S)
    assert m and "only_if_following=True" in m.group(1), "answer-content branch scrolls"


def test_follow_mode_is_used_never_a_bare_scroll_end():
    # exactly-2 broke at 0.20.0: glass-box compaction streams too and
    # legitimately follows. The two regex checks above pin the specific
    # branches; this counts the FLOOR.
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    assert src.count("_scroll_down(only_if_following=True)") >= 2


# ── ThinkingBlock follows its own body ───────────────────────────────────────

class FakeText:
    content = None


def _thinking_block(scroll):
    tb = app_mod.ThinkingBlock.__new__(app_mod.ThinkingBlock)
    tb._buffer = ""
    tb.text = FakeText()
    tb.scroll = scroll
    return tb


def test_thinking_block_at_the_tail_schedules_a_deferred_scroll():
    calls = []
    tb = _thinking_block(FakeScroll(100.0, 100.0))
    tb.call_after_refresh = lambda fn, **kw: calls.append(fn)
    tb.append("hello")
    assert len(calls) == 1, "at the tail -> schedules a scroll"
    # Deferred rather than inline: the extent has not grown until the content
    # is re-measured.
    assert all(callable(c) for c in calls)


def test_thinking_block_leaves_a_reader_who_scrolled_up_inside_the_trace():
    calls = []
    tb = _thinking_block(FakeScroll(10.0, 100.0))
    tb.call_after_refresh = lambda fn, **kw: calls.append(fn)
    tb.append("hello")
    assert calls == [], "🔴 reader scrolled up inside the trace -> does NOT scroll"
    assert tb._buffer == "hello", "following is about the VIEW, not the data"


# ── reasoning_effort was SKIPPED, not honoured (Ryan caught it in LM Studio) ──
# LM Studio drops a reasoning level the loaded virtual model does not accept and
# returns 200, so `/think off` reports success while the model reasons at the
# server default. The only in-band evidence is a trace arriving when none was
# asked for.

class FakeSys:
    def __init__(self, level):
        self.thinking_level = level
        self._reasoning_ignored_warned = False
        self.msgs = []

    _system = lambda self, m: self.msgs.append(m)  # noqa: E731
    _warn_reasoning_ignored = app_mod.LiteTUI._warn_reasoning_ignored


def test_warns_once_when_a_trace_arrives_despite_think_off():
    f = FakeSys("off")
    f._warn_reasoning_ignored()
    assert len(f.msgs) == 1, "🔴 asked for off, trace arrived -> warns"
    assert "SKIPPED" in f.msgs[0] and "200" in f.msgs[0], "names the mechanism"
    assert f"/think {app_mod.LEAST_THINKING_FALLBACK}" in f.msgs[0], "says what to do"
    f._warn_reasoning_ignored()
    f._warn_reasoning_ignored()
    assert len(f.msgs) == 1, "once per session, not once per token"


def test_the_warning_is_gated_on_think_off_and_sits_on_the_reasoning_branch():
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    # 🔴 THE BLOCK, NOT A CHARACTER WINDOW. This was `if token:(.{0,600})`, then
    # `.{0,1600}` after a comment block pushed the gate out of range, and then it
    # broke a THIRD time the same way (T079 added three comment lines above the
    # tick). Each widening defers the same failure and makes the window wider
    # than the thing it is meant to bound.
    #
    # The property is STRUCTURAL — "the gate lives inside the reasoning branch" —
    # so it is asserted structurally. Commentary of any length is now irrelevant,
    # which is what the previous comment already said it wanted.
    tree = ast.parse(src)
    blocks = [
        ast.get_source_segment(src, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "token"
    ]
    assert blocks, "no `if token:` reasoning branch found at all"
    # There is more than one: `_stream` streams the trace and `_compact` extracts
    # it again. The warning belongs to the streaming one, so ANY block carrying
    # both is the proof — naming which would re-couple this to line order.
    guarded = [
        b for b in blocks
        if 'self.thinking_level == "off"' in b and "_warn_reasoning_ignored()" in b
    ]
    assert guarded, (
        "the think-off gate and the warning are no longer together inside a "
        f"reasoning branch ({len(blocks)} such branches scanned)"
    )
    assert "ext.virtualModel.customField" in src and "Skipping this field" in src, (
        "the narrowed valid set is recorded in source, with its provenance"
    )


# ── a NEW thinking block jumps the log to the bottom ─────────────────────────

def test_a_new_thinking_block_scrolls_unconditionally():
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    # rfind, not find: 0.20.0's CompactionCard mounts its own ThinkingBlock
    # EARLIER in the file, and find() silently re-anchored this gate onto the
    # wrong site. The stream's mount is the LAST occurrence.
    mount = src.rfind("thinking = ThinkingBlock()")
    assert mount > 0, "the mount site exists"

    # ORDERING, NOT A WINDOW. This used to read `src[mount:mount+400]` and look
    # for the literal `self._scroll_down()`. Both halves of that rotted:
    #   * the call became `self.call_after_refresh(self._scroll_down)` -- a FIX,
    #     not a regression, because mount() is not measured in this frame, so an
    #     inline scroll targets the PRE-mount extent and parks the viewport just
    #     above the block that appeared;
    #   * a comment explaining that fix pushed the call to +929, past the window.
    # Widening it is a trap: the per-token CONDITIONAL scroll sits at +1756, so
    # any number above that silently inverts the second assertion. The invariant
    # was never "within N characters" -- it is "the FIRST scroll after the mount
    # is the unconditional one". Expressed as ordering, there is no number to
    # tune and no comment edit can move it.
    candidates = [
        src.find("self.call_after_refresh(self._scroll_down)", mount),
        src.find("self._scroll_down()", mount),
    ]
    uncond = min((c for c in candidates if c != -1), default=-1)
    cond = src.find("self._scroll_down(only_if_following=True)", mount)

    assert uncond != -1, "🔴 an UNCONDITIONAL scroll must accompany a new thinking block"
    assert cond == -1 or uncond < cond, (
        "...and it must come FIRST — the conditional form is what never fired for "
        "a freshly mounted block, because the reader is not yet at the new tail"
    )
    # The conditional form must still exist for per-token growth, or a reader
    # who scrolls up mid-trace would be yanked back on every token.
    assert "self._scroll_down(only_if_following=True)" in src
