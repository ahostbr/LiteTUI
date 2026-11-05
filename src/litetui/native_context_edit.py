"""Reviewed exact-byte edits to the current leased agent's fixed memory documents."""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

from litetui import background_input as work
from litetui import gui_rpc, native_context
from litetui.agent_store import MEMORY_FILES


@dataclass
class Reviewed:
    instance: str
    document: str
    token: str
    before: bytes | None
    after: bytes
    apply_id: str | None = None


def _revision(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def _data(app, document):
    status, raw = native_context._memory(app, document)
    if status == "oversize":
        raise ValueError("This memory document exceeds the editing limit")
    if raw is not None:
        raw.decode("utf-8")
    return raw


def _expect(app, document, revision):
    if revision is not None and (type(revision) is not str or not re.fullmatch(r"[0-9a-f]{64}", revision)):
        raise ValueError("Invalid memory revision")
    raw = _data(app, document)
    if _revision(raw) != revision:
        raise ValueError("Memory document changed since it was read")
    return raw


def _reply(binding, document, kind, **fields):
    result = {"kind": kind, "version": 1, **binding, "document": document, **fields}
    if len(work.encoded(result)) > work.LIMITS["reply_bytes"] - 512:
        raise ValueError("Memory edit exceeds the transport limit")
    return result


def dispatch(app, cmd):
    operation = cmd["type"]
    fields = {"gui.context.native_edit": ("revision",), "gui.context.native_review": ("revision", "text"),
              "gui.context.native_apply": ("token",), "gui.context.native_readback": ("token", "apply_id")}
    topic = operation.startswith("gui.context.native_topic_")
    kind = operation.replace("native_topic_", "native_") if topic else operation
    bridge, binding = native_context.read_binding(app, cmd, operation, "document", *fields[kind])
    document = cmd["document"]
    if topic:
        from litetui import native_context_topics as topics
        topics.checked_path(app, document)
    elif type(document) is not str or document not in MEMORY_FILES:
        raise ValueError("Only the current agent's memory, soul and handoff documents can be edited")
    if getattr(app, "_owner_memory_dir", None) is not None:
        raise ValueError("Owner memory overrides cannot be edited here")
    if kind in ("gui.context.native_edit", "gui.context.native_review"):
        gui_rpc._idle(app)
        app._native_context_review = None
        before = _expect(app, document, cmd["revision"])
        if kind == "gui.context.native_edit":
            return _reply(binding, document, "context_edit", revision=cmd["revision"], text=(before or b"").decode("utf-8"))
        text = cmd["text"]
        if type(text) is not str or len(text) > native_context.MAX_DOCUMENT:
            raise ValueError("Memory draft exceeds the UTF-8 document limit")
        after = text.encode("utf-8")
        if len(after) > native_context.MAX_DOCUMENT:
            raise ValueError("Memory draft exceeds the UTF-8 document limit")
        if after == before:
            raise ValueError("Memory draft is unchanged")
        token = secrets.token_hex(32)
        review = _reply(binding, document, "context_review", token=token, before_revision=cmd["revision"],
                        before_bytes=None if before is None else len(before), after_revision=_revision(after),
                        after_bytes=len(after))
        app._native_context_review = Reviewed(bridge.instance, document, token, before, after)
        return review

    pending = getattr(app, "_native_context_review", None)
    if (not isinstance(pending, Reviewed) or pending.instance != bridge.instance or pending.document != document
            or type(cmd["token"]) is not str or cmd["token"] != pending.token):
        raise ValueError("Review the current memory draft again")
    if kind == "gui.context.native_apply":
        gui_rpc._idle(app)
        if pending.apply_id is None:
            # A consumed capability never retries on an uncertain publication.
            app._native_context_review = None

            def guard(temporary):
                bridge.check()
                app._validate_owned_identity()
                gui_rpc._idle(app)
                if native_context._regular_file(temporary) != ("content", pending.after):
                    raise ValueError("Staged memory draft changed before publication")
                if _data(app, document) != pending.before:
                    raise ValueError("Memory document changed before publication")

            if _data(app, document) != pending.before:
                raise ValueError("Memory document changed since review")
            gui_rpc._owned_memory(app, app._agent_session, "write", {
                "session_id": binding["session_id"], "name": document, "text": pending.after.decode("utf-8"),
            }, before_publish=guard, create_only=pending.before is None)
            pending.apply_id = cmd["id"]
            app._native_context_review = pending
        elif pending.apply_id != cmd["id"]:
            raise ValueError("Memory review was already applied")
        kind = "context_apply"
    else:
        if pending.apply_id is None or cmd["apply_id"] != pending.apply_id:
            raise ValueError("Memory readback belongs to another application")
        if _data(app, document) != pending.after:
            raise ValueError("Memory changed after apply; saved content could not be verified")
        kind = "context_saved"
    return _reply(binding, document, kind, token=pending.token, apply_id=pending.apply_id,
                  revision=_revision(pending.after), bytes=len(pending.after))
