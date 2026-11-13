"""Python's transcript system lines, for a native frontend that asks for them.

`LiteTUI.system_message` is how the app and every plugin say something in the chat log
("Hooks disabled by LITETUI_HOOKS=off", "harness seat online · …", "Unknown: /x — try
/help"). The Textual frontend mounts them as `.system-msg` rows; a native frontend draws
its own transcript and saw none of them.

Nothing is sent unasked. The Rust client faults on an event it did not expect, and before
its startup reads have finished (hello, state, history) any event at all is unexpected. So
the lines are held until the frontend asks with ``gui.system_lines``: the reply carries the
lines held so far, each with whether the start splash had been mounted yet (the lines
before it sit above it), and only once that reply has been written does each later line
go out as a ``system_message`` event. A frontend that never asks gets nothing.
"""
from __future__ import annotations

OPERATION = "gui.system_lines"
EVENT = "system_message"
VERSION = 1
MAX_TEXT = 16 * 1024
MAX_HELD = 256


def _line(app, text) -> dict:
    return {"text": str(text)[:MAX_TEXT], "before_splash": not getattr(app, "_splash_mounted", False)}


def observe(app, text: str) -> None:
    """One system line the chat log just mounted."""
    if not getattr(app, "_rpc", False):
        return
    line = _line(app, text)
    if getattr(app, "_gui_system_lines_open", False):
        app._rpc_emit({"type": EVENT, "version": VERSION, **line})
        return
    held = app.__dict__.setdefault("_gui_system_lines_held", [])
    if len(held) < MAX_HELD:
        held.append(line)


def read(app, cmd) -> dict:
    """gui.system_lines: the lines held so far. Later ones follow as events, after this reply."""
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("System lines require version 1")
    if getattr(app, "_gui_system_lines_open", False):
        raise ValueError("System lines are already being sent")
    return {"version": VERSION, "lines": app.__dict__.pop("_gui_system_lines_held", [])}


def after_read(app) -> None:
    """The gui.system_lines reply is written: from now on each line is an event."""
    app._gui_system_lines_open = True
    for line in app.__dict__.pop("_gui_system_lines_held", []):
        app._rpc_emit({"type": EVENT, "version": VERSION, **line})
