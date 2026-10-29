"""Launch-local holds and owned, revision-checked recovery controls."""

import hashlib
import json

from litetui.conversation import ConversationRepository

_STATES = {"queued", "admitting", "admitted", "next_turn", "sending", "uncertain", "accepted"}
_CANCELLABLE = {"queued", "next_turn"}


def held(item):
    return item.get("_restored_input_held") is True


def mark(app, item):
    if getattr(app, "_hold_restored_input", False):
        item["_restored_input_held"] = True


def status(app):
    return {
        "policy": "hold" if getattr(app, "_hold_restored_input", False) else "automatic",
        "held": sum(held(item) for item in getattr(app, "_pending_input", ())),
    }


def _records(messages):
    latest: dict[str, tuple[dict, list, int]] = {}
    for index, message in enumerate(messages):
        entries = (message.get("provider_metadata") or {}).get("steering", [])
        for entry in entries:
            ident = entry.get("id")
            revision = entry.get("revision", 0)
            if type(revision) is not int or not 0 <= revision < 2**53:
                raise ValueError("Invalid recovered input revision")
            previous = latest.get(ident)
            if previous is None or revision >= previous[0].get("revision", 0):
                latest[ident] = (entry, entries, index)
    return latest


def _text(value, limit, *, empty=False):
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit or not (value or empty):
        raise ValueError("Invalid recovered input text")
    return value


def _owned(app, session_id):
    store = app.store
    session = getattr(app, "_agent_session", None)
    if (session is None or store.agent_session is not session
            or not session_id or session_id != app.convo_id or session_id != store.convo_id
            or store.loading or store.persist_error
            or getattr(app, "_restored_input_error", False)):
        raise ValueError("Recovered input requires the current owned, durably saved conversation")
    store.acquire()
    if not store.owned or not getattr(app, "_hold_restored_input", False):
        raise ValueError("Recovered input controls require an owned hold-policy runtime")
    return store


def _inspect(app, session_id):
    store = _owned(app, session_id)
    items = [item for item in app._pending_input if held(item)]
    if len(items) > 1000:
        raise ValueError("Too many recovered inputs to inspect")
    current = _records(app.conversation)
    persisted = {}
    if items:
        meta, messages = ConversationRepository.read(store.convo_path)
        if meta.get("id") != session_id:
            raise ValueError("Recovered input transcript identity changed")
        persisted = _records(messages)
    materialized = {m.get("codex_delivery", {}).get("id") for m in app.conversation}
    rows, records, seen = [], {}, set()
    for item in items:
        entry, ledger = item.get("_codex_entry"), item.get("_codex_ledger")
        if not isinstance(entry, dict) or ledger is None:
            raise ValueError("Unsupported recovered input journal")
        ident = _text(entry.get("id"), 128)
        record = current.get(ident)
        saved = persisted.get(ident)
        if (ident in seen or ident in materialized or record is None
                or record[0] is not entry or record[1] is not ledger.entries
                or not any(candidate is entry for candidate in ledger.entries)
                or entry.get("conversationId") not in (None, session_id)
                or saved is None or saved[0] != entry):
            raise ValueError("Recovered input is stale, foreign or differs from its saved journal")
        seen.add(ident)
        state = entry.get("state")
        if state not in _STATES:
            raise ValueError("Invalid recovered input state")
        payload = entry.get("item")
        if not isinstance(payload, dict):
            raise TypeError("Invalid recovered input payload")
        content = payload.get("content", payload.get("text", ""))
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, sort_keys=True, allow_nan=False)
        rows.append({
            "id": ident, "revision": entry.get("revision", 0), "state": state,
            "thread_id": _text(entry.get("threadId") or "", 128, empty=True),
            "turn_id": _text(entry.get("turnId") or "", 128, empty=True),
            "content": _text(content, 65536, empty=True),
            "source": _text(payload.get("source", ""), 128, empty=True),
            "tool_profile": _text(payload.get("tool_profile", ""), 128, empty=True),
            "can_cancel": state in _CANCELLABLE,
        })
        records[ident] = (item, record)
    result = {"session_id": session_id, "backend": app.backend.name, "model": app.model_id,
              "policy": "hold", "held": len(rows), "entries": rows}
    encoded = json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 262144:
        raise ValueError("Recovered input inspection budget exceeded")
    journal = json.dumps([record[1][0] for record in records.values()],
                         sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(journal) > 8 * 1024 * 1024:
        raise ValueError("Recovered input journal budget exceeded")
    result["snapshot"] = hashlib.sha256(encoded + b"\0" + journal).hexdigest()
    return result, records


def inspect(app, session_id):
    """No provider calls; only entries still owned by this live conversation."""
    return _inspect(app, session_id)[0]


def cancel(app, cmd):
    """Persist denial before removing a never-sent item. Never retry on failure."""
    from litetui.codex_steering import SteeringLedger, save_at

    result, records = _inspect(app, cmd.get("session_id"))
    if cmd.get("snapshot") != result["snapshot"]:
        raise ValueError("Recovered input changed; refresh before cancelling")
    row = next((row for row in result["entries"] if row["id"] == cmd.get("entry_id")), None)
    if row is None or not row["can_cancel"]:
        raise ValueError("Only queued or explicitly rejected next-turn input can be cancelled")
    item, (entry, entries, index) = records[row["id"]]
    # Build persistence against the CURRENT metadata index; restored callbacks
    # may have captured an index before a conversation edit or open.
    ledger = SteeringLedger(entries, lambda: save_at(app, index))
    try:
        ledger.transition(entry, "denied", admission={"reason": "Cancelled by user"})
        meta, messages = ConversationRepository.read(app.store.convo_path)
        saved = _records(messages).get(row["id"])
        if meta.get("id") != result["session_id"] or saved is None or saved[0] != entry:
            raise OSError("Cancellation could not be verified in the saved journal")
    except Exception:
        # transition mutates before saving. Do not expose that memory as durable
        # evidence or remove the held item after an ambiguous storage failure.
        app._restored_input_error = True
        raise
    app._pending_input[:] = [candidate for candidate in app._pending_input if candidate is not item]
    return {"cancelled": row["id"], "session_id": result["session_id"]}
