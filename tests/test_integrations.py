"""Skills, MCP and the harness seat.

The MCP test spawns a REAL stdio server subprocess and speaks real JSON-RPC to
it. A mocked transport would prove the parser and nothing about the protocol,
and the protocol is the part that can be wrong.
"""
import json
import os
import sys
import tempfile
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import harness as harness_mod
from litetui import mcp_client
from litetui import skills as skills_mod

ok = []


def chk(label, cond):
    ok.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


# ─────────────────────────────── skills ────────────────────────────────
print("=== skills: discovery and the index/body split ===")
root = Path(tempfile.mkdtemp(prefix="litetui-skills-"))
sk = root / "skills"
(sk / "deploy").mkdir(parents=True)
(sk / "deploy" / "SKILL.md").write_text(
    "---\nname: deploy\ndescription: Ship a release safely\n---\n\n# Deploy\n\nStep one.\n",
    encoding="utf-8",
)
(sk / "no-frontmatter").mkdir(parents=True)
(sk / "no-frontmatter" / "SKILL.md").write_text("# Bare\n\nBody only.\n", encoding="utf-8")
(sk / "not-a-skill").mkdir(parents=True)  # no SKILL.md
(sk / "verbose").mkdir(parents=True)
(sk / "verbose" / "SKILL.md").write_text(
    "---\nname: verbose\ndescription: " + ("x" * 500) + "\n---\nbody\n", encoding="utf-8"
)

found = skills_mod.discover(root)
names = [s.name for s in found]
chk("found the 3 real skills", len(found) == 3)
chk("a dir with no SKILL.md is skipped", "not-a-skill" not in names)
chk("frontmatter name is used", "deploy" in names)
chk("missing frontmatter falls back to the dir name", "no-frontmatter" in names)
chk("description parsed", any(s.description == "Ship a release safely" for s in found))
chk("runaway description is capped", all(len(s.description) <= skills_mod.MAX_DESC_CHARS for s in found))

idx = skills_mod.index_block(found)
chk("index names every skill", all(n in idx for n in names))
chk("index does NOT inline the body", "Step one." not in idx)
chk("index tells the model how to open one", "`skill`" in idx)
chk("no skills -> empty index, not a stub header", skills_mod.index_block([]) == "")

body = skills_mod.load(found, "deploy")
chk("load returns the real body", "Step one." in body)
chk("load is case-insensitive", "Step one." in skills_mod.load(found, "DEPLOY"))
miss = skills_mod.load(found, "nope")
chk("unknown name is an error", miss.startswith("[error]"))
chk("...and lists what IS available", "deploy" in miss)
chk("empty name is refused", skills_mod.load(found, "").startswith("[error]"))

# ──────────────────────────────── MCP ──────────────────────────────────
print("\n=== MCP: a real stdio server, real JSON-RPC ===")
mroot = Path(tempfile.mkdtemp(prefix="litetui-mcp-"))
server = mroot / "fake_server.py"
server.write_text(textwrap.dedent('''
    import json, sys
    def send(o): sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    # A banner on stdout: real servers do this and it must not be read as a frame.
    send({"jsonrpc": "2.0", "method": "notifications/hello", "params": {}})
    sys.stdout.write("not json at all\\n"); sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        m = json.loads(line)
        mid, meth = m.get("id"), m.get("method")
        if meth == "initialize":
            send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2025-06-18",
                  "capabilities":{},"serverInfo":{"name":"fake","version":"1"}}})
        elif meth == "tools/list":
            send({"jsonrpc":"2.0","id":mid,"result":{"tools":[
                {"name":"echo","description":"Echo text back",
                 "inputSchema":{"type":"object","properties":{"text":{"type":"string"}},
                                "required":["text"]}},
                {"name":"boom","description":"Always errors",
                 "inputSchema":{"type":"object","properties":{}}}]}})
        elif meth == "tools/call":
            p = m.get("params") or {}
            if p.get("name") == "boom":
                send({"jsonrpc":"2.0","id":mid,"error":{"code":-32000,"message":"kaboom"}})
            else:
                txt = (p.get("arguments") or {}).get("text","")
                send({"jsonrpc":"2.0","id":mid,"result":{"content":[
                    {"type":"text","text":"echo: "+txt},
                    {"type":"image","data":"xxx"}]}})
'''), encoding="utf-8")

(mroot / "mcp.json").write_text(json.dumps({"mcpServers": {
    "fake": {"command": sys.executable, "args": [str(server)]},
    "broken": {"command": "definitely-not-a-real-binary-xyz", "args": []},
    "off": {"command": sys.executable, "args": [str(server)], "disabled": True},
}}), encoding="utf-8")

