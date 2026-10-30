"""Owned Claude recovery with explicit admission and durable delivery evidence."""

from __future__ import annotations

import asyncio
import copy
import json

from litetui import restored_input
from litetui.agent_store import _unlinked
from litetui.claude_persistence import ClaudeLedger, LedgerError, path_for
from litetui.conversation import ConversationRepository

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
        "can_cancel": state == "prepared", "can_resolve": state == "uncertain",
        "can_continue": False, "can_reconcile": False,
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
        row = _row(segment, entry)
        from litetui.claude_turn import launch_workspace

        row["can_continue"] = (
            entry["state"] == "prepared" and bool(entry["content"].strip())
            and item is app._pending_input[0] and app.backend.name == "claude"
            and getattr(app.backend, "owns_native_turns", False)
            and segment["workspace"] == launch_workspace(app)
            and all(candidate["state"] == "prepared" for candidate in disk.pending(segment["id"]))
        )
        rows.append(row)
        records[ident] = (item, entry)
    return rows, records, disk.document


def cancel(app, cmd, record):
    """Close one never-submitted delivery and verify the durable result before removal."""
    return _close(app, cmd, record, "prepared", "user_cancelled_before_send", "cancelled")


def resolve(app, cmd, record):
    """Record an explicit resolution; provider effects remain unverified."""
    result = _close(app, cmd, record, "uncertain", "user_resolved_without_replay", "resolved")
    result.update(replayed=False, effects_verified=False)
    return result


def _close(app, cmd, record, expected_state, reason, receipt_key):
    item, entry = record
    store = restored_input._owned(app, cmd.get("session_id"))
    ledger = app._claude_ledger
    if entry["state"] != expected_state:
        raise ValueError("Claude delivery is ineligible for this recovery action")
    # Called only after the shared snapshot and full journal comparison, with no await.
    try:
        saved = ledger.update_delivery(entry["id"], "terminal", stop_reason=reason)
        restored_input._owned(app, cmd.get("session_id"))
        disk = _read(store)
        if disk.document != ledger.document:
            raise OSError("Claude recovery action was not durably saved")
        segment = disk.segment(entry["segment_id"])
        if segment is None or sum(candidate == saved for candidate in segment["entries"]) != 1:
            raise OSError("Claude recovery action could not be verified")
    except Exception:
        app._restored_input_error = True
        raise
    app._pending_input[:] = [candidate for candidate in app._pending_input if candidate is not item]
    return {receipt_key: entry["id"], "session_id": app.convo_id}



def _verified_ledger(app, store, ledger):
    if (app._claude_ledger is not ledger
            or app._claude_ledger_path != store.convo_path.parent):
        raise ValueError("Claude continuation ledger identity changed")
    disk = _read(store)
    if disk.file != ledger.file or disk.document != ledger.document:
        raise OSError("Claude continuation was not durably saved")
    return disk


def _user_record(app, entry, *, required):
    """Verify one original prepared record in memory and on disk, or neither."""
    store = restored_input._owned(app, app.convo_id)
    meta, messages = ConversationRepository.read(store.convo_path)
    expected = {"role": "user", "content": entry["content"],
                "claude_delivery": {"id": entry["id"], "segment_id": entry["segment_id"],
                                    "state": "prepared"}}
    def matching(rows):
        return [message for message in rows
                if message.get("claude_delivery", {}).get("id") == entry["id"]]
    live, saved = matching(app.conversation), matching(messages)
    if (meta.get("id") != app.convo_id or live != saved or len(saved) > 1
            or required and not saved
            or saved and any(saved[0].get(key) != value for key, value in expected.items())):
        raise ValueError("Recovered Claude user record is missing, duplicated or changed")
    return bool(saved)


