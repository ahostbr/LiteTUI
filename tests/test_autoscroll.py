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
    app_mod.LiteTUI._scroll_down(FakeApp(log))
    assert log.scrolled is False, "🔴 streaming + reader scrolled up -> DOES NOT scroll"


def test_a_discrete_event_does_NOT_scroll_a_reader_who_scrolled_up():
    """🔴 THIS ARM ASSERTED THE DEFECT UNTIL T706, WHICH IS WHY A GREEN SUITE
    NEVER CAUGHT IT.

    Ryan, 2026-09-12, watching a turn with tool calls streaming: *"autoscroll
    is always on... it should only turn on when the user scrolls to the bottom
    and then lock. if i start scrolling up on my own right now it drags me back
    down NO MATTER WHAT."*

    "No matter what" is the whole clause: there is no discrete-event exception.
    The old docstring argued a new bubble or tool card "is the user's own
    action" — true when a human presses send, false for every tool card, tool
    result and system line a turn mounts on its own, and those are what drag.
    """
    log = FakeScroll(40.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log))
    assert log.scrolled is False, (
        "a tool card / result / system line yanked a reader who had scrolled up"
    )


def test_streaming_follows_a_reader_at_the_tail():
    log = FakeScroll(100.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log))
    assert log.scrolled is True


def test_unset_anchor_is_trivially_following():
    """Nothing has been scrolled yet, so there is no reader movement to respect."""
    log = FakeScroll(40.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log, follow_anchor=None))
    assert log.scrolled is True


# ── the autoscroll SETTING gates the stream, not discrete events ─────────────

def test_setting_off_stops_the_stream_from_scrolling():
    log = FakeScroll(100.0, 100.0)  # reader AT the tail, so following
    app_mod.LiteTUI._scroll_down(FakeApp(log, autoscroll=False))
    assert log.scrolled is False


def test_setting_on_scrolls_when_following():
    """The discriminating half of the pair above."""
    log = FakeScroll(100.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log, autoscroll=True))
    assert log.scrolled is True


def test_setting_off_disables_following_ENTIRELY():
    """Ryan item 4: the setting is the master switch for following. It used to
    gate the stream only, so with autoscroll OFF a tool card still jumped —
    which is the same "no matter what" complaint arriving through the setting
    instead of through the scroll position."""
    log = FakeScroll(100.0, 100.0)          # at the tail: following would fire
    app_mod.LiteTUI._scroll_down(FakeApp(log, autoscroll=False))
    assert log.scrolled is False, "autoscroll is off and something still followed"


# ── the stream loop actually CALLS it -- the original defect ─────────────────

def test_stream_branches_scroll():
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    m = re.search(r"if token:.*?thinking\.append\(token\)(.{0,400})", src, re.S)
    assert m and "self._scroll_down()" in m.group(1), "reasoning-delta branch scrolls"
    # ⚠️ THE WINDOW IS A PROXIMITY PROXY, NOT A SPECIFICATION. It asserts the
    # branch scrolls; the character count is only how far it looks. Widened
    # 400 -> 600 at T070 O4-c, when publishing the tps rate added two lines to
    # this branch and pushed a scroll call that was still there out of range.
    # Verified before widening: there is exactly ONE scroll call in the
    # following 1,200 chars, so a wider window cannot pass by matching the next
    # branch's call instead of this one.
    # ⬜ T706: the spelling moved (the follow check became the default and the
    # kwarg is gone) and the CLAIM did not — this branch must still scroll.
    m = re.search(r"if delta\.content:(.{0,600})", src, re.S)
    assert m and "self._scroll_down()" in m.group(1), "answer-content branch scrolls"


def test_follow_mode_is_used_never_a_bare_scroll_end():
    """⬜ T706 MADE THIS ARM ALMOST FREE, AND IT IS KEPT FOR THE OTHER HALF.

    It counted calls that opted INTO the follow check. Every call is gated now,
    so that floor is met by construction — what is still worth asserting is the
    second half of its own name: the app must never reach past the door and
    call `scroll_end` itself. `test_every_scroll_call_goes_through_the_ONE_door`
    carries the stronger claim.
    """
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    assert src.count("self._scroll_down()") >= 2
    body = src.split("def _scroll_down(", 1)[1]
    others = src.replace(body, "", 1)
    assert "scroll_end(" not in others, (
        "something calls scroll_end outside `_scroll_down` — that is a bare "
        "scroll that no follow check can see"
    )


# ── ThinkingBlock follows its own body ───────────────────────────────────────

