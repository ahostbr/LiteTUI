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

sys.path.insert(0, str(Path(__file__).parent))
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

    def __init__(self, seat=None, think=None, convo="", used=None, mx=None):
        self.seat = seat
        self.thinking_level = think
        self.convo_id = convo
        self.ctx_used = used
        self.ctx_max = mx


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

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
