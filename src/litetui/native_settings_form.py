"""/settings for the native frontend: the real Textual settings screen, as data.

The native frontend draws Python's own settings dialog instead of keeping a second
copy of its 12 tabs and ~130 rows. `form` mounts the exact screen /settings opens
(plugins.settings_ui.settings_dialog), walks its widgets in order and returns each
tab's rows: section headers, labels, inputs, switches, selects, help lines and
buttons, with their current values. `save` mounts a fresh screen, checks it still
matches the form the edit was made on, sets the edited controls and presses the
screen's own Save, so parsing, validation, persistence, runtime apply and the
messages are the Textual dialog's, not a re-implementation.

Secrets (password inputs) never leave this process: the form says only whether
one is set, and a save writes one only when the frontend sends a new value.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

VERSION = 1
FORM = "gui.settings.native_form"
SAVE = "gui.settings.native_form_save"
OPERATIONS = (FORM, SAVE)
MAX_BYTES = 1024 * 1024
MAX_VALUE = 64 * 1024
MAX_EDITS = 256


def _plain(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    plain = getattr(value, "plain", None)
    if isinstance(plain, str):
        return plain
    return str(value)


def _identity(app):
    session = getattr(app, "_agent_session", None)
    return {"session_id": getattr(app, "convo_id", None),
            "agent_id": session.authority.agent_id if session is not None else None,
            "backend": getattr(getattr(app, "backend", None), "name", None),
            "model": getattr(app, "model_id", None)}


def _check(app, cmd):
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Native settings form requires version 1")
    from litetui import backend_catalog

    backend_catalog._context(app, cmd.get("session_id"))


async def _mount(app):
    from litetui.plugins.settings_ui import settings_dialog

    _body, screen_factory, _callback = settings_dialog(app)
    screen = screen_factory()
    await app.push_screen(screen)
    # Section headers and the dirty-state baseline settle after the first refresh.
    for _ in range(3):
        await asyncio.sleep(0)
    return screen


async def _unmount(app, screen):
    if screen in app.screen_stack and app.screen is screen:
        await app.pop_screen()


REVIEWED = "Changed with its reviewed control (Ctrl+T, Shift+Tab, /tools, /mcp), not here."


def _reviewed(app):
    """Control ids whose settings change only through reviewed runtime controls."""
    from litetui import native_runtime_controls

    if not native_runtime_controls.enabled(app):
        return set(), False
    fields = native_runtime_controls.reviewed_fields(app)
    return {f"f-{name}" for name in fields}, "mcp_disabled_servers" in fields


def _control(widget, section, reviewed=(set(), False)):
    from textual.widgets import Input, Select, Switch, TextArea

    ids, servers = reviewed
    locked = widget.id in ids or (servers and widget.id.startswith("mcp-"))
    row = {"id": widget.id, "disabled": bool(widget.disabled) or locked, "section": section}
    if locked:
        row["reason"] = REVIEWED
    if isinstance(widget, Switch):
        return {"kind": "switch", "value": bool(widget.value), **row}
    if isinstance(widget, Select):
        options = [[_plain(prompt), value] for prompt, value in getattr(widget, "_options", ())
                   if isinstance(value, str)]
        value = widget.value if isinstance(widget.value, str) else ""
        return {"kind": "select", "value": value, "options": options, **row}
    if isinstance(widget, TextArea):
        return {"kind": "textarea", "value": widget.text, **row}
    if isinstance(widget, Input):
        out = {"kind": "input", "placeholder": _plain(widget.placeholder),
               "password": bool(widget.password), **row}
        if widget.password:
            out["set"] = bool(widget.value)
        else:
            out["value"] = widget.value
        return out
    return None


def _items(node, section, out, reviewed=(set(), False)):
    """Rows of one container, in screen order. Horizontal groups become one line."""
    from textual.containers import Horizontal
    from textual.widgets import Button, Input, Label, Select, Static, Switch, TextArea

    from litetui.hooks_screen import HooksEditor
    from litetui.settings_screen import SettingsSectionHeader

    for child in node.children:
        if isinstance(child, SettingsSectionHeader):
            from litetui.settings_ui_model import _search_text

            spec = child.spec
            section = spec.section_id
            # The same text search_settings matches, so the frontend's search finds what Python's does.
            fields = {f.name: " ".join((f.name, f.label, f.description, *f.keywords)).lower()
                      for f in spec.fields}
            out.append({"kind": "section", "id": section, "label": _plain(child.label),
                        "open": bool(child._open), "search": _search_text(spec), "fields": fields})
            continue
        if isinstance(child, HooksEditor):
            out.append({"kind": "hooks_editor", "section": section})
            continue
        if isinstance(child, (Input, Select, Switch, TextArea)):
            if child.id:
                out.append(_control(child, section, reviewed))
            continue
        if isinstance(child, Button):
            out.append({"kind": "button", "id": child.id, "label": _plain(child.label),
                        "disabled": bool(child.disabled), "section": section})
            continue
        if isinstance(child, Label):
            out.append({"kind": "label", "text": _plain(child.content),
                        "inline": child.has_class("set-label-inline"), "section": section})
            continue
        if isinstance(child, Static):
            out.append({"kind": "text", "text": _plain(child.content), "id": child.id,
                        "classes": sorted(child.classes), "section": section})
            continue
        if isinstance(child, Horizontal):
            line = []
            section = _items(child, section, line, reviewed)
            out.append({"kind": "line", "items": line, "section": section})
            continue
        if child.has_class("set-row"):
            # One labelled row (label, control, help): Python spaces rows apart and indents some.
            row = []
            section = _items(child, section, row, reviewed)
            out.append({"kind": "row", "items": row, "indent": child.has_class("set-indent"),
                        "section": section})
            continue
        section = _items(child, section, out, reviewed)
    return section


def _walk(app, screen):
    from textual.widgets import Button, Static, TabbedContent, TabPane

    from litetui.settings_screen import SettingsBody

    body = screen.query_one(SettingsBody)
    tabbed = body.query_one(TabbedContent)
    tabs = []
    reviewed = _reviewed(app)
    for pane in tabbed.query(TabPane):
        items = []
        _items(pane, None, items, reviewed)
        # A pane whose rows sit in a VerticalScroll (.set-scroll) keeps a padding column and a
        # scrollbar column; the Hooks pane has none and uses the full width.
        scroll = any(child.has_class("set-scroll") for child in pane.children)
        tabs.append({"id": pane.id, "label": _plain(tabbed.get_tab(pane.id).label), "items": items,
                     "scroll": scroll})
    buttons = [{"id": b.id, "label": _plain(b.label), "disabled": bool(b.disabled)}
               for b in body.query_one("#set-buttons").query(Button)]
    def text(selector):
        return _plain(body.query_one(selector, Static).content)

    form = {"title": text("#set-title"), "subtitle": text("#set-sub"),
            "search_placeholder": _plain(body.query_one("#set-search").placeholder),
            "search_status": text("#set-search-status"),
            "tabs": tabs, "buttons": buttons}
    message = text("#set-error")
    return body, form, message


def _encoded(value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("Settings form exceeds the native frame limit")
    return data


def _result(app, form, kind, **extra):
    result = {"version": VERSION, "kind": kind, **_identity(app), **form, **extra}
    result["form"] = hashlib.sha256(_encoded(form)).hexdigest()
    _encoded(result)
    return result


async def form(app, cmd):
    _check(app, cmd)
    screen = await _mount(app)
    try:
        _body, shown, _message = _walk(app, screen)
    finally:
        await _unmount(app, screen)
    return _result(app, shown, "settings_form")


def _controls(form):
    found = {}

    def visit(items):
        for item in items:
            if item["kind"] in {"line", "row"}:
                visit(item["items"])
            elif item["kind"] in {"input", "switch", "select", "textarea"}:
                found[item["id"]] = item
    for tab in form["tabs"]:
        visit(tab["items"])
    return found


def _edit(widget, item, value):
    from textual.widgets import TextArea

    if item["disabled"]:
        raise ValueError(item.get("reason") or f"{item['id']} is not editable here")
    kind = item["kind"]
    if kind == "switch":
        if type(value) is not bool:
            raise ValueError(f"{item['id']} needs true or false")
        widget.value = value
        return
    if not isinstance(value, str) or len(value) > MAX_VALUE:
        raise ValueError(f"{item['id']} needs text up to {MAX_VALUE} characters")
    if kind == "select":
        if value not in [option[1] for option in item["options"]]:
            raise ValueError(f"{item['id']}: not one of its choices")
        widget.value = value
    elif kind == "textarea" and isinstance(widget, TextArea):
        widget.load_text(value)
    else:
        widget.value = value


async def save(app, cmd):
    _check(app, cmd)
    edits = cmd.get("values")
    if not isinstance(edits, dict) or not 1 <= len(edits) <= MAX_EDITS:
        raise ValueError("Settings save needs between 1 and 256 edited controls")
    screen = await _mount(app)
    try:
        body, shown, _message = _walk(app, screen)
        current = hashlib.sha256(_encoded(shown)).hexdigest()
        if cmd.get("form") != current:
            return _result(app, shown, "settings_form_save", status="conflict",
                           message="Settings changed since this form was read; review it and save again.")
        controls = _controls(shown)
        for key, value in edits.items():
            item = controls.get(key)
            if item is None:
                raise ValueError(f"Unknown settings control: {key}")
            _edit(body.query_one(f"#{key}"), item, value)
        body._last_save_result = None
        body.action_save()
        for _ in range(3):
            await asyncio.sleep(0)
        closed = screen not in app.screen_stack
        result = body._last_save_result
        if closed:
            status, message = "saved", "Settings saved."
        else:
            from textual.widgets import Static

            message = _plain(body.query_one("#set-error", Static).content)
            status = "refused" if result is None else (
                "saved" if result.fully_saved and not result.has_runtime_failures else "partial")
    finally:
        await _unmount(app, screen)
    # Read back from a fresh screen: the frontend redraws what is now saved.
    after = await _mount(app)
    try:
        _body, fresh, _message = _walk(app, after)
    finally:
        await _unmount(app, after)
    return _result(app, fresh, "settings_form_save", status=status, message=message)


async def dispatch(app, cmd):
    if cmd["type"] == FORM:
        return await form(app, cmd)
    return await save(app, cmd)
