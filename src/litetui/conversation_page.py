"""Bounded read-only catalog for one currently owned agent's native browser."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from litetui.agent_store import StoreError, _unlinked, valid_id
from litetui.conversation import ConversationRepository

PAGE_SIZE = 50
MAX_ENTRIES = 10_000
MAX_RESPONSE = 256 * 1024
# Leave room for framing beneath the native two-MiB line limit.
MAX_PREVIEW = 1024 * 1024
MAX_TRANSCRIPT = 8 * 1024 * 1024
MAX_PAGE_READ = 32 * 1024 * 1024
MAX_SETTINGS = 64 * 1024
_POLICY_KEYS = ("tools_enabled", "mcp_enabled", "tool_policy_profile", "tool_always_allow",
                "tool_deny", "tool_trusted_interpreters", "tools_disabled", "mcp_disabled_servers")


@dataclass(frozen=True)
class _File:
    session_id: str
    path: Path
    stamp: tuple[int, int, int, int, int, int]
    updated_at: float
    settings_stamp: tuple[int, int, int, int, int, int] | None


def _text(value, limit, *, empty=False):
    if (not isinstance(value, str) or not (value or empty)
            or len(value.encode("utf-8")) > limit):
        raise ValueError("Invalid or oversized conversation catalog text")
    return value


def _scope(app, expected):
    from litetui.gui_rpc import _idle

    _idle(app)
    session = getattr(app, "_agent_session", None)
    store = app.store
    current = valid_id(expected)
    if (session is None or store.agent_session is not session or current != app.convo_id
            or current != store.convo_id or store.loading or store.persist_error
            or not store.pending and not store.owned):
        raise ValueError("Conversation catalog requires the current owned session")
    authority = session.authority
    directory = session.conversation_directory(current)
    if store.convo_dir != directory or store.convo_path != directory / "convo.jsonl":
        raise ValueError("Current conversation path disagrees with its owned session")
    return session, store, authority


def _stamp(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not math.isfinite(info.st_mtime) or info.st_mtime < 0:
        raise ValueError("Invalid conversation transcript metadata")
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino, info.st_mode), info.st_mtime


def _catalog(app, expected):
    session, store, authority = _scope(app, expected)
    root = _unlinked(session.conversation_directory(expected).parent)
    records = []
    if root.exists():
        with os.scandir(root) as entries:
            for number, entry in enumerate(entries):
                if number >= MAX_ENTRIES:
                    raise ValueError("Conversation catalog exceeds the entry budget")
                directory = _unlinked(Path(entry.path))
                if not directory.is_dir():
                    continue
                try:
                    ident = valid_id(directory.name)
                except StoreError:
                    continue  # A malformed directory is never an owned selector.
                path = _unlinked(directory / "convo.jsonl")
                if not path.exists():
                    continue  # Staged/empty directories are not saved conversations.
                stamp, updated = _stamp(path)
                settings_path = _unlinked(directory / "settings.json")
                settings_stamp = _stamp(settings_path)[0] if settings_path.exists() else None
                records.append(_File(ident, path, stamp, updated, settings_stamp))
    records.sort(key=lambda record: (-record.stamp[1], record.session_id))
    from litetui.gui_rpc import _tool_policy

    policy = {"facts": _tool_policy(app), "backend": app.backend.name, "model": app.model_id,
              "restrictions": {key: getattr(app.settings, key) for key in _POLICY_KEYS},
              "hold": getattr(app, "_hold_restored_input", False) is True}
    if len(_encoded(policy)) > MAX_SETTINGS:
        raise ValueError("Current runtime policy exceeds the browser snapshot budget")
    document = {"session_id": expected, "agent_id": authority.agent_id,
                "agent_name": authority.name, "policy": policy,
                "files": [(record.session_id, record.stamp, record.settings_stamp) for record in records]}
    snapshot = hashlib.sha256(_encoded(document)).hexdigest()
    if _scope(app, expected) != (session, store, authority):
        raise ValueError("Conversation catalog ownership changed")
    return session, store, authority, records, snapshot


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _cursor(session_id, agent_id, snapshot, offset):
    value = {"v": 1, "session_id": session_id, "agent_id": agent_id,
             "snapshot": snapshot, "offset": offset}
    return base64.urlsafe_b64encode(_encoded(value)).decode("ascii")


def _offset(cursor, session_id, agent_id, snapshot, total):
    if cursor is None:
        return 0
    text = _text(cursor, 1024)
    try:
        raw = base64.b64decode(text, altchars=b"-_", validate=True)
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid conversation page cursor") from exc
    if not isinstance(value, dict):
        raise TypeError("Invalid conversation page cursor")
    offset = value.get("offset")
    if (type(offset) is not int or offset < 0 or offset % PAGE_SIZE
            or offset >= total and offset != 0
            or text != _cursor(session_id, agent_id, snapshot, offset)):
        raise ValueError("Conversation page cursor is stale or belongs to another scope")
    return offset


def _compatibility(app, authority, record):
    """Preview only the authority settings this native browser can preserve.

    Owned-agent backend/model authority overrides historical conversation values.
    Use the same coercion and invocation precedence as _adopt_convo_settings;
    legacy readers keep their tolerant config behavior outside this browser.
    """
    from dataclasses import fields

    from litetui import convo_settings, seat_authority, settings

    cs = convo_settings.ConvoSettings()
    path = _unlinked(record.path.parent / "settings.json")
    if record.settings_stamp is not None:
        if record.settings_stamp[0] > MAX_SETTINGS:
            return "Saved conversation settings exceed the browser read budget"
        try:
            with path.open("rb") as source:
                payload = source.read(MAX_SETTINGS + 1)
            if len(payload) > MAX_SETTINGS:
                return "Saved conversation settings exceed the browser read budget"
            raw = json.loads(payload)
            if not isinstance(raw, dict):
                return "Saved conversation settings are invalid"
            known = {field.name for field in fields(cs)}
            for key, value in raw.items():
                if key not in known:
                    continue
                valid = (isinstance(value, dict) if key in ("execution", "llama_load", "lmstudio_load")
                         else type(value) is int and value in (1, 2) if key == "schema_version"
                         else value is None or type(value) is bool if key == "seat_spawned"
                         else value is None or isinstance(value, str))
                if not valid:
                    return "Saved conversation settings are invalid"
                setattr(cs, key, value)
        except (OSError, ValueError):
            return "Saved conversation settings cannot be read"
    if authority.backend != app.backend.name:
        return "Owned agent backend differs from the active runtime"
    candidate = copy.deepcopy(app.settings)
    for key in _POLICY_KEYS:
        value = getattr(candidate, key)
        if key in cs.execution:
            value = settings._coerce(key, cs.execution[key], value)
        env = settings.ENV_OVERRIDES.get(key)
        if env and os.environ.get(env):
            value = settings._coerce(key, os.environ[env], value)
        if key in getattr(app, "_invocation_saved_values", {}) and key in getattr(app, "_launch_overrides", {}):
            value = copy.deepcopy(app._launch_overrides[key])
        setattr(candidate, key, value)
        if key != "tool_policy_profile" and value != getattr(app.settings, key):
            return f"Saved conversation changes {key.replace('_', ' ')}; reopen in a matching runtime"
    if not getattr(app, "_cli_tool_profile", None):
        profile = convo_settings.resolved(cs, candidate, "tool_policy_profile")
        if profile != seat_authority.seat_profile(app):
            return "Saved conversation changes the tool profile; reopen with an explicit matching profile"
    marker = getattr(app, "_spawned_marker", None)
    if marker is not None and (marker or cs.seat_spawned is True) != getattr(app, "_spawned_seat", True):
        return "Saved conversation changes launch provenance; reopen explicitly"
    return ""


def _summary(app, authority, record, remaining):
    if record.stamp[0] > min(MAX_TRANSCRIPT, remaining):
        raise ValueError("Conversation page exceeds the transcript read budget")
    meta, messages = ConversationRepository.read(record.path, max_bytes=min(MAX_TRANSCRIPT, remaining))
    if meta.get("id") != record.session_id:
        raise ValueError("Conversation transcript identity disagrees with its directory")
    from litetui.native_history import enabled as paged_history
    current = record.session_id == app.convo_id
    writable = bool(record.stamp[5] & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    reason = ("Already open" if current else
              "Conversation file is read-only" if not writable else
              "Restart with restored-input holds before opening saved conversations"
              if getattr(app, "_hold_restored_input", False) is not True else
              "Native browser history requires 1..1000 saved messages"
              if not paged_history(app) and not 1 <= len(messages) <= 1000 else
              "Native browser history supports at most 100 visible messages"
              if not paged_history(app) and sum(message.get("role") != "system" for message in messages) > 100 else
              _compatibility(app, authority, record))
    row = {"session_id": record.session_id, "agent_id": valid_id(authority.agent_id),
           "agent_name": _text(authority.name, 1024),
           "title": _text(ConversationRepository.label(meta, messages), 4096),
           "message_count": len(messages), "updated_at": record.updated_at,
           "current": current, "can_open": not reason, "reason": reason}
    if _stamp(_unlinked(record.path))[0] != record.stamp:
        raise ValueError("Conversation changed while reading its catalog page")
    return row


def _unchanged(app, session_id, before):
    after = _catalog(app, session_id)
    if (after[0] is not before[0] or after[1] is not before[1]
            or after[2] != before[2] or after[4] != before[4]):
        raise ValueError("Conversation catalog changed; refresh before continuing")


def page(app, cmd):
    """Stat the bounded catalog, replay only this page, and never write/repair."""
    current = valid_id(cmd.get("session_id"))
    before = _catalog(app, current)
    _, _, authority, records, snapshot = before
    offset = _offset(cmd.get("cursor"), current, authority.agent_id, snapshot, len(records))
    rows = []
    remaining = MAX_PAGE_READ
    for record in records[offset:offset + PAGE_SIZE]:
        rows.append(_summary(app, authority, record, remaining))
        remaining -= record.stamp[0]
    _unchanged(app, current, before)
    end = offset + len(rows)
    result = {"session_id": current, "agent_id": authority.agent_id, "snapshot": snapshot,
              "offset": offset, "total": len(records), "entries": rows,
              "previous_cursor": _cursor(current, authority.agent_id, snapshot, max(0, offset - PAGE_SIZE))
              if offset else None,
              "next_cursor": _cursor(current, authority.agent_id, snapshot, end) if end < len(records) else None}
    if len(json.dumps(result, allow_nan=False).encode("utf-8")) > MAX_RESPONSE:
        raise ValueError("Conversation page exceeds the response byte budget")
    return result


def validate_open(app, cmd):
    """Fresh selection guard for native callers; the existing open owns mutation."""
    current = valid_id(cmd.get("from_session_id"))
    target = valid_id(cmd.get("session_id"))
    token = _text(cmd.get("snapshot"), 64)
    before = _catalog(app, current)
    _, _, authority, records, snapshot = before
    if token != snapshot:
        raise ValueError("Conversation selection is stale; refresh before opening")
    record = next((record for record in records if record.session_id == target), None)
    if record is None:
        raise ValueError("Conversation selection is outside the owned catalog")
    row = _summary(app, authority, record, MAX_TRANSCRIPT)
    if not row["can_open"]:
        raise ValueError(row["reason"])
    _unchanged(app, current, before)
    return record, before



def preview(app, cmd):
    """Read selected history for native display validation, without adopting it.

    The subsequent open must repeat the selection guard. This response grants no
    connection or ownership transition, and cannot release restored input.
    """
    record, before = validate_open(app, cmd)
    meta, messages = ConversationRepository.read(record.path, max_bytes=MAX_TRANSCRIPT)
    if meta.get("id") != record.session_id:
        raise ValueError("Conversation preview identity disagrees with its directory")
    _unchanged(app, cmd["from_session_id"], before)
    result = {"session_id": record.session_id, "from_session_id": cmd["from_session_id"],
              "agent_id": before[2].agent_id, "snapshot": before[4],
              "meta": meta, "messages": messages, "read_only": False}
    # Match rpc_emit's ensure_ascii default: non-ASCII wire escapes cost bytes.
    if len(json.dumps(result, allow_nan=False).encode("utf-8")) > MAX_PREVIEW:
        raise ValueError("Conversation preview exceeds the response byte budget")
    return result