async def continue_input(app, cmd, record):
    """Admit one original prepared delivery, then schedule its existing native ID."""
    from litetui import hook_host, seat_authority, tool_policy

    item, original = record
    operation = restored_input._text(cmd.get("id"), 128)
    conversation, backend, model = app.conversation, app.backend, app.model_id
    owner, store, ledger = app._agent_session, app.store, app._claude_ledger
    session_id, segment_id = app.convo_id, original["segment_id"]
    selected = ledger.selected
    original = copy.deepcopy(original)
    app._validate_owned_execution()
    if getattr(app, "_mcp_maintenance", False) or getattr(app, "_gui_quitting", False):
        raise ValueError("Runtime maintenance or shutdown is active")
    delivery = {"content": original["content"], "text": original["content"],
                "tool_profile": original.get("profile"), "source": original.get("source"),
                "operation_id": operation, "_claude_entry": item["_claude_entry"],
                "_claude_segment": segment_id, "_claude_conversation": session_id,
                "_restored_claude_continuation": True}
    delivery["source"] = hook_host._turn_source(delivery)
    profile = delivery["tool_profile"]
    if profile is not None and profile not in tool_policy.PROFILES:
        raise ValueError("Invalid saved tool profile")

    def identity():
        restored_input._owned(app, session_id)
        app._validate_owned_execution()
        if (app.conversation is not conversation or app.backend is not backend
                or app.model_id != model or app._agent_session is not owner or app.store is not store
                or app._claude_ledger is not ledger or ledger.selected is None
                or ledger.selected["id"] != segment_id):
            raise ValueError("Conversation changed during Claude continuation admission")

    def receipt(outcome, *, worker=None, reason=None):
        result = restored_input._receipt(app, operation, original["id"], outcome,
                                         worker=worker, reason=reason)
        app._restored_input_attempt["claude"] = {
            "item": item, "original": original, "ledger": ledger, "segment": segment_id,
            "native_session": selected.get("session_id"), "workspace": selected["workspace"],
            "held_entry": copy.deepcopy(item["_claude_entry"]),
        }
        return result

    identity()
    _user_record(app, original, required=False)
    if delivery["source"] == "scheduled":
        refusal = seat_authority.withheld(app, profile or tool_policy.STRICT)
        if refusal:
            return receipt("refused", reason=refusal)

    def record_admission(outcome):
        saved = ledger.record_continuation(original["id"], operation, outcome)
        identity()
        _verified_ledger(app, store, ledger)
        item["_claude_entry"] = saved
        delivery["_claude_entry"] = saved

    try:
        record_admission("admitting")
        claimed = restored_input.inspect(app, session_id)["snapshot"]

        def before_accept():
            identity()
            fresh = restored_input.inspect(app, session_id)
            if (fresh["snapshot"] != claimed or not app._pending_input
                    or app._pending_input[0] is not item):
                raise ValueError("Recovered Claude input changed during admission")

        def persist_user(message):
            before_accept()
            expected = {"role": "user", "content": original["content"],
                        "claude_delivery": {"id": original["id"], "segment_id": segment_id,
                                            "state": "prepared"}}
            if message != expected:
                raise ValueError("Claude admission changed the original user record")
            if not _user_record(app, original, required=False):
                app._append(message)
            _user_record(app, original, required=True)

        before_accept()
        state = hook_host.snapshot(app)
        reasons: list[str] = []
        if not state.hooks and not state.error:
            accepted = hook_host.accept_prompt_origin(
                app, delivery, on_refused=reasons.append, persist_user=persist_user)
        else:
            accepted = await hook_host.admit_prompt_origin(
                app, delivery, on_refused=reasons.append, before_accept=before_accept,
                persist_user=persist_user)
        before_accept()
        if accepted is None:
            record_admission("refused")
            return receipt("refused", reason=reasons[-1] if reasons else "Prompt was not admitted")
        _user_record(app, original, required=True)
        record_admission("admitted")
        # accept_input copied delivery before this last admission write.
        app._claude_active_input["_claude_entry"] = delivery["_claude_entry"]
        # Keep the original held item as a queue barrier until readback. The
        # worker uses its separate active delivery. Idle flush must not start
        # later work between worker retirement and restoring an unsent hold.
        worker = app._stream(origin=accepted)
        return receipt("accepted", worker=worker)
    except (Exception, asyncio.CancelledError):
        app._restored_input_error = True
        raise


def settled(app, attempt):
    """Restore unresolved delivery evidence only after the exact worker retired."""
    context = attempt["claude"]
    item, original, ledger = context["item"], context["original"], context["ledger"]
    store = restored_input._owned(app, attempt["session_id"])
    try:
        disk = _verified_ledger(app, store, ledger)
        segment = disk.selected
        if (segment is None or segment["id"] != context["segment"]
                or segment["workspace"] != context["workspace"]
                or context["native_session"] and segment.get("session_id") != context["native_session"]):
            raise ValueError("Continued Claude segment changed before settlement")
        entry = next((candidate for candidate in segment["entries"]
                      if candidate["id"] == original["id"]), None)
        if entry is None or any(entry.get(key) != original.get(key)
                                for key in ("id", "segment_id", "content", "profile", "source", "operation_id")):
            raise ValueError("Continued Claude delivery changed before settlement")
        state = entry["state"]
        if state not in {"prepared", "uncertain", "terminal"}:
            raise ValueError("Continued Claude delivery has no settled evidence")
        if attempt["outcome"] == "refused" and state != "prepared":
            raise ValueError("Refused Claude admission changed delivery state")
        if attempt["outcome"] == "accepted":
            if (entry.get("continuation_operation_id") != attempt["operation"]
                    or entry.get("continuation_outcome") != "admitted"):
                raise ValueError("Continued Claude admission evidence changed")
            _user_record(app, original, required=True)
        queued = [candidate for candidate in app._pending_input
                  if candidate.get("_claude_entry", {}).get("id") == original["id"]]
        previous = context.get("settled_state")
        if previous is None:
            if (len(queued) != 1 or queued[0] is not item or not restored_input.held(item)
                    or item["_claude_entry"] != context["held_entry"]
                    or app._pending_input[0] is not item):
                raise ValueError("Continued Claude queue barrier changed")
            if state == "terminal":
                app._pending_input.pop(0)
            else:
                item["_claude_entry"] = entry
            context["settled_state"] = state
        elif (previous != state
              or state == "terminal" and queued
              or state != "terminal" and (len(queued) != 1 or queued[0] is not item
                                          or not restored_input.held(item) or item["_claude_entry"] != entry)):
            raise ValueError("Continued Claude settlement evidence changed")
        # Full mixed-provider inspection verifies the other holds before success.
        restored_input.inspect(app, attempt["session_id"])
        return {"continuation_provider": "claude", "continuation_state": state}
    except Exception:
        app._restored_input_error = True
        raise
