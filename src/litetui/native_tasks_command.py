"""/tasks, /glassbox and /engine for the native frontend: Python's own commands, run as typed.

None of them opens anything in Python. /engine (plugins/model_switch._cmd_engine) reports,
starts, stops or re-lanes the NInfer, Strata or Xolotl process LiteTUI may own, or says
which backend to pick first, all in the chat log. /tasks (plugins/tasks_plugin._cmd_tasks) answers in the
chat log (the task list or "no background tasks", a tail, a kill's result, its usage
line); /glassbox (plugins/glassbox_plugin._cmd_glassbox) starts or stops the recorder and
its local feed and says so ("[glassbox] serving http://127.0.0.1:7580/events", "recording
to ...", its usage line), refusing when another app owns the listener. A native frontend
sends the command and its typed arguments here and the handler runs exactly as it does in
the Textual app; its lines reach the frontend as the system_message events every other
chat-log line uses (native_system_lines.py). A refusal comes back as the error.
"""
from __future__ import annotations

OPERATION = "gui.tasks.native_command"
VERSION = 1
MAX_ARGUMENTS = 4096


def _handlers():
    from litetui.plugins.glassbox_plugin import _cmd_glassbox
    from litetui.plugins.model_switch import _cmd_engine
    from litetui.plugins.tasks_plugin import _cmd_tasks

    return {"/tasks": _cmd_tasks, "/glassbox": _cmd_glassbox, "/engine": _cmd_engine}


def dispatch(app, cmd) -> dict:
    if set(cmd) - {"type", "id", "version", "command", "arguments"}:
        raise ValueError("Unknown native command field")
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Native commands require version 1")
    handler = _handlers().get(cmd.get("command"))
    if handler is None:
        raise ValueError("That command is not run this way")
    arguments = cmd.get("arguments")
    if (not isinstance(arguments, str) or "\0" in arguments
            or len(arguments.encode("utf-8")) > MAX_ARGUMENTS):
        raise ValueError("Command arguments must be text of at most 4096 bytes")
    handler(app, cmd["command"], arguments)
    return {"version": VERSION, "ran": True}
