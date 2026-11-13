"""/hooks for the native frontend: Python's own HooksEditor, run headless, as data.

Python's /hooks (plugins/settings_ui._cmd_hooks) opens HooksScreen: the HooksEditor
(hooks_screen.py) in a round-bordered box, then Close and Dock to side. The editor keeps its
draft in the widget (the scope, the rows, the disk baseline, the selected hook) and its
handlers do the work: New, Duplicate, Override global, Delete, Move, Save hooks (the
canonical hook_config.save with its baseline check), Reload, the repair save, Test script
(hook_host.invoke under the normal approval policy) and Resubmit.

The native frontend draws the editor itself. So that every decision stays Python's, the real
HooksScreen is mounted in the headless app while the frontend's dialog is open: ``open``
pushes it, ``act`` sets the edited controls and presses a button or picks an option, letting
the editor's own handlers run, ``read`` re-reads it (a test runs in a worker) and ``close``
pops it. Each answer is the editor's widgets in screen order with what each one shows.
"""
from __future__ import annotations

import asyncio

VERSION = 1
PREFIX = "gui.hooks.native_editor_"
OPERATIONS = tuple(PREFIX + name for name in ("open", "act", "read", "close"))
MAX_VALUE = 64 * 1024
MAX_VALUES = 64


def _plain(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    plain = getattr(value, "plain", None)
    return plain if isinstance(plain, str) else str(value)


def _screen(app):
    from litetui.hooks_screen import HooksScreen

    screen = getattr(app, "_native_hooks_screen", None)
    if screen is not None and screen in app.screen_stack:
        return screen
    return None


def _editor(app):
    from litetui.hooks_screen import HooksEditor

    screen = _screen(app)
    if screen is None:
        raise ValueError("The hooks editor is not open")
    return screen.query_one(HooksEditor)


async def _settle(app):
    # Handlers run on posted messages; a few loop turns let them land and the form refresh.
    for _ in range(4):
        await asyncio.sleep(0)


def _options(select):
    return [_plain(label) for label, value in getattr(select, "_options", ()) if value is not select.NULL]


def _select_index(select):
    values = [value for _label, value in getattr(select, "_options", ()) if value is not select.NULL]
    try:
        return str(values.index(select.value))
    except ValueError:
        return ""


def _item(widget):
    from textual.containers import Horizontal
    from textual.widgets import Button, Input, Label, Select, Static, Switch, TextArea

    if not widget.display:
        return None
    base = {"id": widget.id, "disabled": bool(widget.disabled)}
    if isinstance(widget, Horizontal):
        children = [item for item in map(_item, widget.children) if item is not None]
        return {"kind": "line", "items": children}
    if isinstance(widget, Button):
        return {"kind": "button", "label": _plain(widget.label), "variant": widget.variant, **base}
    if isinstance(widget, Select):
        return {"kind": "select", "prompt": _plain(widget.prompt), "options": _options(widget),
                "value": _select_index(widget), **base}
    if isinstance(widget, Switch):
        return {"kind": "switch", "value": bool(widget.value), **base}
    if isinstance(widget, TextArea):
        return {"kind": "textarea", "value": widget.text, **base}
    if isinstance(widget, Input):
        return {"kind": "input", "value": widget.value, "placeholder": _plain(widget.placeholder), **base}
    if isinstance(widget, Label):
        return {"kind": "label", "text": _plain(widget.content), **base}
    if isinstance(widget, Static):
        return {"kind": "text", "text": _plain(widget.content), **base}
    return None


def snapshot(app) -> dict:
    editor = _editor(app)
    items = [item for item in map(_item, editor.children) if item is not None]
    testing = any(worker.group == "hook-test" and not worker.is_finished
                  for worker in getattr(editor, "workers", ()))
    return {"version": VERSION, "kind": "hooks_editor", "items": items, "testing": testing,
            "disabled": bool(editor.disabled)}


def _check(cmd, fields):
    if set(cmd) - {"type", "id"} != {"version", *fields}:
        raise ValueError("Hooks editor command fields are malformed")
    if type(cmd["version"]) is not int or cmd["version"] != VERSION:
        raise ValueError("Hooks editor requires version 1")


def _set(editor, values):
    """The frontend's edits to Inputs, the Switch and TextAreas, which handlers read on a press."""
    from textual.widgets import Input, Switch, TextArea

    if not isinstance(values, dict) or len(values) > MAX_VALUES:
        raise ValueError("Hooks editor values are malformed")
    for wid, value in values.items():
        widget = editor.query_one(f"#{wid}")
        if isinstance(widget, Switch) and type(value) is bool:
            widget.value = value
        elif isinstance(widget, TextArea) and isinstance(value, str) and len(value.encode()) <= MAX_VALUE:
            widget.load_text(value)
        elif isinstance(widget, Input) and isinstance(value, str) and len(value.encode()) <= MAX_VALUE:
            widget.value = value
        else:
            raise ValueError(f"Hooks editor value for {wid} is malformed")


def _action(editor, action):
    from textual.widgets import Button, Select

    if action is None:
        return
    if not isinstance(action, dict):
        raise ValueError("Hooks editor action is malformed")
    if set(action) == {"press"}:
        button = editor.query_one(f"#{action['press']}", Button)
        if button.disabled or not button.display:
            raise ValueError("That button is not available")
        button.press()
        return
    if set(action) == {"select", "index"}:
        select = editor.query_one(f"#{action['select']}", Select)
        values = [value for _label, value in select._options if value is not select.NULL]
        index = action["index"]
        if type(index) is not int or not 0 <= index < len(values):
            raise ValueError("That option is not offered")
        select.value = values[index]
        return
    raise ValueError("Hooks editor action is malformed")


async def dispatch(app, cmd):
    from litetui.hooks_screen import HooksScreen

    operation = cmd["type"]
    if operation == PREFIX + "open":
        _check(cmd, ())
        if _screen(app) is None:
            screen = HooksScreen()
            app._native_hooks_screen = screen
            await app.push_screen(screen)
            await _settle(app)
        return snapshot(app)
    if operation == PREFIX + "read":
        _check(cmd, ())
        return snapshot(app)
    if operation == PREFIX + "act":
        _check(cmd, ("values", "action"))
        editor = _editor(app)
        _set(editor, cmd["values"])
        await _settle(app)
        _action(editor, cmd["action"])
        await _settle(app)
        return snapshot(app)
    if operation == PREFIX + "close":
        _check(cmd, ())
        screen = _screen(app)
        app._native_hooks_screen = None
        if screen is not None and app.screen is screen:
            app.pop_screen()
            await _settle(app)
        return {"version": VERSION, "closed": True}
    raise ValueError("Unknown hooks editor operation")
