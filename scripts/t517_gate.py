"""T517: only a tool whose schema declares `background` may be backgrounded — flag or auto-promotion."""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
tp = root / "src" / "litetui" / "tasks.py"
t = tp.read_text(encoding="utf-8")
assert "def backgroundable" not in t
anchor = "async def wait_or_promote(aw, seconds: float):\n"
helper = '''@functools.lru_cache(maxsize=None)
def backgroundable(tool: str) -> bool:
    """May this tool run in the background at all?

    Ryan (2026-09-08 13:3x): "not everything should be backgroundable ... only
    what makes sense" — the rule is the SCHEMA: a tool qualifies only if its own
    JSON declares a `background` property (bash, powershell). read/edit/grep are
    instant, ask_user_question waits on the human by design, chrome and pccontrol
    are single steps, studio image generation SUSPENDS the agent's own model while
    it runs (a backgrounded generate would leave the next turn without a model),
    studio sound is already a job. The same gate covers the explicit flag and the
    auto-promotion, so nothing can be backgrounded that was never declared.
    """
    try:
        from litetui import tool_schemas
        spec = tool_schemas.load(tool)
    except Exception:
        return False
    props = ((spec.get("function") or {}).get("parameters") or {}).get("properties") or {}
    return "background" in props


'''
assert anchor in t
t = t.replace(anchor, helper + anchor, 1)
if "import functools" not in t:
    t = t.replace("import contextvars\n", "import contextvars\nimport functools\n", 1)
tp.write_text(t, encoding="utf-8", newline="\n")

ap = root / "src" / "litetui" / "app.py"
a = ap.read_text(encoding="utf-8")
old = '        background = isinstance(args, dict) and bool(args.pop("background", False))\n'
new = ('        # Only a tool whose schema declares `background` may leave the turn (T517,\n'
       '        # Ryan: "not everything should be backgroundable"): the flag on any other\n'
       '        # tool is dropped, and the auto-promotion below never applies to it.\n'
       '        may_bg = tasks_mod.backgroundable(name)\n'
       '        background = may_bg and isinstance(args, dict) and bool(args.pop("background", False))\n'
       '        if isinstance(args, dict):\n'
       '            args.pop("background", None)\n')
assert a.count(old) == 1
a = a.replace(old, new)
old2 = '        limit = int(getattr(self.settings, "tool_auto_background_s", 0) or 0)\n'
new2 = '        limit = int(getattr(self.settings, "tool_auto_background_s", 0) or 0) if may_bg else 0\n'
assert a.count(old2) == 1
a = a.replace(old2, new2)
ap.write_text(a, encoding="utf-8", newline="\n")

tt = root / "tests" / "test_background_tasks.py"
s = tt.read_text(encoding="utf-8")
assert "backgroundable" not in s
s = s.rstrip("\n") + '''


def test_only_a_schema_that_declares_background_is_backgroundable():
    # Ryan: "not everything should be backgroundable ... only what makes sense".
    assert tasks_mod.backgroundable("bash") and tasks_mod.backgroundable("powershell")
    for tool in ("read", "edit", "grep", "write", "ask_user_question", "chrome",
                 "pccontrol", "studio", "web_fetch", "skill", "view_image", "listen", "harness"):
        assert not tasks_mod.backgroundable(tool), tool
    assert not tasks_mod.backgroundable("no-such-tool")
'''
tt.write_text(s, encoding="utf-8", newline="\n")
compile(t, str(tp), "exec")
compile(a, str(ap), "exec")
print("gate patched; tasks.py and app.py compile")
