"""Reviewed exact-text edits to the current conversation's system prompt.

The prompt is the conversation's first system row. The legacy ``/system`` handler
and ``gui.system.set`` mutate that row in memory before persistence is checked
and cannot review, bound or read anything back. This module persists first (one
canonical edit or snapshot record), changes the live row only when the append
reported no failure, and a separate readback replays the transcript from disk.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

from litetui import appsvc, gui_rpc, native_context, native_history
from litetui import background_input as work
from litetui.conversation import ConversationRepository

MAX_PROMPT = native_context.MAX_DOCUMENT
DOCUMENT = "system"
PREFIX = "gui.context.native_system_"
EXTRA = {"edit": ("revision",), "review": ("revision", "text"), "apply": ("token",),
         "readback": ("token", "apply_id")}


@dataclass
class Reviewed:
    instance: str
    token: str
    before: bytes | None
    after: bytes
    injected: bool
    apply_id: str | None = None


def _revision(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def _row(app):
    """The first system row, the same selection as gui.system.get and the F1 reader."""
    for index, message in enumerate(app.conversation):
        if message.get("role") == "system":
            if index:
                raise ValueError("The system prompt is not the first message; it cannot be edited here")
            return message
    return None


def _data(app):
    row = _row(app)
    if row is None:
        return None
    value = row.get("content")
    if not isinstance(value, str):
        raise ValueError("Current system content must be plain text")  # noqa: TRY004 - invalid source evidence
    if len(value) > MAX_PROMPT:
        raise ValueError("The system prompt exceeds the editing limit")
    data = value.encode("utf-8")
    if len(data) > MAX_PROMPT:
        raise ValueError("The system prompt exceeds the editing limit")
    return data


def _expect(app, revision):
    if revision is not None and (type(revision) is not str or not re.fullmatch(r"[0-9a-f]{64}", revision)):
        raise ValueError("Invalid system prompt revision")
    data = _data(app)
    if _revision(data) != revision:
        raise ValueError("The system prompt changed since it was read")
    return data


def _effect(app):
    """When the saved text reaches the model, per the backend's real consumer."""
    backend = app.backend
    again = (" The host adds its memory store snapshot after your text on that request unless the text already "
             "contains it.")
    if getattr(backend, "name", None) == "claude":
        return ("Saved to this LiteTUI conversation only. Claude keeps the system prompt its session was opened "
                "with, so this text is never sent to Claude; a new Claude session builds its prompt from the host "
                "sections, not from this edit.")
    if getattr(backend, "name", None) == "codex" and hasattr(backend, "app_server"):
        return ("Saved now and sent from your next prompt: Codex receives the changed instructions as additional "
                "context on its existing thread. Earlier turns in that thread are not rewritten." + again)
    return "Saved now and sent with your next request in this conversation." + again


def _disk(app):
    """Replay the transcript through the canonical reader: (row 0 or None, meta)."""
    captured = native_history.capture_file(app.convo_path)
    if not captured.writable or captured.meta.get("id") != app.convo_id:
        raise ValueError("The conversation transcript is not the writable file of this conversation")
    messages = captured.messages
    return (messages[0] if messages and messages[0].get("role") == "system" else None), captured.meta, len(captured.raw)


def _stored(app, expected=None):
    """saved: transcript row equals the live row; session_only: no file yet; diverged otherwise."""
    if app.store.pending:
        return "session_only"
    row, meta, _size = _disk(app)
    live = _row(app)
    if live is None:
        return "saved" if row is None else "diverged"
    if row is None or row.get("content") != live.get("content") or row.get("role") != "system":
        return "diverged"
    if expected is not None and meta.get("store_injected") is not expected:
        return "diverged"
    return "saved"


def _reply(binding, kind, **fields):
    result = {"kind": kind, "version": 1, **binding, "document": DOCUMENT, **fields}
    if len(work.encoded(result)) > work.LIMITS["reply_bytes"] - 512:
        raise ValueError("System prompt edit exceeds the transport limit")
    return result


def _validate(text):
    if type(text) is not str or len(text) > MAX_PROMPT:
        raise ValueError("System prompt draft exceeds the UTF-8 limit")
    data = text.encode("utf-8")
    if len(data) > MAX_PROMPT:
        raise ValueError("System prompt draft exceeds the UTF-8 limit")
    if not text.strip():
        raise ValueError("System prompt must be nonempty text")
    if "\0" in text:
        raise ValueError("System prompt cannot contain NUL")
    return data