mgr = mcp_client.MCPManager(mroot)
mgr.load()
try:
    chk("the good server started", "fake" in mgr.servers)
    chk("a broken server does NOT take down the others", "fake" in mgr.servers and "broken" in mgr.failures)
    chk("a broken server is RECORDED, not swallowed", bool(mgr.failures.get("broken")))
    chk("disabled server is skipped", "off" not in mgr.servers)
    chk("banner + non-JSON stdout did not break initialize", len(mgr.servers["fake"].tools) == 2)

    specs = mgr.tool_specs()
    spec_names = [s["function"]["name"] for s in specs]
    chk("tools are namespaced by server", "mcp__fake__echo" in spec_names)
    chk("the disabled server contributes nothing", not any("off" in n for n in spec_names))
    echo_spec = next(s for s in specs if s["function"]["name"] == "mcp__fake__echo")
    chk("the server's own inputSchema is passed through",
        echo_spec["function"]["parameters"].get("required") == ["text"])

    disp = mgr.dispatch()
    out = disp["mcp__fake__echo"]({"text": "hi"})
    chk("a real tools/call round-trips", "echo: hi" in out)
    chk("a non-text content block is named, not dropped", "image" in out)
    err = disp["mcp__fake__boom"]({})
    chk("a server-side error becomes a tool error string", err.startswith("[error]"))
    chk("...naming the server and tool", "fake" in err and "boom" in err)
    chk("an error does not kill the server", "echo: again" in disp["mcp__fake__echo"]({"text": "again"}))

    log = mroot / mcp_client.MCP_LOG_NAME
    chk("stderr/noise went to a log file, not the terminal", log.exists())
finally:
    mgr.stop_all()
chk("stop_all terminated the child", mgr.servers["fake"].proc.poll() is not None)

print("\n=== MCP: no mcp.json is a no-op, not an error ===")
empty_root = Path(tempfile.mkdtemp(prefix="litetui-nomcp-"))
m2 = mcp_client.MCPManager(empty_root)
m2.load()
chk("no config -> no servers", m2.servers == {})
chk("no config -> no failures either", m2.failures == {})
chk("no config -> no tools", m2.tool_specs() == [])

# ────────────────────────────── harness ────────────────────────────────
print("\n=== harness seat: the ONLY-MY-MAIL rule ===")
box = Path(tempfile.mkdtemp(prefix="litetui-inbox-"))
harness_mod.INBOX_ROOT = box
harness_mod.NEW, harness_mod.CUR, harness_mod.DONE = box / "new", box / "cur", box / "done"
harness_mod.NEW.mkdir(parents=True)
harness_mod.DONE.mkdir(parents=True)

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
seat = harness_mod.Seat(agent_id=ME, name="LiteTUI", model="m")


def put(fn, **kw):
    msg = {"id": fn, "from": OTHER, "to": ME, "body": "hello",
           "timestamp": datetime.now(timezone.utc).isoformat(),
           "priority": "normal", "ttl_minutes": 60}
    msg.update(kw)
    (harness_mod.NEW / fn).write_text(json.dumps(msg), encoding="utf-8")


put("mine.json")
put("theirs.json", to=OTHER)
put("broadcast.json", to=None)
put("myecho.json", **{"from": ME})
put("stale.json", timestamp=(datetime.now(timezone.utc) - timedelta(minutes=180)).isoformat())
(harness_mod.NEW / "garbage.json").write_text("{not json", encoding="utf-8")

got = seat.poll()
bodies = [m["id"] for m in got]
chk("my message was delivered", "mine.json" in bodies)
chk("a broadcast was delivered", "broadcast.json" in bodies)
chk("🔴 ANOTHER AGENT'S MAIL WAS NOT DELIVERED", "theirs.json" not in bodies)
chk("🔴 ...and is STILL IN new/, unclaimed", (harness_mod.NEW / "theirs.json").exists())
chk("my own echo is skipped", "myecho.json" not in bodies)
chk("an expired message is not delivered", "stale.json" not in bodies)
chk("...but IS cleared from new/", not (harness_mod.NEW / "stale.json").exists())
chk("unparseable file is left alone, not crashed on", (harness_mod.NEW / "garbage.json").exists())
chk("delivered mail moved out of new/", not (harness_mod.NEW / "mine.json").exists())
chk("delivered mail landed in done/", (harness_mod.DONE / "mine.json").exists())
chk("a second poll re-delivers nothing", seat.poll() == [])

print("\n=== harness: formatting + graceful absence ===")
f = harness_mod.format_message({"from": OTHER, "priority": "urgent", "body": "ping"})
chk("format names the sender", OTHER[:8] in f)
chk("format carries the body", "ping" in f)
chk("format shows priority", "urgent" in f)

gone = Path(tempfile.mkdtemp(prefix="litetui-noinbox-")) / "nope"
harness_mod.NEW = gone
chk("a missing inbox is empty, not an exception", harness_mod.Seat("x", "y", "z").poll() == [])

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
