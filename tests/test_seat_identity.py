"""The seat's fleet identity: a stable NAME across restarts, honestly reported.

Two separate things, and conflating them is the bug this file guards.

  agent_id   DERIVED FROM THE CONVERSATION as of 2026-08-21, by Ryan's
             ruling. This block used to say a per-process id "is CORRECT and
             must stay", and that is now SUPERSEDED -- kept here in full,
             because its reasoning is still the map of what can go wrong.

             The old objections, and what happened to each:

               "convo_id changes WITHIN a process (/new, resume), so identity
               would shift mid-session"
                 -- Half right, and the half it got wrong is the point. Only
                 /new mints a new convo id; /resume restores the existing one,
                 which is exactly why it can be reused. A shift now happens
                 only on a DELIBERATE /new or a switch to another
                 conversation, and _sync_seat_identity handles it: retire the
                 stale row, adopt the new id, let the next heartbeat register.

               "two windows resuming the same conversation would register as
               one agent -- two consumers on one mailbox"
                 -- STILL TRUE AND STILL OPEN. Not a reason to keep minting a
                 new identity every launch, which cost more than it saved: one
                 conversation produced three ids in an evening
                 (ed8ee93e -> 8113984f -> e8a69016), two of them left
                 heartbeating at nothing, and a dispatch sent to the id last
                 seen was silently never delivered. `send` exits 0 either way.

             If the two-window case ever bites, the fix belongs at
             registration -- refuse or re-randomise when the id is already
             held by a DIFFERENT live pid -- not in going back to per-process
             ids. liteharness is ours to change.

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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
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


print("\n=== the seat id follows the conversation (Ryan, 2026-08-21) ===")
app_src = (Path(__file__).resolve().parent.parent / "src" / "app.py").read_text(encoding="utf-8")
chk("the seat id is DERIVED from the conversation, so a resume keeps it",
    "_sync_seat_identity" in app_src and "agent_id_for_convo" in app_src)
chk("...and it is synced from BOTH places convo_id changes, not just one",
    app_src.count("self._sync_seat_identity()") >= 2)
chk("...and new_agent_id stays a fresh uuid4 -- the no-conversation fallback",
    "uuid.uuid4()" in Path(harness_mod.__file__).read_text(encoding="utf-8")
    .split("def new_agent_id", 1)[1].split("def ", 1)[0])
chk("...and the same conversation resolves to the same seat id",
    harness_mod.agent_id_for_convo("abc") == harness_mod.agent_id_for_convo("abc"))
chk("...while different conversations do not collide",
    harness_mod.agent_id_for_convo("abc") != harness_mod.agent_id_for_convo("abd"))

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