def validate_argument(text):
    """The /system <text> prefill: same bounds as a draft, checked at route time."""
    if text:
        _validate(text)


def _commit(app, bridge, pending):
    bridge.check()
    app._validate_owned_identity()
    gui_rpc._idle(app)
    store = app.store
    if store.loading or store.persist_error:
        raise ValueError("Conversation persistence is unavailable; the system prompt was not changed")
    if _data(app) != pending.before:
        raise ValueError("The system prompt changed since review")
    row = _row(app)
    text = pending.after.decode("utf-8")
    message = {**row, "content": text} if row is not None else {"role": "system", "content": text}
    identity = getattr(app, "_prompt_store_identity", None)
    if not store.pending:
        if row is None:
            record = store.snapshot_record([message, *app.conversation], "system prompt inserted",
                                           store_identity=identity, store_injected=pending.injected)
        else:
            record = store.edit_record(0, message, "system prompt changed",
                                       store_identity=identity, store_injected=pending.injected)
        _row_before, _meta, size = _disk(app)
        payload = ConversationRepository.record_bytes(record, separator=True)
        if size + len(payload) > native_history.MAX_TRANSCRIPT:
            raise ValueError("The system prompt record exceeds the 8 MiB transcript budget")
        store.write_record(record)
        if store.persist_error:
            raise OSError("Saving the conversation failed; the live system prompt was not changed. "
                          "Reopen the conversation if its transcript looks different.")
    if row is None:
        app.conversation.insert(0, message)
    else:
        app.conversation[0] = message
    # The replacement carries only the text the user reviewed. Without a store
    # snapshot inside it the host must inject one again on the next request.
    app._store_injected = pending.injected


def dispatch(app, cmd):
    operation = cmd["type"]
    kind = operation.removeprefix(PREFIX)
    bridge, binding = native_context.read_binding(app, cmd, operation, "document", *EXTRA[kind])
    if cmd["document"] != DOCUMENT:
        raise ValueError("Only the conversation system prompt can be edited here")
    if kind in ("edit", "review"):
        gui_rpc._idle(app)
        app._native_system_review = None
        before = _expect(app, cmd["revision"])
        effect = _effect(app)
        if kind == "edit":
            return _reply(binding, "context_edit", revision=cmd["revision"],
                          text=(before or b"").decode("utf-8"), effect=effect, stored=_stored(app))
        after = _validate(cmd["text"])
        if after == before:
            raise ValueError("System prompt draft is unchanged")
        token = secrets.token_hex(32)
        review = _reply(binding, "context_review", token=token, before_revision=cmd["revision"],
                        before_bytes=None if before is None else len(before), after_revision=_revision(after),
                        after_bytes=len(after), effect=effect)
        injected = ("\n\n" + appsvc.STORE_HEADER) in cmd["text"]
        app._native_system_review = Reviewed(bridge.instance, token, before, after, injected)
        return review

    pending = getattr(app, "_native_system_review", None)
    if (not isinstance(pending, Reviewed) or pending.instance != bridge.instance
            or type(cmd["token"]) is not str or cmd["token"] != pending.token):
        raise ValueError("Review the current system prompt draft again")
    if kind == "apply":
        gui_rpc._idle(app)
        if pending.apply_id is None:
            # A consumed capability never retries on an uncertain publication.
            app._native_system_review = None
            _commit(app, bridge, pending)
            pending.apply_id = cmd["id"]
            app._native_system_review = pending
        elif pending.apply_id != cmd["id"]:
            raise ValueError("System prompt review was already applied")
        stored = _stored(app, pending.injected)
        if stored == "diverged":
            raise ValueError("The conversation transcript does not hold the applied system prompt")
        return _reply(binding, "context_apply", token=pending.token, apply_id=pending.apply_id,
                      revision=_revision(pending.after), bytes=len(pending.after), stored=stored)
    if pending.apply_id is None or cmd["apply_id"] != pending.apply_id:
        raise ValueError("System prompt readback belongs to another application")
    if _data(app) != pending.after:
        raise ValueError("The system prompt changed after apply; saved content could not be verified")
    stored = _stored(app, pending.injected)
    if stored == "diverged":
        raise ValueError("The conversation transcript does not hold the applied system prompt")
    return _reply(binding, "context_saved", token=pending.token, apply_id=pending.apply_id,
                  revision=_revision(pending.after), bytes=len(pending.after), stored=stored, effect=_effect(app))
