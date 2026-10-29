"""Owned inspection/cancellation of saved Claude input, without provider execution."""

from __future__ import annotations

import json

from litetui import restored_input
from litetui.agent_store import _unlinked
from litetui.claude_persistence import ClaudeLedger, LedgerError, path_for

_MAX_JOURNAL = 8 * 1024 * 1024


def _read(store):
    path = _unlinked(path_for(store.convo_path.parent))
    if path.exists() and path.stat().st_size > _MAX_JOURNAL:
        raise ValueError("Recovered Claude journal budget exceeded")
    try:
        ledger = ClaudeLedger(path.parent)
    except LedgerError as exc:
        raise ValueError(str(exc)) from exc
    document = ledger.document
    if len(json.dumps(document, allow_nan=False).encode("utf-8")) > _MAX_JOURNAL:
        raise ValueError("Recovered Claude journal budget exceeded")
    selected = ledger.selected
    if document.get("selected_segment") and selected is None:
        raise ValueError("Recovered Claude segment is missing")
    seen = set()
    for ident, segment in document["segments"].items():
        if segment.get("id") != ident:
            raise ValueError("Recovered Claude segment identity changed")
        for entry in segment["entries"]:
            key = restored_input._text(entry.get("id"), 128)
            if key in seen or entry.get("segment_id") != ident:
                raise ValueError("Duplicate or foreign Claude delivery identity")
            seen.add(key)
    return ledger


def _row(segment, entry):
    text = restored_input._text
    state = entry.get("state")
    if state not in {"prepared", "uncertain"}:
        raise ValueError("Unsupported recovered Claude delivery state")
    return {
        "id": text(entry.get("id"), 128), "provider": "claude", "revision": 0,
        "state": state, "content": text(entry.get("content"), 65536, empty=True),
        "source": text(entry.get("source") or "", 128, empty=True),
        "tool_profile": text(entry.get("profile") or "", 128, empty=True),
        "thread_id": text(segment.get("session_id") or "", 128, empty=True),
        "turn_id": "", "segment_id": text(segment.get("id"), 128),
        "workspace": text(segment.get("workspace"), 4096, empty=True),
        "uncertain_from": text(entry.get("uncertain_from") or "", 32, empty=True),
        "can_cancel": state == "prepared", "can_continue": False, "can_reconcile": False,
    }


def restore(app):
    """Discover selected-segment entries under the Rust hold policy; never queue sends."""
    if not getattr(app, "_hold_restored_input", False):
        return
    store = restored_input._owned(app, app.convo_id)
    ledger = _read(store)
    segment = ledger.selected
    if segment is None:
        return
    pending = ledger.pending(segment["id"])
    if len(pending) > 1000:
        raise ValueError("Too many recovered Claude inputs")
    # Validate the Claude projection and journal budget before altering the queue.
    rows = [_row(segment, entry) for entry in pending]
    restored_input.snapshot(app, app.convo_id, rows, ledger.document)
    queued = {item.get("_claude_entry", {}).get("id"): item for item in app._pending_input
              if "_claude_entry" in item}
    additions = []
    for entry in pending:
        existing = queued.get(entry["id"])
        if existing is not None:
            if (not restored_input.held(existing) or existing["_claude_entry"] != entry
                    or existing.get("_claude_conversation") != app.convo_id
                    or existing.get("_claude_segment") != segment["id"]):
                raise ValueError("Recovered Claude input conflicts with the live queue")
            continue
        item = {"content": entry["content"], "text": entry["content"],
                "tool_profile": entry.get("profile"), "source": entry.get("source"),
                "_claude_entry": entry, "_claude_segment": segment["id"],
                "_claude_conversation": app.convo_id}
        restored_input.mark(app, item)
        additions.append(item)
    app._claude_ledger, app._claude_ledger_path = ledger, store.convo_path.parent
    app._pending_input.extend(additions)


def inspect(app, store, items):
    """Compare the entire live ledger with disk before exposing eligible actions."""
    disk = _read(store)
    ledger = getattr(app, "_claude_ledger", None)
    if (ledger is None or getattr(app, "_claude_ledger_path", None) != store.convo_path.parent
            or ledger.file != disk.file or ledger.document != disk.document):
        raise ValueError("Recovered Claude journal changed; reopen the saved conversation")
    segment = disk.selected
    if segment is None:
        raise ValueError("Recovered Claude input has no selected segment")
    entries = {entry["id"]: entry for entry in segment["entries"]}
    rows, records = [], {}
    for item in items:
        entry = item.get("_claude_entry")
        if not isinstance(entry, dict):
            raise TypeError("Malformed recovered Claude input")
        ident = entry.get("id")
        if (ident in records or item.get("_claude_conversation") != app.convo_id
                or item.get("_claude_segment") != segment["id"]
                or entries.get(ident) != entry):
            raise ValueError("Recovered Claude input is stale or belongs to another session")
        rows.append(_row(segment, entry))
        records[ident] = (item, entry)
    return rows, records, disk.document


def cancel(app, cmd, record):
    """Close one never-submitted delivery and verify the durable result before removal."""
    item, entry = record
    store = restored_input._owned(app, cmd.get("session_id"))
    ledger = app._claude_ledger
    if entry["state"] != "prepared":
        raise ValueError("A possibly sent Claude input cannot be cancelled")
    # Called only after the shared snapshot and full journal comparison, with no await.
    try:
        saved = ledger.update_delivery(entry["id"], "terminal", stop_reason="user_cancelled_before_send")
        restored_input._owned(app, cmd.get("session_id"))
        disk = _read(store)
        if disk.document != ledger.document:
            raise OSError("Claude cancellation was not durably saved")
        segment = disk.segment(entry["segment_id"])
        if segment is None or sum(candidate == saved for candidate in segment["entries"]) != 1:
            raise OSError("Claude cancellation could not be verified")
    except Exception:
        app._restored_input_error = True
        raise
    app._pending_input[:] = [candidate for candidate in app._pending_input if candidate is not item]
    return {"cancelled": entry["id"], "session_id": app.convo_id}
