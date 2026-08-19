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
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import app as app_mod

ok = []


def chk(label, cond):
    ok.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


class FakeScroll:
    """Minimal stand-in exposing only the geometry _at_bottom reads."""

    def __init__(self, scroll_y, max_scroll_y):
        self.scroll_y = scroll_y
        self.max_scroll_y = max_scroll_y
        self.scrolled = False

    def scroll_end(self, animate=False):
        self.scrolled = True


print("=== _at_bottom: the predicate the whole fix rests on ===")
chk("parked exactly at the end -> following", app_mod._at_bottom(FakeScroll(100.0, 100.0)))
chk("within the slack (float scroll lands fractionally) -> following",
    app_mod._at_bottom(FakeScroll(98.5, 100.0)))
chk("🔴 scrolled up to read -> NOT following", not app_mod._at_bottom(FakeScroll(40.0, 100.0)))
chk("one line above the slack -> NOT following", app_mod._at_bottom(FakeScroll(97.0, 100.0)) is False)
chk("nothing to scroll yet (max 0) -> following", app_mod._at_bottom(FakeScroll(0.0, 0.0)))


print("\n=== it fails OPEN, never silently dead ===")
class NoGeometry:
    pass
chk("a widget with no scroll geometry -> following (an over-eager scroll is a nit;",
    app_mod._at_bottom(NoGeometry()))
print("        a dead autoscroll is the bug being fixed)")


print("\n=== _scroll_down honours the flag ===")
class FakeApp:
    def __init__(self, log):
        self._log = log
    def query_one(self, sel):
        return self._log

log = FakeScroll(40.0, 100.0)          # reader has scrolled up
app_mod.LiteTUI._scroll_down(FakeApp(log), only_if_following=True)
chk("🔴 streaming + reader scrolled up -> DOES NOT scroll", log.scrolled is False)

log = FakeScroll(40.0, 100.0)          # same state, discrete event
app_mod.LiteTUI._scroll_down(FakeApp(log))
chk("a discrete event scrolls even when scrolled up (new bubble / tool / final)",
    log.scrolled is True)

log = FakeScroll(100.0, 100.0)         # reader is following
app_mod.LiteTUI._scroll_down(FakeApp(log), only_if_following=True)
chk("streaming + reader at the tail -> follows", log.scrolled is True)


print("\n=== the stream loop actually CALLS it -- the original defect ===")
src = Path(app_mod.__file__).read_text(encoding="utf-8")
import re
# the two branches that had no scroll at all
m = re.search(r"if token:.*?thinking\.append\(token\)(.{0,400})", src, re.S)
chk("reasoning-delta branch scrolls", bool(m) and "only_if_following=True" in m.group(1))
m = re.search(r"if delta\.content:(.{0,400})", src, re.S)
chk("answer-content branch scrolls", bool(m) and "only_if_following=True" in m.group(1))
chk("both use follow mode, not a bare scroll_end",
    src.count("_scroll_down(only_if_following=True)") == 2)


print("\n=== ThinkingBlock follows its own body ===")
tb = app_mod.ThinkingBlock.__new__(app_mod.ThinkingBlock)
tb._buffer = ""
calls = []
class FakeText:
    content = None
tb.text = FakeText()
tb.scroll = FakeScroll(100.0, 100.0)
tb.call_after_refresh = lambda fn, **kw: calls.append(fn)
tb.append("hello")
chk("at the tail -> schedules a scroll", len(calls) == 1)
chk("...deferred to after the refresh, not called inline (the extent has not",
    all(callable(c) for c in calls))
print("        grown until the content is re-measured)")

tb2 = app_mod.ThinkingBlock.__new__(app_mod.ThinkingBlock)
tb2._buffer = ""
calls2 = []
tb2.text = FakeText()
tb2.scroll = FakeScroll(10.0, 100.0)   # reader scrolled up INSIDE the trace
tb2.call_after_refresh = lambda fn, **kw: calls2.append(fn)
tb2.append("hello")
chk("🔴 reader scrolled up inside the trace -> does NOT scroll", calls2 == [])
chk("...but the text still updated (following is about the VIEW, not the data)",
    tb2._buffer == "hello")



print("\n=== reasoning_effort was SKIPPED, not honoured (Ryan caught it in LM Studio) ===")
# LM Studio drops a reasoning level the loaded virtual model does not accept and
# returns 200, so `/think off` reports success while the model reasons at the
# server default. The only in-band evidence is a trace arriving when none was
# asked for.
class FakeSys:
    def __init__(self, level):
        self.thinking_level = level
        self._reasoning_ignored_warned = False
        self.msgs = []
    _system = lambda self, m: self.msgs.append(m)
    _warn_reasoning_ignored = app_mod.LiteTUI._warn_reasoning_ignored

f = FakeSys("off")
f._warn_reasoning_ignored()
chk("🔴 asked for off, trace arrived -> warns", len(f.msgs) == 1)
chk("...names the mechanism, not just the symptom",
    "SKIPPED" in f.msgs[0] and "200" in f.msgs[0])
chk("...tells the user what to do instead",
    f"/think {app_mod.LEAST_THINKING_FALLBACK}" in f.msgs[0])
f._warn_reasoning_ignored(); f._warn_reasoning_ignored()
chk("once per session, not once per token", len(f.msgs) == 1)

src2 = Path(app_mod.__file__).read_text(encoding="utf-8")
import re as _re
m = _re.search(r"if token:(.{0,600})", src2, _re.S)
chk("the check is gated on thinking_level == 'off' (not fired for every trace)",
    bool(m) and 'self.thinking_level == "off"' in m.group(1))
chk("...and sits on the reasoning branch, where the evidence actually is",
    bool(m) and "_warn_reasoning_ignored()" in m.group(1))
chk("the narrowed valid set is recorded in source, with its provenance",
    "ext.virtualModel.customField" in src2 and "Skipping this field" in src2)

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
