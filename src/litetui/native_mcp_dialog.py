"""/mcp for the native frontend: Python's own MCPListBody, run headless, as data.

Python's bare /mcp (plugins/mcp_manage._cmd_mcp) opens MCPListBody (mcp_list.py) in the
modal host: the title, the note, a 16-row scroll region of server rows (name and state,
transport and target, the state's note, any error, and the state's action buttons), the add
form (name, target, Add), a status line, then Close and Dock to side. Every action is applied
when it is made, through the body's own handlers and gates (submit_op, maintenance, the
.mcp.json writer), and the rows re-render from the manager.

As for /hooks (native_hooks_editor.py), the real body runs in the headless app while the
frontend's dialog is open: ``open`` pushes it in a modal host, ``act`` sets the add form's
Inputs and presses a button, ``read`` re-reads it (actions run off the loop) and ``close``
pops it. Each answer is the body's visible widgets in order, the scroll region's rows nested.
"""
from __future__ import annotations

from textual.screen import ModalScreen

from litetui.native_hooks_editor import _action, _check, _item, _set, _settle

VERSION = 1
PREFIX = "gui.mcp.native_dialog_"
OPERATIONS = tuple(PREFIX + name for name in ("open", "act", "read", "close"))


class _Host(ModalScreen):
    """The modal host the dialog opens in: centred, the body full width."""

    DEFAULT_CSS = "_Host { align: center middle; }"

    def compose(self):
        from litetui.mcp_list import MCPListBody

        yield MCPListBody()


def _screen(app):
    screen = getattr(app, "_native_mcp_screen", None)
    return screen if screen is not None and screen in app.screen_stack else None


def _body(app):
    from litetui.mcp_list import MCPListBody

    screen = _screen(app)
    if screen is None:
        raise ValueError("The MCP dialog is not open")
    return screen.query_one(MCPListBody)


def snapshot(app) -> dict:
    body = _body(app)
    items = [entry for entry in map(_item, body.children) if entry is not None]
    busy = bool(getattr(app, "_mcp_maintenance", False))
    return {"version": VERSION, "kind": "mcp_dialog", "items": items, "testing": busy,
            "disabled": bool(body.disabled)}


async def dispatch(app, cmd):
    operation = cmd["type"]
    if operation == PREFIX + "open":
        _check(cmd, ())
        if _screen(app) is None:
            screen = _Host()
            app._native_mcp_screen = screen
            await app.push_screen(screen)
            await _settle(app)
        return snapshot(app)
    if operation == PREFIX + "read":
        _check(cmd, ())
        return snapshot(app)
    if operation == PREFIX + "act":
        _check(cmd, ("values", "action"))
        body = _body(app)
        _set(body, cmd["values"])
        await _settle(app)
        _action(body, cmd["action"])
        await _settle(app)
        return snapshot(app)
    if operation == PREFIX + "close":
        _check(cmd, ())
        screen = _screen(app)
        app._native_mcp_screen = None
        if screen is not None and app.screen is screen:
            app.pop_screen()
            await _settle(app)
        return {"version": VERSION, "closed": True}
    raise ValueError("Unknown MCP dialog operation")
