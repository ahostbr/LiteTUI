"""Autoscroll follows an explicit user-owned lock.

The app no longer infers follow ownership from viewport geometry. When the
scroll lock is on, new output follows the tail; when it is off, no app event
may move the reader. Every deferred scroll captures ``_follow_generation`` so
a newer lock decision invalidates work accepted in an earlier frame.

ThinkingBlock has a separate, local geometry rule because it owns its nested
VerticalScroll: it follows only while that inner viewport is already at its
tail.
"""

from pathlib import Path
import ast
import sys

import pytest

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as app_mod  # noqa: E402
from litetui import settings as settings_mod  # noqa: E402


class FakeScroll:
    """Minimal stand-in for the viewport and deferred-refresh seams."""

    def __init__(self, scroll_y, max_scroll_y):
        self.scroll_y = scroll_y
        self.max_scroll_y = max_scroll_y
        self.scrolled = False

    def call_after_refresh(self, callback):
        # The ordinary unit fake has no refresh loop. Controlled ordering tests
        # below replace this with a queue.
        callback()

    def scroll_end(self, animate=False, on_complete=None, immediate=False):
        self.scrolled = True
        # Production Textual invokes this after its deferred scroll settles.
        # The fake has no refresh loop, so settle synchronously at its tail.
        self.scroll_y = self.max_scroll_y
        if on_complete is not None:
            on_complete()


class DeferredScroll(FakeScroll):
    """Textual's refresh seam, with release controlled by the test."""

    def __init__(self, scroll_y, max_scroll_y):
        super().__init__(scroll_y, max_scroll_y)
        self.actions = []
        self.completions = []

    def call_after_refresh(self, callback):
        self.actions.append(callback)

    def scroll_end(self, animate=False, on_complete=None, immediate=False):
        if not immediate:
            self.actions.append(
                lambda: self.scroll_end(
                    animate=animate, on_complete=on_complete, immediate=True
                )
            )
            return
        self.scrolled = True
        self.scroll_y = self.max_scroll_y
        if on_complete is not None:
            self.completions.append(on_complete)

    def release_action(self):
        self.actions.pop(0)()

    def release_completion(self):
        self.completions.pop(0)()


class FakeApp:
    """Minimal host that delegates the shipping scroll-lock methods."""

    _autocollapse_offscreen = app_mod.LiteTUI._autocollapse_offscreen
    _next_follow_generation = app_mod.LiteTUI._next_follow_generation
    _scroll_down = app_mod.LiteTUI._scroll_down
    action_toggle_scroll_lock = app_mod.LiteTUI.action_toggle_scroll_lock

    def __init__(self, log, autoscroll=True):
        self._log = log
        self.settings = settings_mod.Settings(autoscroll=autoscroll)
        self._follow_generation = 0
        self._scroll_queued = None

    def query_one(self, sel):
        return self._log

    def _refresh_prompt_controls(self):
        pass


# ── nested ThinkingBlock geometry helper ─────────────────────────────────────

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
    """A nested view without measurable geometry should keep following."""

    class NoGeometry:
        pass

    assert app_mod._at_bottom(NoGeometry())


# ── _scroll_down honours the explicit lock ──────────────────────────────────

def test_lock_on_follows_even_when_the_viewport_is_not_at_the_tail():
    log = FakeScroll(40.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log, autoscroll=True))
    assert log.scrolled is True


def test_lock_off_never_scrolls_even_when_the_viewport_is_at_the_tail():
    log = FakeScroll(100.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log, autoscroll=False))
    assert log.scrolled is False


def test_reader_acted_does_not_override_an_unlocked_view():
    """Submitting text is not a hidden escape hatch around the visible lock."""
    log = FakeScroll(40.0, 100.0)
    app_mod.LiteTUI._scroll_down(FakeApp(log, autoscroll=False), reader_acted=True)
    assert log.scrolled is False


# ── the stream loop actually CALLS it -- the original defect ─────────────────

def test_stream_updates_reach_the_single_scroll_door():
    """Trace deltas ask directly; answer growth is observed by ChatLog."""
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    token_blocks = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "token"
    ]
    assert any(
        "self._scroll_down()" in (ast.get_source_segment(src, node) or "")
        for node in token_blocks
    ), "reasoning-delta branch no longer requests a locked scroll"

    content_blocks = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Attribute)
        and node.test.attr == "content"
    ]
    assert any(
        "sink.show(text_full)" in (ast.get_source_segment(src, node) or "")
        for node in content_blocks
    ), "answer deltas no longer update the measured chat content"

    chat_log = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ChatLog"
    )
    size_watcher = next(
        node for node in chat_log.body
        if isinstance(node, ast.FunctionDef) and node.name == "watch_virtual_size"
    )
    watcher_src = ast.get_source_segment(src, size_watcher) or ""
    assert "self.app._scroll_down()" in watcher_src, (
        "answer growth no longer reaches the explicit scroll-lock door"
    )


