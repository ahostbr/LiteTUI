"""The agent's fleet verbs.

The seat could receive mail from the first version and had no way to answer —
reachable but mute. These assert the tool exists, is gated on registration, and
refuses the two sends that would fail SILENTLY rather than loudly.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import harness as harness_mod

ok = []
#: The labels that FAILED. `ok` is bools, which is all an exit status needed; a
#: pytest arm has to be able to say WHICH check failed, and a bool cannot.
#: Recorded alongside rather than by changing `ok`, so `sum(ok)` / `all(ok)` /
#: `len(ok)` keep meaning exactly what they meant (T700).
failures: list[str] = []


def chk(label, cond):
    ok.append(bool(cond))
    if not cond:
        failures.append(label)
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


def seat(registered=True, agent_id="11111111-1111-1111-1111-111111111111"):
    s = harness_mod.Seat(agent_id=agent_id, name="LiteTUI", model="qwen")
    s.registered = registered
    s.sent = []
    s.send = lambda to, body: (s.sent.append((to, body)) or True)
    return s


print("=== the tool spec is well formed ===")
spec = harness_mod.HARNESS_TOOL_SPEC
fn = spec["function"]
chk("named harness", fn["name"] == "harness")
chk("action is required", fn["parameters"]["required"] == ["action"])
chk("four actions", set(fn["parameters"]["properties"]["action"]["enum"]) ==
    {"whoami", "discover", "send", "check"})
chk("description names send's required args", "`to`" in fn["description"] and "`body`" in fn["description"])
chk("it is JSON-serialisable (it goes on the wire)", json.dumps(spec) and True)

print("\n=== whoami ===")
out = harness_mod.run(seat(), {"action": "whoami"})
chk("reports the agent id", "11111111-1111-1111-1111-111111111111" in out)
chk("reports the name", "LiteTUI" in out)
chk("reports registration state", "registered" in out)
s = seat(registered=False); s.error = "liteharness not installed"
chk("surfaces the error when registration failed", "liteharness not installed" in harness_mod.run(s, {"action": "whoami"}))

# \U0001f534 THE SEND ARMS NEED A REGISTRY, BECAUSE `send` RESOLVES AGAINST ONE NOW.
# 4dc8a36 (T536, "send resolves truncated ids against the registry") put
# `resolve_agent(to_raw)` ahead of every other check, so an id the registry does
# not know returns "[error] send: ..." BEFORE the self-send and unregistered
# branches are ever reached. These fixtures use 1111.../2222..., which no real
# registry contains, so three arms below have been red since that commit and a
# fourth was passing for the wrong reason:
#
#     chk("\U0001f534 self-send is refused", out.startswith("[error]"))
#
# is satisfied by the RESOLVE error just as well as by the self-send one. Only
# the arm that reads the message body noticed. An assertion loose enough to be
# satisfied by a different failure is not a weaker test, it is a test of
# something else.
#
# The fix is the idiom this file already uses for the maildir below: hand the
# module a controlled directory instead of the machine's. That keeps the REAL
# `resolve_agent` in the path — stubbing it out would have made these arms blind
# to the very step that broke them.
_registry = Path(tempfile.mkdtemp(prefix="harness-tool-agents-"))
for _aid in ("11111111-1111-1111-1111-111111111111",
             "22222222-2222-2222-2222-222222222222"):
    (_registry / f"{_aid}.json").write_text(
        json.dumps({"agent_id": _aid, "name": f"seat-{_aid[:4]}"}), encoding="utf-8")
harness_mod.AGENTS_DIR = _registry

print("\n=== send: the two silent failures are refused LOUDLY ===")
s = seat()
out = harness_mod.run(s, {"action": "send", "to": s.agent_id, "body": "hi"})
chk("🔴 self-send is refused", out.startswith("[error]"))
chk("...and says WHY it would vanish", "drop" in out.lower() or "discard" in out.lower())
chk("nothing was actually sent", s.sent == [])

s2 = seat(registered=False)
out = harness_mod.run(s2, {"action": "send", "to": "22222222-2222-2222-2222-222222222222", "body": "hi"})
chk("🔴 unregistered send is refused (no return address)", out.startswith("[error]"))
chk("nothing was sent", s2.sent == [])

print("\n=== send: the happy path and the bad args ===")
s3 = seat()
out = harness_mod.run(s3, {"action": "send", "to": "22222222-2222-2222-2222-222222222222", "body": "hello"})
chk("a real send reports success", out.startswith("sent to 22222222"))
chk("it reached Seat.send", s3.sent == [("22222222-2222-2222-2222-222222222222", "hello")])
chk("missing `to` is an error", harness_mod.run(seat(), {"action": "send", "body": "x"}).startswith("[error]"))
chk("missing `body` is an error", harness_mod.run(seat(), {"action": "send", "to": "abc"}).startswith("[error]"))
chk("whitespace-only body is an error", harness_mod.run(seat(), {"action": "send", "to": "abc", "body": "   "}).startswith("[error]"))

print("\n=== check: polls without waiting for the monitor ===")
box = Path(tempfile.mkdtemp(prefix="harness-tool-"))
harness_mod.NEW, harness_mod.DONE = box / "new", box / "done"
harness_mod.NEW.mkdir(parents=True)

# The runner arms LITETUI_NO_HARNESS for every child process, and poll() now
# refuses under it: a disabled seat must not consume mail it will never deliver.
# What follows tests poll's DELIVERY LOGIC, not the gate, so it opts out -- the
# same move test_no_fleet_registration's control arms make for register().
#
# 🔴 THE REDIRECT IS ASSERTED FIRST, AND THAT ORDER IS THE WHOLE POINT. Clearing
# the guard while NEW still pointed at the real maildir would make this file eat
# live fleet mail -- the precise hazard the guard was added for. An opt-out that
# is safe only because someone remembered to redirect first is luck; this makes
# it a precondition.
assert harness_mod.NEW != Path.home() / ".liteharness" / "inbox" / "new", (
    "refusing to un-gate poll() while NEW points at the live maildir"
)
_guard_was = os.environ.pop(harness_mod.NO_HARNESS_ENV, None)

s4 = seat()
chk("empty inbox says so, does not error", harness_mod.run(s4, {"action": "check"}) == "(no new messages)")
import datetime as dt
(harness_mod.NEW / "m.json").write_text(json.dumps({
    "from": "22222222-2222-2222-2222-222222222222", "to": s4.agent_id, "body": "ping",
    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "priority": "urgent",
}), encoding="utf-8")
out = harness_mod.run(s4, {"action": "check"})
chk("a real message is returned", "ping" in out)
chk("...with its sender", "22222222" in out)

print("\n=== unknown action ===")
out = harness_mod.run(seat(), {"action": "explode"})
chk("refused", out.startswith("[error]"))
chk("...and lists the valid actions", all(a in out for a in ("whoami", "discover", "send", "check")))
chk("missing action is refused too", harness_mod.run(seat(), {}).startswith("[error]"))

print("\n=== nothing raises — every path returns text ===")
for args in ({}, {"action": None}, {"action": "send"}, {"action": "check"}, {"action": 123}):
    try:
        r = harness_mod.run(seat(), args if isinstance(args, dict) else {})
        chk(f"{str(args)[:28]:30s} -> str", isinstance(r, str))
    except Exception as e:
        chk(f"{str(args)[:28]:30s} -> RAISED {type(e).__name__}", False)
# Re-arm the guard for anything below this section.
if _guard_was is not None:
    os.environ[harness_mod.NO_HARNESS_ENV] = _guard_was


# ── the same checks, as a pytest arm (T700) ─────────────────────────────
#
# 🔴 THIS FILE IS NAMED `test_*` AND NOTHING HAS EVER RUN IT. A module-level
# `sys.exit` raises SystemExit during collection, which pytest reports as
# INTERNALERROR and which abandons the WHOLE invocation — not just this file.
# Ten files in this directory were in that state (T699 fixed two, T700 the
# rest); each abort hid the others, which is why the class kept looking small.
#
# 🔴 AND THE `_guard_was` BLOCK ABOVE HAD NEVER RUN EITHER. It sat AFTER the
# exit, so the "anything below this section" whose environment it restores was
# unreachable in both modes. It is hoisted above the tally rather than left to
# become reachable by accident: leaving NO_HARNESS_ENV unrestored would leak
# env state into every test that follows this one in the same process.


def test_every_check_in_this_file_passed() -> None:
    assert ok, "no check ran — the body above did not execute"
    assert failures == [], failures


if __name__ == "__main__":
    print(f"\n{sum(ok)}/{len(ok)} passed")
    sys.exit(0 if all(ok) else 1)
