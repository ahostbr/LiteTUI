"""Launch-local holds and owned, revision-checked recovery controls."""

import asyncio
import copy
import hashlib
import json

from litetui.conversation import ConversationRepository

_STATES = {"queued", "admitting", "admitted", "next_turn", "sending", "uncertain", "accepted"}
_CANCELLABLE = {"queued", "next_turn"}
_RECONCILABLE = {"admitting", "admitted", "sending", "uncertain", "accepted"}


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
    claude_items = [item for item in items if "_claude_entry" in item]
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
        if "_claude_entry" in item:
            continue
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
            "can_reconcile": (state in _RECONCILABLE and app.backend.name == "codex"
                              and hasattr(app.backend, "app_server")
                              and bool(entry.get("threadId")) and bool(entry.get("turnId"))
                              and isinstance(payload.get("content"), (str, list))),
            "can_continue": (state in _CANCELLABLE and item is app._pending_input[0]
                             and app.backend.name == "codex" and hasattr(app.backend, "app_server")
                             and isinstance(payload.get("content"), (str, list))
                             and bool(payload["content"].strip() if isinstance(payload["content"], str) else payload["content"])),
        })
        records[ident] = (item, record)
    journal = [record[1][0] for record in records.values()]
    if claude_items:
        from litetui import restored_claude

        extra, claude_records, document = restored_claude.inspect(app, store, claude_items)
        if any(row["id"] in seen for row in extra):
            raise ValueError("Recovered provider input identity collision")
        rows.extend(extra)
        records.update(claude_records)
        journal.append(document)
        order = {id(item): n for n, item in enumerate(items)}
        rows.sort(key=lambda row: order[id(records[row["id"]][0])])
    return snapshot(app, session_id, rows, journal), records


