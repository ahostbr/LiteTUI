"""/modelcfg for the native frontend: Python's own ModelConfigScreen, run headless, as data.

Python's /modelcfg (plugins/model_switch._cmd_modelcfg) opens ModelConfigScreen for the
current model: the title, the keys line, a TabbedContent of Info, Load and Inference (each a
VerticalScroll of .set-row label + control + help), the error line and Dock to side. Esc
cancels; Ctrl+S applies (ModelConfigBody.action_apply: the settings are saved and a loaded
model reloads, which evicts its resident weights). A remote backend (Codex, Claude) gets a
reasoning-effort picker instead; that path is not this dialog.

As /cal (native_calendar_dialog.py), the real screen runs in the headless app while the
frontend's dialog is open, laid out at the frontend's terminal size, and each answer is the
screen's shown widgets in order with their screen regions: Python's controls as
native_hooks_editor._item describes them, the tab strip as {"kind": "tabs"}, and every other
container as a group of its children (a scrolling one carrying its region as the clip and
its scroll). ``act`` sets edited controls, then sends one key (Esc, Ctrl+S), a tab, a focus
move, a scroll, a press or a Select pick.
"""
from __future__ import annotations

from litetui.native_calendar_dialog import (_dock, _fit, _refreshed, _region, _restore, _size)
from litetui.native_hooks_editor import _check, _item, _set, _settle
from litetui.native_hooks_editor import _action as _widget_action

VERSION = 1
PREFIX = "gui.modelcfg.native_dialog_"
OPERATIONS = tuple(PREFIX + name for name in ("open", "act", "read", "close"))

_KEYS = {"escape": "cancel", "ctrl+s": "apply"}


def _screen(app):
    from litetui.plugins.model_switch import ModelConfigScreen

    screen = getattr(app, "_native_modelcfg_screen", None)
    if screen is not None and screen in app.screen_stack and isinstance(screen, ModelConfigScreen):
        return screen
    return None


def walk(widget):
    """One shown widget as data, with its region; containers become groups of children."""
    from textual.containers import ScrollableContainer
    from textual.widgets import (Button, Input, Label, OptionList, Select, Static, Switch, Tabs,
                                 TextArea)

    from litetui.ticker import NumberTicker

    if not widget.display or widget.region.width == 0:
        return None
    if isinstance(widget, Tabs):
        tabs = list(widget.query("Tab"))
        active = widget.active_tab
        return {"kind": "tabs", "id": widget.id, "region": _region(widget),
                "tabs": [{"id": tab.id, "label": str(tab.label), "region": _region(tab)} for tab in tabs],
                "active": active.id if active is not None else None}
    if isinstance(widget, (Button, Select, Switch, TextArea, Input, Label, Static, OptionList,
                           NumberTicker)):
        entry = _item(widget)
        if entry is None:
            return None
        entry["region"] = _region(widget)
        return entry
    children = [entry for entry in map(walk, widget.children) if entry is not None]
    if not children:
        return None
    group = {"kind": "line", "id": widget.id, "classes": [], "region": _region(widget), "items": children}
    if isinstance(widget, ScrollableContainer):
        content = widget.scrollable_content_region
        group.update(clip=[content.x, content.y, content.width, content.height],
                     scroll_y=int(widget.scroll_offset.y), max_scroll_y=int(widget.max_scroll_y))
    return group


def snapshot(app) -> dict:
    from litetui.plugins.model_switch import ModelConfigBody

    screen = _screen(app)
    if screen is None:
        return {"version": VERSION, "kind": "modelcfg_dialog", "screen": "closed"}
    body = screen.query_one(ModelConfigBody)
    box = body.query_one("#set-box")
    content = box.content_region
    return {"version": VERSION, "kind": "modelcfg_dialog", "screen": "form", "size": list(app.size),
            "dock": _dock(screen), "box": _region(box),
            "content": [content.x, content.y, content.width, content.height],
            "scroll_y": 0, "max_scroll_y": 0, "focused": getattr(app.focused, "id", None),
            "items": [e for e in map(walk, box.children) if e is not None]}


