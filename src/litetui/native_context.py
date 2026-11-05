"""Read one fixed context document under the current owned native Work identity."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

from litetui import background_input as work
from litetui.agent_store import MEMORY_FILES, _unlinked

OPERATION = "gui.context.native_page"
MAX_DOCUMENT = 262144
PAGE_BYTES = 4096
DOCUMENTS = MEMORY_FILES | {"system", "hooks", "plugins"}


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_nlink, info.st_mode, info.st_size, info.st_mtime_ns


def _memory(app, document):
    if getattr(app, "_owner_memory_dir", None) is not None:
        raise ValueError("Owner memory overrides are outside this current-agent view")
    return _regular_file(app._agent_session.memory_root / document)


def _regular_file(path):
    path = _unlinked(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return "missing", None
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or getattr(before, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("Context requires a regular unlinked file")
    if before.st_size > MAX_DOCUMENT:
        return "oversize", None
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if _stamp(before) != _stamp(opened):
            raise ValueError("Context file changed before it could be read")
        data = stream.read(MAX_DOCUMENT + 1)
        finished = os.fstat(stream.fileno())
    after = _unlinked(path).lstat()
    if not (_stamp(before) == _stamp(finished) == _stamp(after)) or len(data) != before.st_size:
        raise ValueError("Context file changed while reading")
    return "content", data


def _hooks(app):
    from litetui import lifecycle_hooks as hooks

    config = getattr(app, "hook_config", None)
    if not isinstance(config, hooks.HookConfig):
        raise ValueError("Lifecycle hook configuration is unavailable")  # noqa: TRY004 - unavailable source state

    def read_scope(scope):
        status, data = _regular_file(config.path(scope))
        if status == "missing":
            return ()
        if status == "oversize":
            raise OverflowError
        return config.parse_document(json.loads(data.decode("utf-8-sig")), scope)

    try:
        snapshot = config.snapshot(read_scope=read_scope)
    except OverflowError:
        return "oversize", None
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError("Lifecycle hook configuration could not be inspected") from exc
    if snapshot.error:
        raise ValueError("Invalid lifecycle hook configuration")
    if snapshot.disabled:
        return "content", b"Lifecycle hooks are off (LITETUI_HOOKS=off)."
    if not snapshot.hooks:
        return "content", b"No lifecycle hooks are configured."
    lines = ["Effective lifecycle hooks (project IDs override global IDs)", ""]
    for hook in snapshot.hooks:
        state = "enabled" if hook.enabled else "disabled"
        # Quote IDs to keep control characters from impersonating another entry.
        lines.append(f"{hook.scope}/{json.dumps(hook.id)}  {state}  {hook.mode}")
        lines.append("  Events: " + ", ".join(dict.fromkeys(hook.events)))
        if hook.sources:
            lines.append("  Sources: " + ", ".join(dict.fromkeys(hook.sources)))
        if hook.tools:
            lines.append(f"  Tool filters: {len(hook.tools)}")
    data = "\n".join(lines).encode("utf-8")
    return ("oversize", None) if len(data) > MAX_DOCUMENT else ("content", data)


def _plugins(app):
    from litetui import plugins
    from litetui.friendly_errors import display_error

    if not isinstance(app.plugins, plugins.PluginRegistry):
        raise TypeError("Current plugin registry is unavailable")
    statuses = app.plugins.status
    if len(statuses) > 1024:
        return "oversize", None
    size = 0
    for name, status in statuses.items():
        if type(name) is not str or type(status) is not str:
            raise ValueError("Invalid plugin status")
        size += len(name) + len(status)
        if size > MAX_DOCUMENT:
            return "oversize", None
    lines = [f"{len(statuses)} plugin(s), load order:"]
    for name, raw in statuses.items():
        # Match /plugins' selected error style without writing another runtime-log entry on every read.
        status = display_error(raw, app.settings.error_message_style, surface="plugin")
        # Keep each row on one line even if an import error contains terminal controls.
        lines.append(f"{json.dumps(name, ensure_ascii=False)}  {json.dumps(status, ensure_ascii=False)}")
    data = "\n".join(lines).encode("utf-8")
    return ("oversize", None) if len(data) > MAX_DOCUMENT else ("content", data)


def _system(app):
    # Same first-system-row selection as gui.system.get. Read only the actual
    # conversation row; rendering prompt sections would discard its store snapshot.
    row = next((message for message in app.conversation if message.get("role") == "system"), None)
    if row is None:
        return "missing", None
    value = row.get("content")
    if not isinstance(value, str):
        raise ValueError("Current system content must be plain text")  # noqa: TRY004 - invalid source evidence
    if len(value) > MAX_DOCUMENT:  # Each Unicode code point encodes at least one byte.
        return "oversize", None
    data = value.encode("utf-8")
    return ("oversize", None) if len(data) > MAX_DOCUMENT else ("content", data)


def read_binding(app, cmd, operation, *fields):
    bridge = work.bridge(app)
    if bridge is None or not bridge.active or getattr(app, "_gui_quitting", False):
        raise ValueError("Inspection requires an active, owned Work session")
    bridge.check()
    app._validate_owned_identity()
    binding = {**bridge.identity, "work_instance": bridge.instance}
    work._keys(cmd, ("type", "id", "version", *binding, *fields))
    if (cmd["type"] != operation or type(cmd["version"]) is not int or cmd["version"] != 1
            or any(cmd[key] != value for key, value in binding.items())):
        raise ValueError("Inspection belongs to another version or Work session")
    work.text(cmd["id"])
    return bridge, binding


def page(app, cmd):
    bridge, binding = read_binding(app, cmd, OPERATION, "document", "offset", "revision")
    document, offset, expected = cmd["document"], cmd["offset"], cmd["revision"]
    if type(document) is not str or document not in DOCUMENTS:
        raise ValueError("Unknown current context document")
    if type(offset) is not int or not 0 <= offset < MAX_DOCUMENT or (expected is None and offset != 0):
        raise ValueError("Invalid context byte offset")
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        raise ValueError("Invalid context document revision")
    if document == "system":
        status, data = _system(app)
    elif document == "hooks":
        status, data = _hooks(app)
    elif document == "plugins":
        status, data = _plugins(app)
    else:
        status, data = _memory(app, document)
    bridge.check()
    response = {"version": 1, "kind": "context_page", **binding, "document": document, "offset": offset,
                "status": status, "revision": None, "total_bytes": None, "next_offset": None, "text": ""}
    revision = hashlib.sha256(data).hexdigest() if data is not None else None
    if expected is not None and revision != expected:
        response["status"] = "changed"
        return response
    if data is None:
        return response
    data.decode("utf-8")  # Validate the whole bounded document, including pages after this one.
    response.update(revision=revision, total_bytes=len(data))
    if not data:
        if offset:
            raise ValueError("Empty documents start at offset zero")
        response["status"] = "empty"
        return response
    if offset >= len(data) or data[offset] & 0xC0 == 0x80:
        raise ValueError("Context offset is outside the document or splits a UTF-8 code point")
    end = min(offset + PAGE_BYTES, len(data))
    while end < len(data) and data[end] & 0xC0 == 0x80:
        end -= 1
    response.update(status="page", text=data[offset:end].decode("utf-8"),
                    next_offset=end if end < len(data) else None)
    return response