def snapshot(app, session_id, rows, journal):
    result = {"session_id": session_id, "backend": app.backend.name, "model": app.model_id,
              "policy": "hold", "held": len(rows), "entries": rows}
    encoded = json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 262144:
        raise ValueError("Recovered input inspection budget exceeded")
    journal = json.dumps(journal, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(journal) > 8 * 1024 * 1024:
        raise ValueError("Recovered input journal budget exceeded")
    result["snapshot"] = hashlib.sha256(encoded + b"\0" + journal).hexdigest()
    return result


def inspect(app, session_id):
    """No provider calls; only entries still owned by this live conversation."""
    return _inspect(app, session_id)[0]


def cancel(app, cmd):
    """Persist denial before removing a never-sent item. Never retry on failure."""
    result, records = _inspect(app, cmd.get("session_id"))
    if cmd.get("snapshot") != result["snapshot"]:
        raise ValueError("Recovered input changed; refresh before cancelling")
    row = next((row for row in result["entries"] if row["id"] == cmd.get("entry_id")), None)
    if row is None or not row["can_cancel"]:
        raise ValueError("Only queued or explicitly rejected next-turn Codex input, or prepared Claude input, can be cancelled")
    if row.get("provider") == "claude":
        from litetui import restored_claude

        return restored_claude.cancel(app, cmd, records[row["id"]])
    item, (entry, entries, index) = records[row["id"]]
    # Build persistence against the CURRENT metadata index; restored callbacks
    # may have captured an index before a conversation edit or open.
    try:
        _persist(app, entry, entries, index, "denied", admission={"reason": "Cancelled by user"})
    except Exception:
        # transition mutates before saving. Do not expose that memory as durable
        # evidence or remove the held item after an ambiguous storage failure.
        app._restored_input_error = True
        raise
    app._pending_input[:] = [candidate for candidate in app._pending_input if candidate is not item]
    return {"cancelled": row["id"], "session_id": result["session_id"]}


def resolve(app, cmd):
    """Close only the selected uncertain Claude delivery, without claiming its effects."""
    from litetui import restored_claude

    result, records = _inspect(app, cmd.get("session_id"))
    if cmd.get("snapshot") != result["snapshot"]:
        raise ValueError("Recovered input changed; refresh before resolving")
    row = next((row for row in result["entries"] if row["id"] == cmd.get("entry_id")), None)
    if row is None or row.get("provider") != "claude" or not row.get("can_resolve"):
        raise ValueError("Only an uncertain Claude delivery can be resolved without replay")
    return restored_claude.resolve(app, cmd, records[row["id"]])


def _persist(app, entry, entries, index, state, **values):
    """Verify both the write receipt and the saved value before any side effect."""
    from litetui.codex_steering import SteeringLedger, save_at

    current = _records(app.conversation).get(entry["id"])
    if current is None or current[0] is not entry or current[1] is not entries or current[2] != index:
        raise ValueError("Recovered input metadata moved before persistence")
    SteeringLedger(entries, lambda: save_at(app, index)).transition(entry, state, **values)
    meta, messages = ConversationRepository.read(app.store.convo_path)
    saved = _records(messages).get(entry["id"])
    if meta.get("id") != app.convo_id or saved is None or saved[0] != entry:
        raise OSError("Recovered input transition could not be verified in the saved journal")


async def continue_input(app, cmd):
    """One explicit attempt; original source/profile survive a new operation ID.

    Admission refusal has a structured, not-started result. Exceptions after
    the durable claim are uncertain and never release the item for replay.
    """
    from dataclasses import replace

    from litetui import hook_host, seat_authority, tool_policy

    operation = _text(cmd.get("id"), 128)
    result, records = _inspect(app, cmd.get("session_id"))
    if cmd.get("snapshot") != result["snapshot"]:
        raise ValueError("Recovered input changed; refresh before continuing")
    row = next((row for row in result["entries"] if row["id"] == cmd.get("entry_id")), None)
    if row is None or not row["can_continue"]:
        raise ValueError("Only the eligible oldest recovered Codex input can continue")
    item, (entry, entries, index) = records[row["id"]]
    app._validate_owned_execution()
    if getattr(app, "_mcp_maintenance", False) or getattr(app, "_gui_quitting", False):
        raise ValueError("Runtime maintenance or shutdown is active")
    conversation, backend, model = app.conversation, app.backend, app.model_id
    session, store = app._agent_session, app.store
    original_state = entry["state"]
    # Never relabel recovered unattended work as an attended RPC prompt.
    delivery = copy.deepcopy(entry["item"])
    delivery["source"] = hook_host._turn_source(delivery)
    requested_profile = delivery.get("tool_profile")
    if requested_profile is not None and requested_profile not in tool_policy.PROFILES:
        raise ValueError("Invalid saved tool profile")
    if delivery["source"] == "scheduled":
        refusal = seat_authority.withheld(app, requested_profile or tool_policy.STRICT)
        if refusal:
            return _receipt(app, operation, row["id"], "refused", reason=refusal)
    delivery.update(operation_id=operation, _codex_entry=entry, _codex_ledger=item["_codex_ledger"])
    reasons: list[str] = []

    def identity():
        if (app.conversation is not conversation or app.backend is not backend
                or app.model_id != model or app._agent_session is not session or app.store is not store
                or app.convo_id != result["session_id"]):
            raise ValueError("Conversation changed during recovered-input admission")
        _owned(app, result["session_id"])
        app._validate_owned_execution()

    try:
        _persist(app, entry, entries, index, "admitting", continuation_operation_id=operation,
                 continuation_outcome="admitting")
        claimed = copy.deepcopy(entry)

        def before_accept():
            identity()
            if (not app._pending_input or app._pending_input[0] is not item
                    or entry != claimed):
                raise ValueError("Recovered input changed during admission")
            _inspect(app, result["session_id"])

        before_accept()
        state = hook_host.snapshot(app)
        if original_state == "next_turn" or not state.hooks and not state.error:
            accepted = hook_host.accept_prompt_origin(app, delivery, on_refused=reasons.append)
            if accepted is not None and original_state == "next_turn":
                app._hook_turn_id = entry.get("admission", {}).get("turn_id", app._hook_turn_id)
                accepted = replace(accepted, hook_id=app._hook_turn_id)
        else:
            accepted = await hook_host.admit_prompt_origin(
                app, delivery, on_refused=reasons.append, before_accept=before_accept)
        identity()
        if accepted is None:
            before_accept()
            _persist(app, entry, entries, index, original_state, continuation_outcome="refused")
            return _receipt(app, operation, row["id"], "refused",
                            reason=reasons[-1] if reasons else "Prompt was not admitted")
        # Admission materializes the original input exactly once. Verify it was
        # durably appended before scheduling a provider-capable worker.
        meta, messages = ConversationRepository.read(store.convo_path)
        delivered = [m for m in messages if m.get("codex_delivery", {}).get("id") == row["id"]]
        if (meta.get("id") != app.convo_id or len(delivered) != 1
                or delivered[0].get("content") != delivery["content"]):
            raise OSError("Continued input was not durably materialized")
        if entry != claimed or not app._pending_input or app._pending_input[0] is not item:
            raise ValueError("Recovered input changed after admission")
        _persist(app, entry, entries, index, "admitted",
                 admission={**entry.get("admission", {}), "turn_id": accepted.hook_id},
                 continuation_outcome="admitted")
        app._pending_input.pop(0)
        worker = app._stream(origin=accepted)
        return _receipt(app, operation, row["id"], "accepted", worker=worker)
    except (Exception, asyncio.CancelledError):
        app._restored_input_error = True
        raise

def _receipt(app, operation, entry_id, outcome, *, worker=None, reason=None):
    app._restored_input_attempt = {
        "operation": operation, "entry": entry_id, "outcome": outcome, "worker": worker,
        "conversation": app.conversation, "session_id": app.convo_id,
        "backend": app.backend, "model": app.model_id, "store": app.store,
        "owner": app._agent_session,
    }
    result = {"turn": outcome, "entry_id": entry_id, "session_id": app.convo_id,
              "held": status(app)["held"]}
    if reason is not None:
        result["reason"] = reason
    return result


async def settled(app, cmd):
    """Read after the exact continuation worker retires, never just turn_end."""
    from textual.worker import WorkerCancelled

    from litetui.gui_rpc import _idle

    attempt = getattr(app, "_restored_input_attempt", None)
    if (attempt is None or cmd.get("operation_id") != attempt["operation"]
            or cmd.get("entry_id") != attempt["entry"] or cmd.get("session_id") != attempt["session_id"]):
        raise ValueError("Unknown continuation settlement request")

    def current():
        _owned(app, cmd["session_id"])
        if (app._restored_input_attempt is not attempt
                or app.conversation is not attempt["conversation"] or app.backend is not attempt["backend"]
                or app.model_id != attempt["model"] or app.store is not attempt["store"]
                or app._agent_session is not attempt["owner"]):
            raise ValueError("Continuation identity changed before queue readback")

    current()
    if attempt["outcome"] == "accepted":
        worker = attempt["worker"]
        if worker is None:
            raise ValueError("Continuation worker is unavailable")
        try:
            # Textual wait() catches cancellation and cancels its underlying
            # worker task. Shield it so cancellation of this read cannot stop
            # execution or masquerade as successful worker retirement.
            await asyncio.shield(worker.wait())
        except WorkerCancelled:
            # The caller's asyncio cancellation is NOT swallowed. A cancelled
            # Textual worker is retired; Rust separately requires turn settlement.
            pass
    current()
    _idle(app)
    result = inspect(app, cmd["session_id"])
    result.update(operation_id=attempt["operation"], entry_id=attempt["entry"],
                  continuation_outcome=attempt["outcome"])
    return result


class _HistoryReader:
    """Bound the existing reader and permit only observational native requests."""

    def __init__(self, server):
        self.server = server
        self.requests = 0
        self.bytes = 0

    async def request(self, method, params):
        if method not in ("thread/read", "thread/turns/list") or self.requests >= 100:
            raise ValueError("Recovered-input history read limit exceeded")
        self.requests += 1
        response = await self.server.request(method, params)
        self.bytes += len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if self.bytes > 8 * 1024 * 1024:
            raise ValueError("Recovered-input history byte limit exceeded")
        return response


async def reconcile(app, cmd):
    """Observe positive provider acceptance; never send, retry or start a turn."""
    from litetui import codex_history, codex_steering

    operation = _text(cmd.get("id"), 128)
    before, records = _inspect(app, cmd.get("session_id"))
    if cmd.get("snapshot") != before["snapshot"]:
        raise ValueError("Recovered input changed; refresh before checking history")
    row = next((row for row in before["entries"] if row["id"] == cmd.get("entry_id")), None)
    if row is None or not row["can_reconcile"]:
        raise ValueError("This recovered input is not eligible for provider-history reconciliation")
    if getattr(app, "_mcp_maintenance", False) or getattr(app, "_gui_quitting", False):
        raise ValueError("Runtime maintenance or shutdown is active")
    item, (entry, entries, index) = records[row["id"]]
    conversation, backend, model = app.conversation, app.backend, app.model_id
    owner, store, server = app._agent_session, app.store, app.backend.app_server

    def current():
        from litetui.gui_rpc import _idle

        _idle(app)
        app._validate_owned_execution()
        if (app.conversation is not conversation or app.backend is not backend
                or app.model_id != model or app._agent_session is not owner or app.store is not store
                or app.backend.app_server is not server):
            raise ValueError("Conversation changed during recovered-input history read")
        fresh, fresh_records = _inspect(app, before["session_id"])
        if (fresh["snapshot"] != before["snapshot"]
                or fresh_records.get(row["id"], (None,))[0] is not item):
            raise ValueError("Recovered input changed during history read")

    current()
    await server.start()
    current()
    history = await codex_history.read(_HistoryReader(server), entry["threadId"])
    current()
    result = {"session_id": before["session_id"], "entry_id": row["id"], "reconciled": False}
    if not codex_steering.accepted_in_history(entry, history):
        return result
    # This records an already accepted input, not a new admission. Do not run
    # prompt hooks, change active authority, emit turn events or schedule work.
    try:
        _persist(app, entry, entries, index, "accepted", reconciliation_operation_id=operation)
        message = {
            "role": "user", "content": copy.deepcopy(entry["item"]["content"]),
            "codex_delivery": {"id": entry["id"], "threadId": entry["threadId"], "state": "accepted"},
            "provider_metadata": {"provider": "codex", "app_server_thread_id": entry["threadId"],
                                  "instructions_digest": entry.get("instructions_digest"),
                                  "reconciled_turn_id": entry["turnId"]},
        }
        app._append(message)
        _owned(app, before["session_id"])
        meta, messages = ConversationRepository.read(store.convo_path)
        delivered = [m for m in messages if m.get("codex_delivery", {}).get("id") == entry["id"]]
        if (meta.get("id") != before["session_id"] or len(delivered) != 1
                or any(delivered[0].get(key) != value for key, value in message.items())):
            raise OSError("Reconciled input was not durably materialized")
    except (Exception, asyncio.CancelledError):
        app._restored_input_error = True
        raise
    codex_steering.remove_item(app._pending_input, item)
    result["reconciled"] = True
    return result
