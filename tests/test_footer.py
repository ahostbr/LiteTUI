"""The footer carries the seat's fleet identity, and refuses to claim one it lacks.

Ryan: "his footer needs to show his liteharness name, thinking level and first
chunk of his convo uuid."

The interesting case is the UNREGISTERED one. A seat can fail to register --
liteharness absent, CLI errored -- and a footer that prints the name anyway is a
green light for something that never happened. That is the same class of bug as
the wizard health check counting duplicates as success, in a different pane.
"""
import sys
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as app_mod
from litetui import appsvc
from litetui import turnstats

ok = []


def chk(label, cond):
    ok.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


class FakeSeat:
    def __init__(self, registered, name="BlackGrid"):
        self.registered = registered
        self.name = name


class FakeApp:
    ctx_label_text = app_mod.LiteTUI.ctx_label_text
    # Renamed when the early-return bug was fixed: the old _append_tps could be
    # skipped entirely when the context window had not resolved.
    _append_tps_into = appsvc.append_tps_into

    def __init__(self, seat=None, think=None, convo="", used=None, mx=None, tps=None,
                 settings=None):
        # The footer reads per-field toggles now. Default them ALL ON so every
        # assertion below keeps testing the field it was written for rather than
        # accidentally passing because the field is switched off.
        from litetui.settings import Settings

        self.settings = settings if settings is not None else Settings()
        self.seat = seat
        self.thinking_level = think
        self.convo_id = convo
        self.ctx_used = used
        self.ctx_max = mx
        # `tps` stays on the app -- it is the reactive the footer reads.
        # The bookkeeping behind it moved to turnstats.TpsState (T070 O4-c).
        self.tps = tps


CONVO = "4f3a1c9d-2b7e-4a11-9c30-8e5f6d1b2a44"

print("=== a registered seat is named ===")
t = FakeApp(FakeSeat(True), "low", CONVO, 5087, 100096).ctx_label_text.plain
print("   ", t)
chk("shows the harness NAME", "BlackGrid" in t)
chk("shows the thinking level", "think:low" in t)
chk("shows the first chunk of the convo uuid", "4f3a1c9d" in t)
chk("...only the first chunk, not the whole uuid", CONVO not in t)
chk("still shows the context readout", "ctx 5,087 / 100,096" in t)

print("\n=== 🔴 an UNREGISTERED seat must not claim a name ===")
t = FakeApp(FakeSeat(False), None, CONVO).ctx_label_text.plain
print("   ", t)
chk("does NOT print the name it does not hold", "BlackGrid" not in t)
chk("says so plainly instead", "unregistered" in t)
chk("no seat object at all is also handled", "no seat" in FakeApp(None).ctx_label_text.plain)

print("\n=== unset thinking reads as the server default, not as 'off' ===")
t = FakeApp(FakeSeat(True), None, CONVO).ctx_label_text.plain
chk("unset renders as think:default", "think:default" in t)
chk("...and never as think:off (they are different states)", "think:off" not in t)

print("\n=== degrades rather than crashing ===")
t = FakeApp(FakeSeat(True), "high", "", None, None).ctx_label_text.plain
chk("no conversation yet -> the id is simply absent", "think:high" in t and "ctx" in t)
chk("no context numbers yet -> em dash, no exception", "ctx \u2014" in t)

print("\n=== the recompose constraints the Footer class already encodes ===")
src = Path(app_mod.__file__).read_text(encoding="utf-8")

# WHOLE-PACKAGE source, not app.py's. The ctx-label check below is an invariant
# about how the label is ADDRESSED, not about which file constructs it -- and
# 4e6125e proved the difference by moving the construction site to widgets.py
# while the property itself held (classes="ctx-label" once, id="ctx-label" zero).
# Reading one file made a correct lift look like a regression: red test, named
# assertion, real diff, and nothing actually broken.
#
# A gate spelled `"literal" in <one file>` asserts TWO things and declares one --
# the property, and silently "...and it lives HERE". T070 is a project whose
# entire purpose is moving code out of app.py, so the undeclared half is the half
# that breaks.
#
# The two chk() calls after this one still read `src` on purpose: their subjects
# genuinely live in app.py today and they pass. They are on the SAME fuse -- O2
# lifts method groups next -- so re-scope them when they move, do not pre-empt it.
pkg_src = "".join(
    p.read_text(encoding="utf-8", errors="replace")
    for p in sorted(Path(app_mod.__file__).parent.rglob("*.py"))
)
chk("the label is still addressed by CLASS, never a fixed id",
    'classes="ctx-label"' in pkg_src and 'id="ctx-label"' not in pkg_src)
chk("_refresh_ctx_label still updates EVERY match (a transient duplicate is cosmetic)",
    'for label in labels:' in src)
chk("changing the thinking level refreshes the footer, not just the header",
    "self._update_header()" in src and src.count("self._refresh_ctx_label()") >= 4)

print("\n=== tok/s ===")
import time as _t

a = FakeApp(FakeSeat(True), "low", CONVO, 5087, 100096)
chk("absent before any turn -> the footer simply omits it",
    "tok/s" not in a.ctx_label_text.plain)

a = FakeApp(FakeSeat(True), "low", CONVO, 5087, 100096, tps=42.34)
line = a.ctx_label_text.plain
print("   ", line)
chk("rendered to one decimal", "42.3 tok/s" in line)
chk("\U0001F534 LAST on the line -- the label is dock:right, so this IS the right edge",
    line.rstrip().endswith("42.3 tok/s"))
chk("ctx still sits to its left", line.index("ctx 5,087") < line.index("42.3 tok/s"))

print("\n=== the clock starts at the first token, and that token is not counted ===")
s = turnstats.TpsState()
s.start()
chk("a fresh turn clears the clock", s.t0 is None and s.n == 0)
s.tick()
chk("first delta starts the clock and counts nothing (nothing to divide by yet)",
    s.t0 is not None and s.n == 0)
s.tick()
s.tick()
chk("later deltas count", s.n == 2)

print("\n=== the SERVER's count replaces the delta estimate ===")
s = turnstats.TpsState()
s.start()
s.tick()
s.t0 = _t.monotonic() - 2.0          # pretend 2s of generation
s.n = 10                             # the live estimate had counted 10 deltas
rate = s.final(60)                   # the server says 60 real tokens
chk("uses usage.completion_tokens, not the delta count",
    rate is not None and abs(rate - 30.0) < 1.0)
chk("...which is 3x what the delta estimate alone would have shown", rate > 20)

print("\n=== it never divides by zero, and never fabricates a rate ===")
s = turnstats.TpsState()
s.start()
chk("\U0001F534 no first token yet -> returns None rather than inventing a number",
    s.final(100) is None)
s = turnstats.TpsState()
s.start()
s.tick()
chk("server reported 0 completion tokens -> nothing to publish", s.final(0) is None)

print("\n=== repaint throttle: this runs on EVERY token of every turn ===")
s = turnstats.TpsState()
s.start()
s.tick()
s.t0 = _t.monotonic() - 1.0
published = [r for r in [s.tick()] if r is not None]
first_paint = s.painted
for _ in range(50):
    r = s.tick()
    if r is not None:
        published.append(r)
chk("50 further deltas do not repaint 50 times", s.painted == first_paint)
# Stronger than the old `a.tps is not None`: that could not distinguish "one
# publish" from "fifty-one publishes", because every one of them wrote the same
# attribute. Counting what the app WOULD publish can.
chk("...and exactly one value was published, not fifty-one", len(published) == 1)

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