class FakeText:
    content = None


def _thinking_block(scroll):
    """A REAL ThinkingBlock with only its two view objects replaced.

    🔴 THIS USED `__new__` AND HAND-SET THREE FIELDS, AND THAT IS WHY IT BROKE.
    `59df1c4` ("freeze the header readout when thinking ends") gave `append()` a
    token counter — `self._toks += 1`, initialised correctly in `__init__` — and
    the hand-built instance had never run `__init__`, so it had no `_toks` and
    every arm here died with AttributeError at widgets.py:269. The production
    change was right; the double was frozen at the shape the class had when
    somebody typed it out.
        A DOUBLE ASSEMBLED FIELD BY FIELD IS A SNAPSHOT OF THE CLASS ON THE DAY
        IT WAS WRITTEN, AND NOTHING TELLS IT THE CLASS MOVED.

    ⬜ `ThinkingBlock()` CONSTRUCTS FINE OUTSIDE A RUNNING APP — measured, no
    mounting or event loop needed — so running the real `__init__` costs
    nothing and makes the whole class of failure impossible: a field added
    there is present here the moment it exists. Only `text` and `scroll` are
    swapped, because those are the view objects these arms exist to fake.
    """
    tb = app_mod.ThinkingBlock()
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

def test_a_new_thinking_block_defers_its_scroll_to_after_the_mount():
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
    deferred = min((c for c in candidates if c != -1), default=-1)

    assert deferred != -1, "a scroll must accompany a new thinking block"

    # 🔴 WHAT THIS ARM USED TO ASSERT, AND WHY IT NO LONGER CAN (T706).
    # It required the accompanying scroll to be UNCONDITIONAL, reasoning that
    # the conditional form "never fired for a freshly mounted block, because
    # the reader is not yet at the new tail". That was true of the OLD
    # `_at_bottom` check and stopped being true when `_still_following` moved to
    # the anchor: mounting a block raises max_scroll_y and leaves scroll_y
    # alone, so the reader is still following and the gated call fires. The
    # unconditional escape hatch was load-bearing for a predicate that no longer
    # exists — and under Ryan's rule ("no matter what") it is exactly the drag.
    #
    # ⬜ WHAT SURVIVES IS THE DEFERRAL, which is the half that was always about
    # measurement rather than about policy: `mount()` is not measured in this
    # frame, so an inline scroll targets the PRE-mount extent and parks the
    # viewport short. `call_after_refresh` is what makes the scroll land.
    assert "self.call_after_refresh(self._scroll_down)" in src, (
        "the mount-time scroll stopped being deferred — an inline scroll here "
        "aims at the extent the log had BEFORE the block was mounted"
    )


# ── T706: follow is a LOCK, not a default ────────────────────────────────────
#
# Ryan: "it should only turn on when the user scrolls to the bottom and then
# lock. if i start scrolling up on my own right now it drags me back down no
# matter what."
#
# ⬜ NO NEW GEOMETRY CHECK. The follow ANCHOR already answers "did the reader
# move, or did the content move" — `_still_following` compares scroll_y against
# where WE last scrolled, and content growth raises max_scroll_y without
# touching scroll_y. Re-locking therefore needs no scroll watcher: returning to
# the bottom puts scroll_y back at or past the anchor and following resumes on
# its own. Reintroducing an at-bottom check is what the thinking-block fix
# removed, and it is what made those three arms flaky.


def test_returning_to_the_bottom_RE_LOCKS_following():
    """Ryan item 3: "turn on when the user scrolls to the bottom and then lock".

    The reader scrolled up (40 against a 100 anchor), then came back. No event
    told the app so — the anchor comparison simply becomes true again.
    """
    log = FakeScroll(100.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log, follow_anchor=100.0))
    assert log.scrolled is True, "the reader came back to the tail and nothing followed"


def test_the_reader_returning_after_the_content_grew_also_re_locks():
    """The same, with the bottom having moved while they were away — which is
    the normal case during a turn. scroll_y ends up far PAST the old anchor."""
    log = FakeScroll(400.0, 400.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log, follow_anchor=100.0))
    assert log.scrolled is True


