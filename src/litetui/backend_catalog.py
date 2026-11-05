"""Owned backend support metadata; never service health or model readiness.

The registry supplies names, labels, visibility and owned-engine controls. Codex
engine vocabulary matches the CLI: ``native`` means the active Codex object has
its canonical app_server capability; ``http`` means it does not. A requested
setting does not establish the effective engine, nor does either prove readiness.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass, is_dataclass
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
    if (getattr(app, "model_id", None) or None) != authority.model:
        raise ValueError("Active model disagrees with owned agent authority")
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



def _encoded(value, limit):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    if len(data) > limit:
        raise ValueError("Backend snapshot exceeds its byte budget")
    return data


def _disk_hash(path, limit):
    from litetui.agent_store import _unlinked

    path = _unlinked(path)
    if not path.exists():
        return None
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError("Backend snapshot file is invalid or oversized")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    after = _unlinked(path).stat()
    if (len(data) > limit or
            (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) !=
            (after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev)):
        raise ValueError("Backend snapshot file changed while reading")
    return hashlib.sha256(data).hexdigest()


def fingerprint(app, context, result, *, include_pending=True):
    """Bind a selection to current owned disk, runtime, policy and launch facts."""
    from litetui import store_identity
    from litetui.gui_rpc import _tool_policy

    launch = getattr(app, "_launch_options", None)
    facts = {
        "catalog": result, "settings": asdict(context.settings),
        "authority": asdict(context.authority), "tool_policy": _tool_policy(app),
        "store_identity": store_identity.capture(app), "cwd": os.getcwd(),
        "launch": asdict(launch) if is_dataclass(launch) and not isinstance(launch, type) else None,
        "launch_backend": getattr(app, "_launch_backend", None),
        "launch_overrides": getattr(app, "_launch_overrides", {}),
        "invocation_saved": getattr(app, "_invocation_saved_values", {}),
        "cli_backend": getattr(app, "_cli_initial_backend", None),
        "cli_model": getattr(app, "_cli_initial_model", None),
        "cli_thinking": getattr(app, "_cli_thinking_level", None),
        "effective_thinking": getattr(app, "_cli_effective_thinking", None),
        "thinking": getattr(app, "_thinking_level", None),
        "hold": getattr(app, "_hold_restored_input", False),
        "pending_count": len(getattr(app, "_pending_input", ())) if include_pending else None,
        "history": hashlib.sha256(_encoded(app.conversation, 8 * 1024 * 1024)).hexdigest(),
        "files": [
            _disk_hash(context.session.store.data_root / "settings.json", 1024 * 1024),
            _disk_hash(context.session.memory_root / "settings.json", 1024 * 1024),
            _disk_hash(context.store.convo_dir / "settings.json", 1024 * 1024),
            _disk_hash(context.store.convo_path, 8 * 1024 * 1024),
            _disk_hash(context.store.convo_dir / "claude_ledger.json", 8 * 1024 * 1024),
        ],
    }
    return hashlib.sha256(_encoded(facts, 1024 * 1024)).hexdigest()

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
        "selected_model": _text(app.model_id, 4096) if app.model_id else "",
        "codex_engine": {"requested_native": before.requested_native, "effective": before.effective},
        "entries": rows,
    }
    snapshot = fingerprint(app, before, result)
    after = _context(app, before.session_id)
    if (any(getattr(after, field) is not getattr(before, field)
            for field in ("session", "store", "backend", "settings"))
            or after != before):
        raise ValueError("Backend catalog context changed while reading; refresh before continuing")
    if fingerprint(app, after, result) != snapshot:
        raise ValueError("Backend catalog changed while reading; refresh before continuing")
    result["snapshot"] = snapshot
    if len(json.dumps(result, allow_nan=False).encode("utf-8")) > MAX_RESPONSE:
        raise ValueError("Backend catalog exceeds the response byte budget")
    return result
