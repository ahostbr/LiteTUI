"""Bounded live command metadata and pure routes into verified native controls.

No plugin handler, palette callback or skill body executes through this module.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from litetui import backend_catalog, plugins
from litetui.agent_store import valid_id

VERSION = 1
MAX_ROWS = 1024
MAX_BYTES = 512 * 1024
# Reviewed adapters, not a second command/help catalog. Unknown contributions stay visible.
_ROUTES = {
    ("model-switch", "/model"): "models",
    ("model-switch", "/backend"): "backend",
    ("misc", "/think"): "thinking",
    ("misc", "/quit"): "quit",
    ("convo", "/convos"): "conversations",
    ("convo", "/resume"): "conversations",
    ("settings-ui", "/settings"): "settings",
}


def _text(value, limit=1024, *, empty=False):
    if not isinstance(value, str) or (not empty and not value) or len(value.encode("utf-8")) > limit:
        raise ValueError("Command metadata contains invalid or oversized text")
    return value


def _encoded(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("Command metadata exceeds its response budget")
    return data


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _token(value):
    value = _text(value, 128)
    if (not value.lstrip("/") or value.lower() != value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("Invalid registered command token")
    return value


def _order(value):
    if type(value) is not int or not -(2 ** 31) <= value < 2 ** 31:
        raise ValueError("Invalid command order")
    return value


def _row(kind, owner, title, help_text, group, order, tokens, binding, action, palette):
    row = {
        "kind": kind, "owner": _text(owner, 128), "title": _text(title),
        "help": _text(help_text, 4096, empty=True), "group": _text(group, 128),
        "group_label": _text(plugins.PALETTE_GROUP_LABELS.get(group, group.title()), 256),
        "order": _order(order), "tokens": tokens, "palette": palette,
        "native_action": action, "accepts_arguments": False,
        "unavailable_reason": "" if action else "This contribution has no native action adapter yet.",
    }
    row["id"] = _digest([binding, row])
    return row


def _collect(app):
    registry = app.plugins
    if not isinstance(registry, plugins.PluginRegistry):
        raise TypeError("Current plugin registry is unavailable")
    commands = registry.commands
    if len(commands) > MAX_ROWS * 32 or len(registry.palette_rows) > MAX_ROWS or len(app.skills) > MAX_ROWS:
        raise ValueError("Command inventory exceeds its entry budget")
    rows: list[dict] = []
    seen: set[int] = set()
    reserved: set[str] = set()
    bindings: list[object] = [id(registry)]
    for key, entry in commands.items():
        if (not isinstance(entry, plugins.CommandEntry) or not isinstance(entry.tokens, tuple)
                or not 1 <= len(entry.tokens) <= 32):
            raise ValueError("Invalid command registration")
        tokens = [_token(token) for token in entry.tokens]
        if len(set(tokens)) != len(tokens) or key not in tokens or any(commands.get(token) is not entry for token in tokens):
            raise ValueError("Command aliases disagree with their owner")
        reserved.update(token[1:].casefold() for token in tokens if token.startswith("/"))
        if id(entry) in seen:
            continue
        seen.add(id(entry))
        binding: list[object] = [id(entry), id(entry.handler)]
        bindings.append(binding)
        rows.append(_row("command", entry.owner, entry.palette or tokens[0], entry.help,
                         entry.group, entry.order, tokens, binding,
                         _ROUTES.get((entry.owner, tokens[0])), entry.palette is not None))
    for palette_entry in registry.palette_rows:
        if not isinstance(palette_entry, plugins.PaletteRow):
            raise TypeError("Invalid palette registration")
        binding = [id(palette_entry), id(palette_entry.run)]
        bindings.append(binding)
        row = _row("palette", palette_entry.owner, palette_entry.title, palette_entry.help,
                   palette_entry.group, palette_entry.order, [], binding, None, True)
        row["tag"] = _text(palette_entry.tag, 256, empty=True)
        rows.append(row)
    for skill in app.skills:
        name = _text(skill.name, 128)
        if name.casefold() in reserved:
            continue
        binding = [id(skill), str(skill.path), skill.source]
        bindings.append(binding)
        row = _row("skill", "skills", name, skill.description or "", "tools", 500,
                   ["/" + name], binding, None, False)
        row["source"] = _text(skill.source, 256)
        row["unavailable_reason"] = "Skill invocation requires a prepared-prompt adapter."
        rows.append(row)
    if len(rows) > MAX_ROWS or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Command inventory is oversized or has duplicate identities")
    palette_rows = sorted((row for row in rows if row["palette"]),
                          key=lambda row: plugins.palette_title_key(row["title"]))
    completion_rows = sorted((row for row in rows if row["kind"] == "command"),
                             key=lambda row: plugins.palette_sort_key(row["group"], row["order"], row["tokens"][0].lstrip("/")))
    completion_rows += sorted((row for row in rows if row["kind"] == "skill"), key=lambda row: row["title"].lower())
    for row in rows:
        row["palette_order"] = None
        row["completion_order"] = None
    for index, row in enumerate(palette_rows):
        row["palette_order"] = index
    for index, row in enumerate(completion_rows):
        row["completion_order"] = index
    return registry, rows, bindings


def catalog(app, cmd):
    """Read one source-owned registry generation without running its callbacks."""
    from litetui.gui_rpc import _tool_policy

    before = backend_catalog._context(app, cmd.get("session_id"))
    registry, rows, bindings = _collect(app)
    policy = _tool_policy(app)
    facts = {"registry": bindings, "authority": asdict(before.authority), "policy": policy,
             "backend": before.selected, "model": app.model_id, "session_id": before.session_id,
             "rows": rows}
    token = _digest(facts)
    after = backend_catalog._context(app, before.session_id)
    current_registry, current_rows, current_bindings = _collect(app)
    if (after != before or current_registry is not registry or current_rows != rows
            or current_bindings != bindings or _tool_policy(app) != policy):
        raise ValueError("Command registry or owned context changed while reading; refresh")
    result = {"version": VERSION, "kind": "command_catalog", "session_id": before.session_id,
              "agent_id": valid_id(before.authority.agent_id), "snapshot": token,
              "backend": before.selected, "model": app.model_id, "entries": rows}
    _encoded(result)
    return result


def resolve(app, cmd):
    """Resolve a selected row to a native control; never execute source handlers."""
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Command routing requires version 1")
    current = catalog(app, cmd)
    if cmd.get("agent_id") != current["agent_id"] or cmd.get("snapshot") != current["snapshot"]:
        raise ValueError("Command selection is stale or belongs to another agent; refresh")
    if cmd.get("arguments", "") != "":
        raise ValueError("This native control adapter does not accept command arguments")
    selected = next((row for row in current["entries"] if row["id"] == cmd.get("row_id")), None)
    if selected is None:
        raise ValueError("Command selection is missing from the current registry")
    if selected["native_action"] is None:
        raise ValueError(selected["unavailable_reason"])
    return {"version": VERSION, "kind": "command_route", "session_id": current["session_id"],
            "agent_id": current["agent_id"], "snapshot": current["snapshot"],
            "row_id": selected["id"], "native_action": selected["native_action"]}