def test_the_readers_OWN_action_scrolls_even_when_scrolled_up():
    """Ryan item 1: sending a prompt is the reader's own action, so it follows
    and RE-ENGAGES the lock. This is the one exception, and it exists because
    the reader did it — not because the app decided the event was important.

    ⚠️ IT IS NOT `_user_bubble`. That helper mounts the bubble for cron fires,
    inbox mail and goal-loop turns as well as for a human pressing send
    (`grep -rn "_user_bubble(" src/` — 15 callers, 4 of them unattended), and
    those are exactly the yanks being removed. The flag belongs at
    `_submit_text`, which only a person reaches.
    """
    log = FakeScroll(40.0, 100.0)
    app = FakeApp(log)
    app_mod.LiteTUI._scroll_down(app, reader_acted=True)
    assert log.scrolled is True, "the reader pressed send and the view did not move"
    assert app._follow_anchor == log.scroll_y, "the lock was not re-engaged"


def test_a_refused_scroll_does_NOT_move_the_anchor():
    """🔴 THE ARM THAT SEPARATES A GATE FROM A FIX, and the reason one bare call
    used to poison the whole turn.

    `_scroll_down` writes `_follow_anchor` after it scrolls. The old bare path
    scrolled unconditionally AND re-anchored, so a single tool card both dragged
    the reader down and told every later follow check that the reader was at the
    tail. A gate that still moved the anchor would leave that second half intact.
    """
    log = FakeScroll(40.0, 100.0)
    app = FakeApp(log)
    app_mod.LiteTUI._scroll_down(app)
    assert log.scrolled is False
    assert app._follow_anchor == 100.0, "a refused scroll moved the anchor anyway"


def test_every_scroll_call_goes_through_the_ONE_door():
    """Ryan item 2 as a structural claim: no caller may opt out.

    Twelve of the seventeen call sites used to pass nothing and scroll
    unconditionally. The parameter that let them is gone, so the only way to
    bypass the follow check is `reader_acted`, and that is spelled at the call
    site where a reviewer can see it.
    """
    import re
    from pathlib import Path

    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    # ⚠️ THE SIGNATURE, NOT THE WORD. The first cut of this arm asserted the
    # string `only_if_following` was absent from app.py — and the docstring
    # explaining the change quotes it, so the arm failed on its own prose. A
    # text gate counts the writing ABOUT a thing as the thing.
    assert "def _scroll_down(self, *, reader_acted: bool = False)" in src, (
        "the one door changed shape; `reader_acted` is the only sanctioned "
        "way past the follow check"
    )
    assert "only_if_following: bool" not in src, (
        "a caller can still opt out of the follow check, so 'no matter what' "
        "is not enforced anywhere"
    )
    acted = re.findall(r"_scroll_down\(reader_acted=True\)", src)
    assert len(acted) >= 1, "nothing re-engages the lock; a prompt submit must"
    # The validity gate: if every call became reader_acted the rule would be
    # green and the drag unchanged.
    bare = re.findall(r"_scroll_down\(\)", src)
    assert len(bare) > len(acted), (
        f"{len(acted)} reader_acted vs {len(bare)} gated calls — the exception "
        f"has become the rule"
    )


def test_emptying_the_log_clears_the_anchor():
    """🔴 FOUND BY READING THE CALL SITES, NOT BY AN ARM — and it would have
    shipped as "resume opens at the top".

    Gating every scroll means the anchor is suddenly load-bearing for paths
    that never consulted it. `_resume` empties the log, rebuilds it from the
    transcript and scrolls to the end; the anchor at that moment belongs to the
    conversation being REPLACED. A stale 500 against a rebuilt log at 0 reads
    as "the reader scrolled up", the scroll is refused, and the conversation
    opens at the top with no error anywhere.

    Asserted on the SOURCE because both sites are inside long UI methods that a
    unit host cannot reach, and the claim is structural: every place that
    empties the log resets the anchor in the same breath.
    """
    from pathlib import Path

    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    empties = [i for i in range(len(src)) if src.startswith("remove_children()", i)]
    assert empties, "no log-emptying site found — this arm has lost its subject"
    # ⚠️ ORDERING, NOT A WINDOW — and the first cut of this arm used a 400-char
    # window and failed on its own subject, because the comment explaining the
    # reset is longer than the window and pushed the assignment out of range.
    # This file already carries that lesson at test_stream_branches_scroll
    # ("the window is a proximity PROXY, not a specification"); a rule with a
    # tunable number in it invites exactly this.
    for i in empties:
        nxt = src.find("_follow_anchor", i)
        assert nxt != -1 and src[nxt:nxt + 30].startswith("_follow_anchor = None"), (
            f"the first thing a log-emptying site does with the anchor is not "
            f"clearing it (near offset {i}) — the next scroll would be judged "
            f"against a position from the conversation just thrown away"
        )
