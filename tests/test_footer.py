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
import app as app_mod

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
    _append_tps_into = app_mod.LiteTUI._append_tps_into
    _tps_start = app_mod.LiteTUI._tps_start
    _tps_tick = app_mod.LiteTUI._tps_tick
    _tps_final = app_mod.LiteTUI._tps_final

    def __init__(self, seat=None, think=None, convo="", used=None, mx=None, tps=None,
                 settings=None):
        # The footer reads per-field toggles now. Default them ALL ON so every
        # assertion below keeps testing the field it was written for rather than
        # accidentally passing because the field is switched off.
        from settings import Settings

        self.settings = settings if settings is not None else Settings()
        self.seat = seat
        self.thinking_level = think
        self.convo_id = convo
        self.ctx_used = used
        self.ctx_max = mx
        self.tps = tps
        self._tps_t0 = None
        self._tps_n = 0
        self._tps_painted = 0.0


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
chk("the label is still addressed by CLASS, never a fixed id",
    'classes="ctx-label"' in src and 'id="ctx-label"' not in src)
chk("_refresh_ctx_label still updates EVERY match (a transient duplicate is cosmetic)",
    'for label in labels:' in src)
chk("changing the thinking level refreshes the footer, not just the header",
    "_update_header" in src and src.count("self._refresh_ctx_label()") >= 4)

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
a = FakeApp()
a._tps_start()
chk("a fresh turn clears the clock", a._tps_t0 is None and a._tps_n == 0)
a._tps_tick()
chk("first delta starts the clock and counts nothing (nothing to divide by yet)",
    a._tps_t0 is not None and a._tps_n == 0)
a._tps_tick()
a._tps_tick()
chk("later deltas count", a._tps_n == 2)

print("\n=== the SERVER's count replaces the delta estimate ===")
a = FakeApp()
a._tps_start()
a._tps_tick()
a._tps_t0 = _t.monotonic() - 2.0     # pretend 2s of generation
a._tps_n = 10                        # the live estimate had counted 10 deltas
a._tps_final(60)                     # the server says 60 real tokens
chk("uses usage.completion_tokens, not the delta count",
    a.tps is not None and abs(a.tps - 30.0) < 1.0)
chk("...which is 3x what the delta estimate alone would have shown", a.tps > 20)

print("\n=== it never divides by zero, and never fabricates a rate ===")
a = FakeApp()
a._tps_start()
a._tps_final(100)
chk("\U0001F534 no first token yet -> stays None rather than inventing a number",
    a.tps is None)
a = FakeApp()
a._tps_start()
a._tps_tick()
a._tps_final(0)
chk("server reported 0 completion tokens -> left alone", a.tps is None)

print("\n=== repaint throttle: this runs on EVERY token of every turn ===")
a = FakeApp()
a._tps_start()
a._tps_tick()
a._tps_t0 = _t.monotonic() - 1.0
a._tps_tick()
first_paint = a._tps_painted
for _ in range(50):
    a._tps_tick()
chk("50 further deltas do not repaint 50 times", a._tps_painted == first_paint)
chk("...and the value is still live", a.tps is not None)

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
