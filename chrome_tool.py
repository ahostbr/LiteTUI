"""Ryan's real Chrome as tool verbs, with its two confusing states explained.

chrome-bridge/bridge.py already has the verbs (ping, tabs, nav, text, click,
shot). Wrapping them is trivial; what earns its keep here is translating two
states that read as failures and are not:

  1. ERR_CONNECTION_REFUSED WHEN IDLE IS NORMAL. Python is the SERVER and the
     Chrome extension is the CLIENT. With no relay running, a connection error
     is the expected state, not a page failure — but an agent reads
     "connection refused" as "the site is down" and starts debugging the wrong
     thing. Translated here into the actual remedy.

  2. `shot` RETURNS AN IMAGE, AND A TOOL RESULT CANNOT CARRY ONE. Same
     structural fact as view_image: a tool result is a role:"tool" message whose
     content is a STRING. So `shot` returns a PATH and tells the model to open it
     with view_image, which stages it into a role:"user" turn.

Every child runs through ttyguard, so nothing here can leave the TUI's terminal
in mouse-reporting mode or take its stdin.
"""

from __future__ import annotations

import sys
from pathlib import Path

import ttyguard

ROOT = Path(__file__).parent
# Lives under tools/ since 2026-08-20. The gate below is
# `SCRIPT.exists()`: a wrong path does not error, it removes the tool
# from the model's list entirely — the capability just stops existing,
# with nothing said. Moving this directory REQUIRES editing this line.
SCRIPT = ROOT / "tools" / "chrome-bridge" / "bridge.py"
SHOT_DIR = ROOT / "chrome-bridge"

ACTIONS = ("ping", "tabs", "nav", "text", "click", "shot")

_RELAY_HINT = (
    "\n[the relay is not running — this is the NORMAL idle state, not a page "
    "failure. python is the server and the Chrome extension is the client. "
    "Start it with: python chrome-bridge/bridge.py serve]"
)

CHROME_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "chrome",
        "description": (
            "Drive Ryan's real Chrome browser. Actions: ping (is the relay up) - "
            "tabs (list open tabs) - nav (needs url, optional new_tab) - text "
            "(page text, optional css selector) - click (css selector, or x and y) - "
            "shot (screenshot to a file). "
            "shot returns a PATH, not the picture — open it with the view_image "
            "tool to actually see it. "
            "A connection error when nothing is running is the normal idle state, "
            "not a broken page: the relay has to be started first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "url": {"type": "string", "description": "URL for nav"},
                "new_tab": {"type": "boolean", "description": "nav: open in a new tab"},
                "selector": {"type": "string", "description": "CSS selector for text/click"},
                "x": {"type": "integer", "description": "click by coordinate instead of selector"},
                "y": {"type": "integer", "description": "click by coordinate instead of selector"},
            },
            "required": ["action"],
        },
    },
}


def _looks_like_no_relay(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in (
        "connection refused", "err_connection_refused", "actively refused",
        "connectionrefused", "max retries exceeded", "failed to establish",
    ))


def _run(argv: list[str], timeout: int = 90) -> str:
    try:
        r = ttyguard.run([sys.executable, str(SCRIPT), *argv], timeout=timeout)
    except Exception as e:
        return f"[error] chrome: {type(e).__name__}: {e}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0 or _looks_like_no_relay(out):
        msg = f"[error] chrome exit {r.returncode}: {out or '(no output)'}"
        if _looks_like_no_relay(out):
            msg += _RELAY_HINT
        return msg
    return out or "(no output)"


def run(args: dict) -> str:
    """Dispatch the `chrome` tool. Never raises — every path returns text."""
    action = str(args.get("action") or "").strip().lower()
    if not action:
        return f"[error] chrome: `action` is required. Valid: {', '.join(ACTIONS)}"
    if action not in ACTIONS:
        return f"[error] chrome: unknown action {action!r}. Valid: {', '.join(ACTIONS)}"
    if not SCRIPT.exists():
        return f"[error] chrome: bridge not found at {SCRIPT}"

    if action in ("ping", "tabs"):
        return _run([action])

    if action == "nav":
        url = args.get("url")
        if not url:
            return "[error] chrome nav: `url` is required"
        argv = ["nav", str(url)]
        if args.get("new_tab"):
            argv.insert(1, "--new-tab")
        return _run(argv)

    if action == "text":
        argv = ["text"]
        if args.get("selector"):
            argv.append(str(args["selector"]))
        return _run(argv)

    if action == "click":
        if args.get("selector"):
            return _run(["click", str(args["selector"])])
        if args.get("x") is not None and args.get("y") is not None:
            try:
                x, y = int(str(args["x"]).strip()), int(str(args["y"]).strip())
            except (TypeError, ValueError):
                return (f"[error] chrome click: x and y must be whole numbers, got "
                        f"{args.get('x')!r} and {args.get('y')!r}")
            return _run(["click", "--x", str(x), "--y", str(y)])
        return "[error] chrome click: give a `selector`, or both `x` and `y`"

    # shot: a PATH, never the bytes. See the module docstring.
    out_path = SHOT_DIR / "chrome-shot.png"
    res = _run(["shot", str(out_path)], timeout=120)
    if res.startswith("[error]"):
        return res
    if not out_path.exists():
        return f"[error] chrome shot: reported success but wrote nothing to {out_path}"
    return (
        f"Saved {out_path}. Use view_image with that path to actually look at it — "
        "this result is text and cannot carry the picture."
    )
