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

import os
import sys
from pathlib import Path

import ttyguard

ROOT = Path(__file__).resolve().parent.parent  # repo root; src/ is below it
# Lives under tools/ since 2026-08-20. The gate below is
# `SCRIPT.exists()`: a wrong path does not error, it removes the tool
# from the model's list entirely — the capability just stops existing,
# with nothing said. Moving this directory REQUIRES editing this line.
SCRIPT = ROOT / "tools" / "chrome-bridge" / "bridge.py"
RELAY_DIR = SCRIPT.parent
# 🔴 THIS LINE WAS LEFT BEHIND BY THE SAME MOVE, four lines under the comment
# warning about it. SCRIPT was repointed and SHOT_DIR was not, so `shot` wrote
# to a directory that does not exist — the action has been broken since. The
# warning above was written and then not applied to the constant beneath it.
SHOT_DIR = RELAY_DIR

ACTIONS = (
    "ping", "tabs", "nav", "text", "click", "write_text", "shot",
    "start", "stop", "status",
)

_RELAY_HINT = (
    "\n[the relay is not running — this is the NORMAL idle state, not a page "
    "failure. Python is the server and the Chrome extension is the client, so "
    "with no relay up a connection error is expected. Fix it yourself: call "
    "this same tool with action=\"start\", then retry.]"
)

CHROME_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "chrome",
        "description": (
            "Drive Ryan's real Chrome browser. Actions: start / stop / status "
            "(the relay this tool talks through) - ping (is the extension "
            "answering) - tabs (list open tabs) - nav (needs url, optional "
            "new_tab) - text (page text, optional css selector) - click (css "
            "selector, or x and y) - write_text (type into a field: needs text, "
            "optional selector, clear, enter) - shot (screenshot to a file). "
            "shot returns a PATH, not the picture — open it with the view_image "
            "tool to actually see it. "
            "A connection error when nothing is running is the normal idle state, "
            "not a broken page — call action=start and retry. "
            "write_text with no selector types into whatever is focused, so "
            "click then write_text works on fields with no stable selector."
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
                "text": {"type": "string", "description": "write_text: what to type"},
                "clear": {
                    "type": "boolean",
                    "description": "write_text: replace the field (default true); false appends",
                },
                "enter": {
                    "type": "boolean",
                    "description": "write_text: press Enter afterwards (submits most forms)",
                },
            },
            "required": ["action"],
        },
    },
}


# The previous wording opened "the relay is running but ..." — and this same
# string fires on the DIRECT-mode path, where the relay is deliberately stopped.
# A hint that states a wrong fact sends the reader to the wrong half of the
# system, which is the opposite of what this module exists for.
_NO_EXT_HINT = (
    "\n[no Chrome extension is answering — this is the EXTENSION half, not the "
    "Python half. Often transient right after action=\"start\" or an extension "
    "reload: it reconnects on a backoff, and an MV3 service worker that has been "
    "evicted only wakes on its 30s keepalive alarm, so wait ~35s before "
    "concluding anything. If it persists the extension needs a manual Reload at "
    "chrome://extensions, which nothing here can do — extensions cannot script "
    "chrome:// pages.]"
)


def _looks_like_no_extension(text: str) -> bool:
    return "no extension connected" in text.lower()


def _last_error_line(text: str) -> str:
    """The cause, not the stack.

    bridge.py surfaces failures as an uncaught ChromeError, so its stderr is a
    dozen frames ending in one line that says what happened. Handing all of it
    to a model buries the cause in noise it cannot act on.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if "Error:" in ln and not ln.startswith(("File ", "Traceback")):
            return ln
    return text


def _looks_like_no_relay(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in (
        "connection refused", "err_connection_refused", "actively refused",
        "connectionrefused", "max retries exceeded", "failed to establish",
    ))


def _run(argv: list[str], timeout: int = 90) -> str:
    try:
        # Windows consoles default to cp1252: any page text with a char outside
        # that set (U+2024, em-dash, emoji - most modern sites) crashed
        # bridge.py's print() with UnicodeEncodeError (measured 2026-08-20 on a
        # YouTube page). utf-8 would fix the crash but ttyguard decodes the
        # capture as cp1252, so the text would arrive as mojibake. cp1252 with
        # errors=replace is the cleanest path: Latin text prints normally,
        # out-of-set chars become "?" instead of killing the call.
        env = dict(os.environ, PYTHONIOENCODING="cp1252:replace")
        r = ttyguard.run([sys.executable, str(SCRIPT), *argv], timeout=timeout, env=env)
    except Exception as e:
        return f"[error] chrome: {type(e).__name__}: {e}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0 or _looks_like_no_relay(out):
        detail = _last_error_line(out) if out else "(no output)"
        msg = f"[error] chrome exit {r.returncode}: {detail}"
        if _looks_like_no_relay(out):
            msg += _RELAY_HINT
        elif _looks_like_no_extension(out):
            msg += _NO_EXT_HINT
        return msg
    return out or "(no output)"


def _relay(call: str, timeout: int = 60) -> str:
    """Run one relayctl function in a child and return what it said.

    A CHILD, not an import. relayctl.start() launches the relay DETACHED with
    its output redirected to log files — importing it here would put that
    machinery inside the TUI's own process, and this module's contract is that
    every child goes through ttyguard so nothing can leave the terminal in
    mouse-reporting mode or take its stdin.

    relayctl is also an interactive dashboard when run as a script, which is
    why this calls the function rather than the CLI.
    """
    code = (
        "import sys;sys.path.insert(0, r'{}');"
        "import relayctl;print(relayctl.{})".format(RELAY_DIR, call)
    )
    try:
        r = ttyguard.run([sys.executable, "-c", code], timeout=timeout)
    except Exception as e:
        return f"[error] chrome relay: {type(e).__name__}: {e}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        return f"[error] chrome relay exit {r.returncode}: {out or '(no output)'}"
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

    # The relay's lifecycle belongs to whoever needs it. Before this, an agent
    # that hit the idle state could only report it and stop.
    if action == "start":
        started = _relay("start()")
        if started.startswith("[error]"):
            return started
        # 🔴 THE PORT BEING OPEN IS NOT THE TOOL BEING USABLE. relayctl.start()
        # returns as soon as the relay answers, but the extension is a CLIENT on
        # its own reconnect timer — so "started" was true and the very next call
        # failed. Reporting readiness the caller does not have is the whole
        # defect this module exists to prevent, so wait for the real thing and
        # say which state was reached.
        state = _relay("wait_for_extension(12.0)", timeout=45)
        return f"relay: {started} — {state}"
    if action == "stop":
        return f"relay: {_relay('kill()')}"
    if action == "status":
        state = _relay("probe(2.0)")
        return f"relay: {state}"

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

    if action == "write_text":
        value = args.get("text")
        if value is None or str(value) == "":
            return (
                "[error] chrome write_text: `text` is required. Give a `selector` "
                "too, or click the field first and it types into the focused one."
            )
        argv = ["write", str(value)]
        if args.get("selector"):
            argv.append(str(args["selector"]))
        if args.get("clear") is False:
            argv.append("--append")
        if args.get("enter"):
            argv.append("--enter")
        return _run(argv)

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
