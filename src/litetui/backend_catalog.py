"""Owned backend support metadata; never service health or model readiness.

The registry supplies names, labels, visibility and owned-engine controls. Codex
engine vocabulary matches the CLI: ``native`` means the active Codex object has
its canonical app_server capability; ``http`` means it does not. A requested
setting does not establish the effective engine, nor does either prove readiness.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from litetui import llm_backend
from litetui.agent_store import valid_id

VERSION = 1
MAX_BACKENDS = 32
MAX_RESPONSE = 64 * 1024
MAX_NAME = 128
MAX_LABEL = 1024


@dataclass(frozen=True)
class _Context:
    session: Any
    store: Any
    backend: Any
    settings: Any
    authority: Any
    session_id: str
    selected: str
    configured: str
    requested_native: bool
    effective: str | None


def _text(value, limit):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise ValueError("Backend catalog contains invalid or oversized text")
    return value


def _context(app, expected):
    from litetui.gui_rpc import _idle

    _idle(app)
    current = valid_id(expected)
    session = getattr(app, "_agent_session", None)
    store = app.store
    if (session is None or store.agent_session is not session or current != app.convo_id
            or current != store.convo_id or store.loading or store.persist_error
            or not store.pending and not store.owned):
        raise ValueError("Backend catalog requires the current owned session")
    authority = session.authority
    directory = session.conversation_directory(current)
    if store.convo_dir != directory or store.convo_path != directory / "convo.jsonl":
        raise ValueError("Current conversation path disagrees with its owned session")
    backend, settings = app.backend, app.settings
    selected = _text(getattr(backend, "name", None), MAX_NAME)
    configured = _text(settings.backend, MAX_NAME)
    if authority.backend != selected:
        raise ValueError("Active backend disagrees with owned agent authority")
    requested = settings.codex_native_engine
    if type(requested) is not bool:
        raise ValueError("Configured Codex engine selection must be a boolean")
    effective = ("native" if hasattr(backend, "app_server") else "http") if selected == "codex" else None
    return _Context(session, store, backend, settings, authority, current,
                    selected, configured, requested, effective)


def _rows(selected):
    registry = {}
    for number, pair in enumerate(llm_backend.BACKENDS):
        if number >= MAX_BACKENDS:
            raise ValueError("Backend registry exceeds the catalog entry budget")
        name, label = pair
        name, label = _text(name, MAX_NAME), _text(label, MAX_LABEL)
        if name in registry:
            raise ValueError("Backend registry contains duplicate names")
        registry[name] = label
    if not registry or not llm_backend.ENGINE_BACKENDS.issubset(registry):
        raise ValueError("Backend registry has invalid owned-engine metadata")
    rows, seen = [], set()
    for number, pair in enumerate(llm_backend.visible_backends()):
        if number >= MAX_BACKENDS:
            raise ValueError("Visible backends exceed the catalog entry budget")
        name, label = pair
        name, label = _text(name, MAX_NAME), _text(label, MAX_LABEL)
        if name in seen or registry.get(name) != label:
            raise ValueError("Visible backend catalog disagrees with the source registry")
        seen.add(name)
        rows.append({"name": name, "label": label, "selected": name == selected,
                     "owned_engine_controls": name in llm_backend.ENGINE_BACKENDS})
    if selected not in seen:
        raise ValueError("Active backend is not visible in the source catalog; choose a supported backend explicitly")
    return rows


def catalog(app, cmd):
    """Read bounded source metadata for this owned idle session without mutation."""
    before = _context(app, cmd.get("session_id"))
    rows = _rows(before.selected)
    if before.configured not in {row["name"] for row in rows}:
        raise ValueError("Configured backend is not visible in the source catalog; choose a supported backend explicitly")
    result = {
        "version": VERSION, "kind": "support_metadata", "readiness_checked": False,
        "session_id": before.session_id, "agent_id": valid_id(before.authority.agent_id),
        "agent_name": _text(before.authority.name, MAX_LABEL),
        "selected_backend": before.selected, "configured_backend": before.configured,
        "codex_engine": {"requested_native": before.requested_native, "effective": before.effective},
        "entries": rows,
    }
    after = _context(app, before.session_id)
    if (any(getattr(after, field) is not getattr(before, field)
            for field in ("session", "store", "backend", "settings"))
            or after != before):
        raise ValueError("Backend catalog context changed while reading; refresh before continuing")
    if len(json.dumps(result, allow_nan=False).encode("utf-8")) > MAX_RESPONSE:
        raise ValueError("Backend catalog exceeds the response byte budget")
    return result