async def _settled(app) -> None:
    import asyncio

    await _settle(app)
    for _ in range(3):
        await _refreshed(app)
    try:
        await asyncio.wait_for(app.animator.wait_until_complete(), 1.0)
    except asyncio.TimeoutError:
        pass
    await _settle(app)


async def _act(app, screen, action) -> None:
    from textual.widgets import Input, TabbedContent

    if not isinstance(action, dict):
        raise ValueError("Model settings action is malformed")
    if set(action) == {"key"}:
        binding = _KEYS.get(action["key"])
        if binding is None:
            raise ValueError("That key does nothing here")
        await screen.run_action(binding)
        return
    if set(action) == {"tab"}:
        tabbed = screen.query_one("#mc-tabs", TabbedContent)
        pane = action["tab"]
        if not isinstance(pane, str) or not tabbed.query(f"#{pane}"):
            raise ValueError("That tab is not offered")
        tabbed.active = pane
        return
    if set(action) == {"focus"}:
        target = screen.query_one(f"#{action['focus']}")
        if not target.display or target.disabled or not target.focusable:
            raise ValueError("That control cannot take focus")
        target.focus()
        return
    if set(action) == {"scroll"}:
        delta = action["scroll"]
        if type(delta) is not int or not -100 <= delta <= 100:
            raise ValueError("Model settings scroll is malformed")
        found = [w for w in screen.query(".set-scroll") if w.display]
        if not found:
            raise ValueError("Nothing scrolls here")
        found[0].scroll_relative(y=delta, animate=False)
        return
    if set(action) == {"submit"}:
        target = screen.query_one(f"#{action['submit']}", Input)
        await target.action_submit()
        return
    _widget_action(screen, action)


async def dispatch(app, cmd):
    from litetui.plugins.model_switch import ModelConfigScreen, _cmd_modelcfg

    operation = cmd["type"]
    if operation == PREFIX + "open":
        _check(cmd, ("size",))
        if getattr(app.backend, "remote", False):
            # Python's /modelcfg on a remote backend is a reasoning-effort picker, not this
            # screen: say so, open nothing, and the frontend keeps its own effort route.
            return {"version": VERSION, "kind": "modelcfg_dialog", "screen": "remote"}
        await _fit(app, _size(cmd))
        if _screen(app) is None:
            # Python's own /modelcfg, with the frontend as the keyboard.
            app._native_dialog_driving = True
            try:
                _cmd_modelcfg(app, "/modelcfg", "")
                await _settle(app)
            finally:
                app._native_dialog_driving = False
            if not isinstance(app.screen, ModelConfigScreen):
                # "No model selected — …": Python said why in the chat log; nothing opened.
                await _restore(app)
                return {"version": VERSION, "kind": "modelcfg_dialog", "screen": "closed"}
            app._native_modelcfg_screen = app.screen
            await _settled(app)
        return snapshot(app)
    if operation == PREFIX + "read":
        _check(cmd, ("size",))
        await _fit(app, _size(cmd))
        return snapshot(app)
    if operation == PREFIX + "act":
        _check(cmd, ("size", "values", "action"))
        await _fit(app, _size(cmd))
        screen = _screen(app)
        if screen is None:
            raise ValueError("The model settings are not open")
        _set(screen, cmd["values"])
        await _settle(app)
        app._native_dialog_driving = True
        try:
            await _act(app, screen, cmd["action"])
            await _settled(app)
        finally:
            app._native_dialog_driving = False
        if _screen(app) is None:
            app._native_modelcfg_screen = None
            await _restore(app)
        return snapshot(app)
    if operation == PREFIX + "close":
        _check(cmd, ())
        screen = _screen(app)
        app._native_modelcfg_screen = None
        if screen is not None and app.screen is screen:
            app.pop_screen()
            await _settle(app)
        await _restore(app)
        return {"version": VERSION, "closed": True}
    raise ValueError("Unknown model settings operation")
