"""/tasks for the native frontend: Python's own command, run as typed.

Python's /tasks (plugins/tasks_plugin._cmd_tasks) opens nothing: it answers in the chat log
(the task list or "no background tasks", a tail, a kill's result, or its usage line). A
native frontend sends the typed arguments here and the handler runs exactly as it does in
the Textual app; its lines reach the frontend as the system_message events every other
chat-log line uses (native_system_lines.py). The answer carries nothing else.
"""
from __future__ import annotations

OPERATION = "gui.tasks.native_command"
VERSION = 1
MAX_ARGUMENTS = 4096


def dispatch(app, cmd) -> dict:
    if set(cmd) - {"type", "id", "version", "arguments"}:
        raise ValueError("Unknown /tasks command field")
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("/tasks requires version 1")
    arguments = cmd.get("arguments")
    if (not isinstance(arguments, str) or "\0" in arguments
            or len(arguments.encode("utf-8")) > MAX_ARGUMENTS):
        raise ValueError("/tasks arguments must be text of at most 4096 bytes")
    from litetui.plugins.tasks_plugin import _cmd_tasks

    _cmd_tasks(app, "/tasks", arguments)
    return {"version": VERSION, "ran": True}
