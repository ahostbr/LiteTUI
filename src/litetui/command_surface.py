"""Bounded live command metadata and pure routes into verified native controls.

No plugin handler, palette callback or skill body executes through this module.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from litetui import backend_catalog, llm_backend, plugins
from litetui.agent_store import valid_id

VERSION = 1
MAX_ROWS = 1024
MAX_BYTES = 512 * 1024
# Reviewed adapters, not a second command/help catalog. Unknown contributions stay visible.
_ROUTES = {
    ("model-switch", "/model"): "models",
    ("model-switch", "/backend"): "backend",
    ("model-switch", "/reconnect"): "model_reconnect",
    ("model-switch", "/load"): "model_load",
    ("model-switch", "/unload"): "model_unload",
    ("model-switch", "/engine"): "engine",
    ("convo", "/compact"): "conversation_compact",
    ("convo", "/system"): "system_prompt",
    ("settings-ui", "/hooks"): "hooks",
    ("reload-plugins", "/reload-plugins"): "plugin_reload",
    ("scheduler", "/job"): "work_new_job",
    ("misc", "/plan"): "plan",
    ("misc", "/think"): "thinking",
    ("misc", "/tools"): "tool_list",
    ("subagent", "/subagent-set"): "agent_prefs_override",
    ("model-switch", "/modelcfg"): "model_preferences",
    ("subagent", "/subagent-global"): "agent_prefs_global",
    ("misc", "/quit"): "quit",
    ("misc", "/pause"): "chat_pause",
    ("misc", "/keys"): "keys",
    ("misc", "/clear-screen"): "clear_screen",
    ("help", "/help"): "help",
    ("mcp_manage", "/mcp"): "mcp",
    ("glassbox", "glassbox"): "glassbox",
    ("convo", "/convos"): "conversations",
    ("convo", "/resume"): "conversation_resume",
    ("convo", "/claude"): "claude",
    ("convo", "/new"): "conversation_new",
    ("convo", "/rename"): "conversation_rename",
    ("settings-ui", "/settings"): "settings",
    ("skills", "/skills"): "skills",
    ("host", "/plugins"): "plugins",
    ("tasks", "/tasks"): "work_tasks",
    ("scheduler", "/cron"): "work_jobs",
    ("scheduler", "/calendar"): "work_calendar",
    ("goal-loop", "/loop"): "work_loops",
    ("goal-loop", "/goal"): "work_goal",
}
_PALETTE_ROUTES = {("settings-ui", "Theme"): "themes", ("scheduler", "Scheduled jobs"): "work_jobs"}
# Commands that stay unrouted say why, per exact (owner, first token). A third
# party registering the same token never inherits these reasons.
_UNAVAILABLE = {
    ("misc", "/screenshot"): "Screenshot saves an image of the Textual window and the terminal frontend has no capture path of its own; use your terminal's screenshot, or Ctrl+Shift+C to copy transcript text.",
    ("misc", "/maximize"): "Maximize enlarges the focused Textual panel; the terminal frontend has no panel focus and the transcript already takes every free row. Ctrl+G toggles the compact view; Alt+Up and Alt+Down scroll.",
    ("misc", "/test-sidebar"): "Test sidebar is a Textual-only dialog spike with no terminal equivalent.",
    ("monitor", "/monitor"): "Browser monitor is a desktop overlay that is not available in the terminal frontend.",
    ("sidecar-preview", "/sidecar"): "Native sidecar preview is a desktop overlay that is not available in the terminal frontend.",
    ("mark", "/mark"): "Mark the screen is a desktop overlay that is not available in the terminal frontend.",
}
_NO_ARGUMENTS = {
    "tool_list": "/tools takes no arguments; choose tools in the reviewed tool list.",
    "model_reconnect": "/reconnect takes no arguments; it reviews a reconnect to the current model only.",
    "settings": "/settings takes no arguments; pick a setting inside the settings control.",
    "help": "/help takes no arguments; the help overlay lists every command and key.",
    "keys": "/keys takes no arguments; the overlay lists every native key.",
    "clear_screen": "/clear-screen takes no arguments; it clears the display only.",
}
_WORK_INSPECTION = {
    "work_tasks": {"list"},
    "work_jobs": {"list", "ls"},
    "work_loops": {"list", "ls", "status"},
    "work_goal": {"status", "show"},
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
        "native_action": action,
        "accepts_arguments": (action in {
            "skill_prepare", "conversation_rename", "glassbox", "conversation_compact",
            "model_load", "model_unload", "engine", "plugin_reload", "plan", "system_prompt",
            "models", "thinking", "backend", "conversation_resume", "claude", "mcp",
        } or kind == "command" and action in _WORK_INSPECTION),
        "unavailable_reason": "" if action else _UNAVAILABLE.get(
            (owner, tokens[0] if tokens else ""), "This contribution has no native action adapter yet."),
    }
    row["id"] = _digest([binding, row])
    return row


def _skill_binding(skill):
    return [id(skill), _text(str(skill.path), 4096), _text(skill.source, 256),
            _text(str(Path(skill.path).absolute()), 4096)]


def _skill_row(skill):
    name = _text(skill.name, 128)
    binding = _skill_binding(skill)
    row = _row("skill", "skills", name, skill.description or "", "tools", 500,
               ["/" + name], binding, "skill_prepare", False)
    row["source"] = _text(skill.source, 256)
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
        reserved.update(token[1:].lower() for token in tokens if token.startswith("/"))
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
                   palette_entry.group, palette_entry.order, [], binding,
                   _PALETTE_ROUTES.get((palette_entry.owner, palette_entry.title)), True)
        row["tag"] = _text(palette_entry.tag, 256, empty=True)
        rows.append(row)
    for skill in app.skills:
        bindings.append(_skill_binding(skill))
        rows.append(_skill_row(skill))
    if len(rows) > MAX_ROWS or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Command inventory is oversized or has duplicate identities")
    palette_rows = sorted((row for row in rows if row["palette"]),
                          key=lambda row: plugins.palette_title_key(row["title"]))
    completion_rows = sorted((row for row in rows if row["kind"] == "command"),
                             key=lambda row: plugins.palette_sort_key(row["group"], row["order"], row["tokens"][0].lstrip("/")))
    skill_rows = sorted((row for row in rows if row["kind"] == "skill"), key=lambda row: row["title"].lower())
    completion_rows += [row for row in skill_rows if row["title"].lower() not in reserved]
    for row in rows:
        row["palette_order"] = None
        row["completion_order"] = None
        row["skill_order"] = None
    for index, row in enumerate(palette_rows):
        row["palette_order"] = index
    for index, row in enumerate(completion_rows):
        row["completion_order"] = index
    for index, row in enumerate(skill_rows):
        row["skill_order"] = index
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


def _selection(app, cmd):
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Command routing requires version 1")
    current = catalog(app, cmd)
    if cmd.get("agent_id") != current["agent_id"] or cmd.get("snapshot") != current["snapshot"]:
        raise ValueError("Command selection is stale or belongs to another agent; refresh")
    return current


def _preselection(app, action, arguments):
    """Refuse arguments a native control cannot honor; honored ones only preselect.

    Nothing here applies a model, level, backend, conversation or MCP change: the
    argument reaches the same reviewed control the palette opens.
    """
    wanted = arguments.strip()
    if action == "models" and wanted:
        if wanted.isascii() and wanted.isdigit():
            count = len(app.available_models)
            if not 1 <= int(wanted) <= count:
                raise ValueError(f"Invalid model number. Use 1-{count}" if count else "No models discovered")
        elif wanted not in app.available_models:
            raise ValueError(f"Model not found: {wanted}")
    elif action == "thinking" and wanted:
        from litetui.thinking_capabilities import thinking_capabilities

        levels = thinking_capabilities(app)["levels"]
        level = "default" if wanted.lower() in ("unset", "default", "server") else wanted.lower()
        if level not in levels:
            raise ValueError(f"Thinking level {wanted!r} is not offered by this backend and model. "
                             f"Offered: {', '.join(levels)}")
    elif action == "backend" and wanted:
        tokens = wanted.lower().split()
        if "--default" in tokens:
            raise ValueError("--default changes the startup default; the native backend selector switches "
                             "this conversation only. Command arguments were kept")
        names = [name for name, _label in llm_backend.visible_backends()]
        if len(tokens) > 1 or tokens[0] not in names:
            raise ValueError(f"Backend argument must be one of: {', '.join(names)}")
    elif action == "conversation_resume" and wanted:
        if wanted.isascii() and wanted.isdigit():
            raise ValueError("A numeric /resume position follows the Textual picker's order; give a "
                             "conversation id prefix or choose it in the native browser (/convos)")
        if len(wanted) > 36 or any(char not in "0123456789abcdef-" for char in wanted):
            raise ValueError("A /resume argument must be a lowercase conversation id prefix")
        session = getattr(app, "_agent_session", None)
        if session is None:
            raise ValueError("Choose an owned agent first with /resume; archives are read-only")
        from litetui.storage_catalog import conversations

        try:
            found = [row.conversation_id for row in conversations(
                session.store.data_root, agent_id=session.authority.agent_id)
                if row.conversation_id.startswith(wanted)]
        except (OSError, ValueError) as error:
            raise ValueError(f"Agent conversations blocked: {error}") from error
        if not found:
            raise ValueError(f"No conversation matches {wanted!r}; open /convos to choose one")
        if len(found) > 1:
            raise ValueError(f"{wanted!r} matches {len(found)} conversations; give more of the id "
                             "(the native browser needs one exact choice)")
    elif action == "claude":
        if app.backend.name != "claude":
            raise ValueError("/claude requires /backend claude.")
        if wanted.lower() not in {"", "status", "resolve", "continue", "new"}:
            raise ValueError("/claude status | resolve | continue | new")
    elif action == "mcp" and wanted.lower() not in {"", "list", "ls", "status"}:
        raise ValueError("The native MCP panel accepts list only; add, connect, disconnect, reconnect, "
                         "remove, reconcile and reload are reviewed inside the panel")


def resolve(app, cmd):
    """Resolve exact rows or source-parsed text; never execute registered handlers."""
    current = _selection(app, cmd)
    raw = "command" in cmd
    if raw:
        if "row_id" in cmd or "arguments" in cmd:
            raise ValueError("Raw command and selected row modes cannot be combined")
        original = _text(cmd["command"], 65536)
        parts = original.split(maxsplit=1)
        if not parts or not parts[0].startswith("/"):
            raise ValueError("A slash command is required")
        token = parts[0].lower()
        arguments = parts[1] if len(parts) > 1 else ""
        # Registered commands have canonical precedence over skill shorthand.
        matches = [row for row in current["entries"] if row["kind"] == "command"
                   and token in row["tokens"]]
        if not matches:
            bare = token.lstrip("/")
            matches = [row for row in current["entries"] if row["kind"] == "skill"
                       and row["completion_order"] is not None and row["title"].lower() == bare]
        if len(matches) != 1:
            raise ValueError("Command is unknown or ambiguous; select an exact catalog row")
        selected = matches[0]
        if selected["native_action"] == "skills" and arguments:
            skill_parts = arguments.split(maxsplit=1)
            name = skill_parts[0].lower().lstrip("/")
            matches = [row for row in current["entries"] if row["kind"] == "skill"
                       and row["title"].lower() == name]
            if len(matches) != 1:
                raise ValueError("Skill command arguments name an unknown or ambiguous skill; select an exact row")
            selected = matches[0]
            arguments = skill_parts[1] if len(skill_parts) > 1 else ""
    else:
        arguments = cmd.get("arguments", "")
        if not isinstance(arguments, str):
            raise ValueError("Command arguments must be text")
        _text(arguments, 65536, empty=True)
        selected = next((row for row in current["entries"] if row["id"] == cmd.get("row_id")), None)
        if selected is None:
            raise ValueError("Command selection is missing from the current registry")
    if selected["native_action"] is None:
        raise ValueError(selected["unavailable_reason"])
    if selected["native_action"] == "conversation_rename":
        from litetui.conversation_transition import validate_title_input

        # Prefill only; canonical normalization and mutation require a new review.
        arguments = validate_title_input(arguments)
    action = selected["native_action"]
    if action == "glassbox" and arguments.strip().lower() not in {"", "status", "start", "on", "stop", "off"}:
        raise ValueError("Glassbox accepts status, start/on or stop/off only; mutations require a fresh review")
    if action == "conversation_compact":
        if "\0" in arguments:
            raise ValueError("Compaction guidance cannot contain NUL")
        if arguments and app.backend.name == "codex" and hasattr(app.backend, "app_server"):
            raise ValueError("Codex native compaction does not accept guidance; command arguments were kept")
    if action == "system_prompt":
        from litetui import native_system_prompt

        native_system_prompt.validate_argument(arguments)
    if action in {"model_load", "model_unload"} and arguments.strip() not in {"", current["model"]}:
        raise ValueError("This lifecycle control reviews the current model only; select the requested model with /model first")
    if action == "engine" and arguments.strip().lower() not in {"", "status", "start", "stop"}:
        raise ValueError("Engine command supports status, start or stop; lane arguments require the engine configuration control")
    if action == "plan" and arguments.strip().lower() not in {"", "on", "off"}:
        raise ValueError("Plan accepts on, off or no argument to toggle")
    if action == "plugin_reload" and arguments and (arguments.strip() != arguments or len(arguments.split()) != 1):
        raise ValueError("Plugin reload accepts one exact tool name or skills; select it in the refresh control")
    if arguments and action in _WORK_INSPECTION:
        verb = arguments.strip()
        if (verb if action == "work_tasks" else verb.lower()) not in _WORK_INSPECTION[action]:
            raise ValueError("Open Work and review this action there. This command accepts inspection verbs only.")
    if arguments != "" and action in _NO_ARGUMENTS:
        raise ValueError(_NO_ARGUMENTS[action])
    if arguments != "" or action == "claude":
        _preselection(app, action, arguments)
    if arguments != "" and not selected["accepts_arguments"]:
        raise ValueError("This native control adapter does not accept command arguments")
    result = {"version": VERSION, "kind": "command_route", "session_id": current["session_id"],
              "agent_id": current["agent_id"], "snapshot": current["snapshot"],
              "backend": current["backend"], "model": current["model"], "arguments": arguments,
              "row_id": selected["id"], "native_action": selected["native_action"]}
    if raw:
        result["command"] = cmd["command"]
    return result