def test_chat_log_does_not_turn_wheel_motion_into_lock_ownership() -> None:
    """Scrolling history is independent of the explicit lock control."""
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    chat_log = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ChatLog"
    )
    handlers = {
        node.name for node in chat_log.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "on_mouse_scroll_up" not in handlers
    assert "on_mouse_scroll_down" not in handlers


def test_follow_mode_is_used_never_a_bare_scroll_end():
    """The app must not bypass its explicit-lock door with a direct scroll."""
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    assert src.count("self._scroll_down()") >= 2
    body = src.split("def _scroll_down(", 1)[1]
    others = src.replace(body, "", 1)
    assert "scroll_end(" not in others, (
        "something calls scroll_end outside `_scroll_down` — that is a bare "
        "scroll the explicit lock cannot gate"
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
    # `_at_bottom` check. The app now uses only the explicit lock, so mounting a
    # block cannot silently infer or revoke follow ownership from geometry.
    #
    # ⬜ WHAT SURVIVES IS THE DEFERRAL, which is the half that was always about
    # measurement rather than about policy: `mount()` is not measured in this
    # frame, so an inline scroll targets the PRE-mount extent and parks the
    # viewport short. `call_after_refresh` is what makes the scroll land.
    assert "self.call_after_refresh(self._scroll_down)" in src, (
        "the mount-time scroll stopped being deferred — an inline scroll here "
        "aims at the extent the log had BEFORE the block was mounted"
    )


# ── explicit lock generation and deferred work ────────────────────────────


def test_turning_lock_off_invalidates_an_accepted_deferred_scroll() -> None:
    """A newer reader decision must beat work accepted in an earlier frame."""
    log = DeferredScroll(100.0, 140.0)
    app = FakeApp(log, autoscroll=True)

    app_mod.LiteTUI._scroll_down(app)
    assert len(log.actions) == 1

    app.action_toggle_scroll_lock()
    assert app.settings.autoscroll is False
    assert app._follow_generation == 1

    log.scroll_y = 70.0
    log.release_action()
    assert log.scroll_y == 70.0, "stale deferred action overruled the unlocked reader"
    assert log.scrolled is False


def test_lock_change_invalidates_old_work_before_new_lock_work_runs() -> None:
    log = DeferredScroll(100.0, 140.0)
    app = FakeApp(log, autoscroll=True)

    app_mod.LiteTUI._scroll_down(app)       # generation 0
    app.action_toggle_scroll_lock()         # generation 1, off
    app.action_toggle_scroll_lock()         # generation 2, on; queues fresh work
    assert len(log.actions) == 2

    log.scroll_y = 70.0
    log.release_action()
    assert log.scroll_y == 70.0, "generation-0 action was not refused"
    log.release_action()
    assert log.scroll_y == 140.0, "current lock generation did not follow"


def test_repeated_requests_in_one_generation_queue_one_scroll() -> None:
    log = DeferredScroll(100.0, 140.0)
    app = FakeApp(log)

    app_mod.LiteTUI._scroll_down(app)
    app_mod.LiteTUI._scroll_down(app)

    assert len(log.actions) == 1
    log.release_action()
    assert app._scroll_queued is None
    assert log.scrolled is True


def test_lock_off_does_not_claim_the_generation_queue_slot() -> None:
    log = DeferredScroll(100.0, 140.0)
    app = FakeApp(log, autoscroll=False)

    app_mod.LiteTUI._scroll_down(app)

    assert log.actions == []
    assert app._scroll_queued is None


def test_every_app_scroll_call_goes_through_the_one_door():
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    assert "def _scroll_down(self, *, reader_acted: bool = False)" in src
    body = src.split("def _scroll_down(", 1)[1]
    others = src.replace(body, "", 1)
    assert "scroll_end(" not in others, (
        "something calls scroll_end outside `_scroll_down`, bypassing the lock"
    )
    assert src.count("self._scroll_down()") >= 2


def test_every_log_rebuild_invalidates_queued_scroll_work():
    """A callback authorized for discarded content cannot act on the new log."""
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    empties = [i for i in range(len(src)) if src.startswith("remove_children()", i)]
    assert empties
    for i in empties:
        line_end = src.find("\n", i)
        nxt = src.find("_next_follow_generation()", line_end)
        assert nxt != -1, f"log rebuild near offset {i} never invalidates scroll work"
        intervening_scroll = src.find("_scroll_down(", line_end, nxt)
        assert intervening_scroll == -1, (
            f"log rebuild near offset {i} scrolls before invalidating stale work"
        )
