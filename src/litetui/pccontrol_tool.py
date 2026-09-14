"""Desktop control as ONE tool verb, with the two traps built into the wrapper.

pccontrol/pccontrol.py is a capable CLI and Ryan was pointing the agent at the
directory by hand. Wrapping it is easy; wrapping it SAFELY is the point, because
two of its failure modes are silent and both have already cost a real misfire:

  1. A FAILED `activate` DOES NOT STOP A SUBSEQUENT PASTE. The helper types into
     whatever window happens to hold focus, so a wrong-window paste looks exactly
     like a right-window paste. A precondition that fails without halting the
     action is not a precondition -- so `activate` here returns a result the
     caller must branch on, and the model is told so in the tool description.

  2. `paste` REPORTS `pasted N chars`. Refuse Enter unless N is what you sent.
     The canonical incident: `/compact` arrived as `C:/Program Files/Git/compact`
     -- 28 chars, not 8 -- because a shell rewrote a leading slash before the
     transport saw it, and four terminals received the mangled string. This
     wrapper never goes through a shell (argv list, shell=False), which removes
     that specific rewrite, and it still reports the count so the caller can
     check.

Every child runs through ttyguard, so a subprocess cannot leave the TUI's
terminal in mouse-reporting mode or steal its stdin.
"""

from __future__ import annotations

import sys
from pathlib import Path

from litetui import paths
from litetui import ttyguard
from litetui import tool_schemas

ROOT = Path(__file__).resolve().parent.parent.parent  # repo root; src/ is below it
# Lives under tools/ since 2026-08-20. The gate below is
# `SCRIPT.exists()`: a wrong path does not error, it removes the tool
# from the model's list entirely — the capability just stops existing,
# with nothing said. Moving this directory REQUIRES editing this line.
SCRIPT = ROOT / "tools" / "pccontrol" / "pccontrol.py"
SCREENSHOT = ROOT / "tools" / "pccontrol" / "screenshot.ps1"

#: Verbs that move the mouse or press keys. Grouped so the description can warn
#: about them as a class rather than one at a time.
_POINT_ACTIONS = ("click", "doubleclick", "rightclick", "marker")
_TEXT_ACTIONS = ("type", "paste", "settext")
_BARE_ACTIONS = ("windows", "status")

ACTIONS = _POINT_ACTIONS + _TEXT_ACTIONS + _BARE_ACTIONS + (
    "keypress", "activate", "launch", "screenshot",
)

PCCONTROL_TOOL_SPEC = tool_schemas.load("pccontrol")


def _int(value, field: str) -> tuple[int | None, str | None]:
    """Coerce a model-supplied number. Returns (value, error).

    A model will hand you "500px", "nope", or None for a coordinate. int() on
    those raises ValueError from inside a function that promises to return text,
    which turns a bad argument into a crashed turn. Found by the wrapper's own
    test, not by review.
    """
    if value is None:
        return None, f"[error] pccontrol: `{field}` is required"
    try:
        return int(str(value).strip()), None
    except (TypeError, ValueError):
        return None, f"[error] pccontrol: `{field}` must be a whole number, got {value!r}"


def _run(argv: list[str], timeout: int = 60) -> str:
    """Invoke the CLI with an argv LIST -- never a shell string.

    shell=False is not a style choice. A leading `/` in an argument is rewritten
    by MSYS-flavoured shells before the callee ever sees it, which is how
    `/compact` became a 28-character path. No shell, no rewrite.
    """
    try:
        r = ttyguard.run([sys.executable, str(SCRIPT), *argv], timeout=timeout)
    except Exception as e:
        return f"[error] pccontrol: {type(e).__name__}: {e}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        return f"[error] pccontrol exit {r.returncode}: {out or '(no output)'}"
    return out or "(no output)"


def run(args: dict) -> str:
    """Dispatch the `pccontrol` tool. Never raises -- every path returns text."""
    action = str(args.get("action") or "").strip().lower()
    if not action:
        return f"[error] pccontrol: `action` is required. Valid: {', '.join(ACTIONS)}"
    if action not in ACTIONS:
        return f"[error] pccontrol: unknown action {action!r}. Valid: {', '.join(ACTIONS)}"
    if not SCRIPT.exists():
        return f"[error] pccontrol: helper not found at {SCRIPT}"

    def _flags() -> list[str]:
        f: list[str] = []
        mon, mon_err = _int(args.get("monitor"), "monitor") if args.get("monitor") is not None else (None, None)
        if mon is not None and not mon_err:
            f += ["--mon", str(mon)]
        if args.get("label"):
            f += ["--label", str(args["label"])]
        if args.get("color"):
            f += ["--color", str(args["color"])]
        ms, ms_err = _int(args.get("ms"), "ms") if args.get("ms") is not None else (None, None)
        if ms is not None and not ms_err:
            f += ["--ms", str(ms)]
        return f

    if action == "screenshot":
        mon = args.get("monitor")
        if mon is None:
            return "[error] pccontrol screenshot: `monitor` is required (0, 1, 2 ...)"
        if not SCREENSHOT.exists():
            return f"[error] pccontrol: screenshot helper not found at {SCREENSHOT}"
        mon_i, err = _int(mon, "monitor")
        if err:
            return err
        shot = paths.data_root() / "pccontrol" / f"mon{mon_i}.jpg"
        shot.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = ttyguard.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(SCREENSHOT), "-Monitor", str(mon_i), "-Output", str(shot)],
                timeout=90,
            )
        except Exception as e:
            return f"[error] pccontrol screenshot: {type(e).__name__}: {e}"
        if r.returncode != 0 or not shot.exists():
            return f"[error] pccontrol screenshot exit {r.returncode}: {(r.stderr or r.stdout or '').strip()[:200]}"
        # A path, not the picture: a tool result is a string and cannot carry an
        # image. Hand it to view_image, which stages it through a user turn.
        return f"Saved {shot}. Use view_image with that path to actually look at it."

    if action in _POINT_ACTIONS:
        if args.get("x") is None or args.get("y") is None:
            return f"[error] pccontrol {action}: `x` and `y` are required"
        x, err = _int(args.get("x"), "x")
        if err:
            return err
        y, err = _int(args.get("y"), "y")
        if err:
            return err
        return _run([action, str(x), str(y)] + _flags())

    if action in _TEXT_ACTIONS:
        text = args.get("text")
        if text is None or str(text) == "":
            return f"[error] pccontrol {action}: `text` is required"
        out = _run([action, str(text)])
        # Surface the length the caller must check against, rather than making
        # them parse it back out of prose.
        if action == "paste" and not out.startswith("[error]"):
            out += f"\n[sent {len(str(text))} chars — refuse Enter unless the count above matches]"
        return out

    if action == "keypress":
        key = args.get("key")
        if not key:
            return "[error] pccontrol keypress: `key` is required (e.g. Enter, ctrl+c)"
        return _run(["keypress", str(key)])

    if action in ("activate", "launch"):
        target = args.get("target")
        if not target:
            return f"[error] pccontrol {action}: `target` is required"
        out = _run([action, str(target)])
        if action == "activate" and out.startswith("[error]"):
            out += (
                "\n[activate FAILED — do NOT paste or type now. It would go to whatever "
                "window currently has focus. Screenshot, then marker, then click instead: "
                "several windows can share one process, so a title often cannot address them.]"
            )
        return out

    return _run([action])
