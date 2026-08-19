"""pccontrol and chrome as tool verbs — the wrappers exist for the traps.

Wrapping a CLI is trivial. What these two wrappers are FOR is translating
failure modes that are silent or misleading, so the tests worth having are the
ones about those, not about the happy paths:

  pccontrol  a failed `activate` does not stop a later paste, so the wrapper
             must tell the caller to stop; and `paste` reports a character count
             that must be checked before pressing Enter.
  chrome     a connection error while idle is NORMAL (python is the server, the
             extension is the client), and `shot` returns an image, which a tool
             result structurally cannot carry.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import chrome_tool
import pccontrol_tool

ok = []


def chk(label, cond):
    ok.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


print("=== pccontrol: the spec warns about both traps ===")
d = pccontrol_tool.PCCONTROL_TOOL_SPEC["function"]["description"]
chk("🔴 warns a failed activate does not stop a paste",
    "does not stop" in d and "focus" in d)
chk("🔴 warns to check paste's character count before Enter",
    "do NOT press Enter" in d and "matches" in d)
chk("tells the model coordinates address windows, titles often cannot",
    "Coordinates address windows" in d)
chk("points at marker for checking aim before clicking", "WITHOUT clicking" in d)

print("\n=== pccontrol: arguments are required, never guessed ===")
for label, args, needle in [
    ("no action", {}, "`action` is required"),
    ("unknown action", {"action": "explode"}, "unknown action"),
    ("click without coords", {"action": "click"}, "`x` and `y` are required"),
    ("marker without coords", {"action": "marker"}, "`x` and `y` are required"),
    ("paste without text", {"action": "paste"}, "`text` is required"),
    ("keypress without key", {"action": "keypress"}, "`key` is required"),
    ("activate without target", {"action": "activate"}, "`target` is required"),
    ("screenshot without monitor", {"action": "screenshot"}, "`monitor` is required"),
]:
    r = pccontrol_tool.run(args)
    chk(f"{label:26s} -> explains", r.startswith("[error]") and needle in r)

print("\n=== pccontrol: nothing raises, whatever it is handed ===")
for args in ({}, {"action": None}, {"action": 42}, {"action": "click", "x": "nope", "y": 1}):
    try:
        r = pccontrol_tool.run(args)
        chk(f"{str(args)[:34]:36s} -> str", isinstance(r, str))
    except Exception as e:
        chk(f"{str(args)[:34]:36s} -> RAISED {type(e).__name__}", False)

print("\n=== pccontrol: no shell, so no argument rewriting ===")
src = Path(pccontrol_tool.__file__).read_text(encoding="utf-8")
chk("🔴 invokes with an argv LIST via ttyguard, never a shell string",
    "ttyguard.run([sys.executable" in src and "shell=True" not in src)
chk("...and the reason is recorded (a leading / gets rewritten by some shells)",
    "leading `/`" in src or "leading /" in src)
chk("screenshot returns a PATH and defers to view_image",
    "Use view_image with that path" in src)

print("\n=== chrome: the idle state is translated, not surfaced raw ===")
chk("recognises a refused connection", chrome_tool._looks_like_no_relay("ERR_CONNECTION_REFUSED"))
chk("...and its python spellings",
    chrome_tool._looks_like_no_relay("Max retries exceeded with url")
    and chrome_tool._looks_like_no_relay("No connection could be made because the target machine actively refused it"))
chk("does NOT fire on an ordinary page error",
    not chrome_tool._looks_like_no_relay("404 Not Found"))
chk("🔴 the hint says it is NORMAL, not a page failure", "NORMAL idle state" in chrome_tool._RELAY_HINT)
chk("...and gives the actual remedy", "bridge.py serve" in chrome_tool._RELAY_HINT)

print("\n=== chrome: shot cannot return the picture ===")
csrc = Path(chrome_tool.__file__).read_text(encoding="utf-8")
d2 = chrome_tool.CHROME_TOOL_SPEC["function"]["description"]
chk("🔴 the spec says shot returns a PATH, not the picture",
    "returns a PATH, not the picture" in d2)
chk("...and names view_image as the way to see it", "view_image" in d2)
chk("the implementation refuses to claim success on a missing file",
    "reported success but wrote nothing" in csrc)

print("\n=== chrome: arguments ===")
for label, args, needle in [
    ("no action", {}, "`action` is required"),
    ("unknown action", {"action": "nope"}, "unknown action"),
    ("nav without url", {"action": "nav"}, "`url` is required"),
    ("click with neither", {"action": "click"}, "`selector`"),
]:
    r = chrome_tool.run(args)
    chk(f"{label:22s} -> explains", r.startswith("[error]") and needle in r)

print("\n=== both go through ttyguard, so neither can wreck the terminal ===")
for name, s in (("pccontrol", src), ("chrome", csrc)):
    chk(f"{name:10s} imports ttyguard", "import ttyguard" in s)
    chk(f"{name:10s} makes no raw subprocess call", "subprocess.run(" not in s and "subprocess.Popen(" not in s)

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
