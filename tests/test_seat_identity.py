"""The seat's fleet identity: a stable NAME across restarts, honestly reported.

Two separate things, and conflating them is the bug this file guards.

  agent_id   one per PROCESS. Minted fresh each launch, persisted nowhere.
             That is CORRECT and must stay. The conversation id cannot be
             reused for it: convo_id changes WITHIN a process (/new, resume),
             so identity would shift mid-session, and two windows resuming the
             same conversation would register as one agent -- two consumers on
             one mailbox, the exact defect harness.py exists to avoid.

  name       the durable handle a human or a peer writes down. Without
             --takeover it could not survive a restart, because the previous
             process still held it, so the registry issued a random one.
             Measured on the live roster 2026-08-19: SIX rows for one seat --
             LiteTUI, BlackGrid, HazeCrypt, PrimeWard, HotPack, CyanWedge.

And the name we DISPLAY must be the name the fleet actually knows. `self.name`
is only what we asked for.
"""
import os
import sys
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness as harness_mod

ok = []


def chk(label, cond):
    ok.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


print("=== register asks to reclaim its own name ===")
src = Path(harness_mod.__file__).read_text(encoding="utf-8")
# Slice from _presence_argv, not from register: the argv moved into that helper
# when heartbeat() started sharing it, and a source-slice test breaks on a
# refactor that changed no behaviour. Kept as source inspection only for the
# flag itself; the NAME is now checked behaviourally below, which is what the
# assertion was always trying to prove.
reg = src.split("def _presence_argv", 1)[1].split("def deregister", 1)[0]
chk("--takeover is passed", '"--takeover"' in reg)
chk("...and the requested name is still passed with it",
    "--name" in harness_mod.Seat(agent_id="i", name="Asked", model="m")._presence_argv())
chk("...and it is the name that was ASKED for",
    "Asked" in harness_mod.Seat(agent_id="i", name="Asked", model="m")._presence_argv())
chk("deregister does NOT take over (it is a teardown, not a claim)",
    "--takeover" not in src.split("def deregister", 1)[1].split("def _addressed_to_me", 1)[0])


print("\n=== the resolved name is parsed out of register's own output ===")
LINE = ("Registered agent e6b4ea65-dd14-448b-b9cb-bc9f1b9721a5: "
        "cli=litetui, model=qwen3.8-27b, tier=worker, name=CyanWedge")
chk("reads the name the REGISTRY assigned", harness_mod._resolved_name(LINE) == "CyanWedge")
chk("...even with a team suffix after it",
    harness_mod._resolved_name(LINE + ", team=fleet") == "CyanWedge")
chk("...and with a spatial suffix",
    harness_mod._resolved_name(LINE + " @canvas-pane-3") == "CyanWedge")
chk("multi-line output still finds it",
    harness_mod._resolved_name("some banner\n" + LINE + "\ntrailing") == "CyanWedge")

print("\n=== it degrades to the requested name, never to an exception ===")
chk("empty output -> None", harness_mod._resolved_name("") is None)
chk("None -> None", harness_mod._resolved_name(None) is None)
chk("a format change -> None (keep what we asked for, do not crash on startup)",
    harness_mod._resolved_name("Registered agent abc: all good") is None)
chk("a bare trailing 'name=' -> None, not an empty string",
    harness_mod._resolved_name("x name=") is None)


print("\n=== the seat adopts what it was actually given ===")
class FakeResult:
    def __init__(self, out, rc=0):
        self.stdout = out
        self.stderr = ""
        self.returncode = rc


def seat_with(result):
    s = harness_mod.Seat(agent_id="11111111-1111-1111-1111-111111111111",
                         name="LiteTUI", model="qwen")
    harness_mod.ttyguard.run = lambda *a, **k: result
    return s


import types
_real_run = harness_mod.ttyguard.run

# The guard that stops the SUITE writing to the live registry
# (LITETUI_NO_HARNESS, see tests/test_no_fleet_registration.py) returns from
# register() before ttyguard is consulted. These assertions drive the SUCCESS
# path against a stub, so lift it here and restore it below.
#
# Lifted for this block ONLY. Clearing it for the whole file would re-open the
# hole for anything added underneath, silently.
_guard = os.environ.pop(harness_mod.NO_HARNESS_ENV, None)
try:
    s = seat_with(FakeResult(LINE))
    s.register()
    chk("🔴 asked for LiteTUI, registry said CyanWedge -> the seat reports CyanWedge",
        s.name == "CyanWedge")
    chk("...and is registered", s.registered is True)

    s = seat_with(FakeResult("Registered agent abc: cli=litetui, name=LiteTUI"))
    s.register()
    chk("got the name it asked for -> unchanged", s.name == "LiteTUI")

    s = seat_with(FakeResult("banner with no name field"))
    s.register()
    chk("unparseable output -> keeps the requested name rather than blanking it",
        s.name == "LiteTUI")

    s = seat_with(FakeResult("", rc=1))
    s.register()
    chk("🔴 registration FAILED -> name is not adopted from a failed run",
        s.registered is False and s.name == "LiteTUI")
    chk("...and the failure is recorded", bool(s.error))
finally:
    harness_mod.ttyguard.run = _real_run
    if _guard is not None:
        os.environ[harness_mod.NO_HARNESS_ENV] = _guard


print("\n=== the two ids stay separate, deliberately ===")
app_src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
chk("the agent id is minted per process, not taken from the conversation",
    "agent_id=harness_mod.new_agent_id()" in app_src)
chk("...and new_agent_id is a fresh uuid4, not derived from convo_id",
    "uuid.uuid4()" in Path(harness_mod.__file__).read_text(encoding="utf-8")
    .split("def new_agent_id", 1)[1].split("def ", 1)[0])

print("\n=== 🔴 THE LIVENESS GUARD DOES NOT PROTECT THIS SEAT -- measured ===")
# --takeover is documented to refuse a GENUINELY LIVE holder. That guard reads
# presence.session_pid, and _agent_record_live treats a falsy session_pid as NOT
# live. session_pid is written by liteharness.hooks and NEVER by
# `liteharness.cli register` -- the path this seat uses. So a LiteTUI seat always
# reads as a ghost to the takeover check.
#
# Proven end to end against the real registry 2026-08-19: two live probes, and
# the second took the name from the first.
#
# Consequences, written down so nobody re-derives them optimistically:
#   * Two LiteTUI windows at once WILL trade the name. Cosmetic only -- mail is
#     addressed by agent_id, never by name, so nothing is misdelivered.
#   * The same missing field is why dead rows pile up: the janitor's dead-owner
#     purge keys on session_pid too. Four LiteTUI ghosts were on the roster.
# The real fix belongs in liteharness-oss (record a session_pid on CLI
# registration). Filed there, not worked around here.
_h = Path(harness_mod.__file__).read_text(encoding="utf-8")
chk("the limitation is recorded next to the flag that has it", "session_pid" in _h)
chk("mail is addressed by agent_id, so a traded name misdelivers nothing",
    "_addressed_to_me" in _h and "to == self.agent_id" in _h)

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
