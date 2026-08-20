"""The agent's fleet verbs.

The seat could receive mail from the first version and had no way to answer —
reachable but mute. These assert the tool exists, is gated on registration, and
refuses the two sends that would fail SILENTLY rather than loudly.
"""
import json
import sys
import tempfile
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness as harness_mod

ok = []


def chk(label, cond):
    ok.append(bool(cond))
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

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
