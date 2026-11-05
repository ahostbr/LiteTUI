"""Claude input ownership and shared exact native mid-turn publication tickets."""
from __future__ import annotations

import asyncio
import copy
import re
import uuid
from dataclasses import dataclass

from litetui import background_input as work
from litetui.backend_session import SessionState
from litetui.claude_persistence import path_for
from litetui.claude_session import ClaudeSession
from litetui.claude_turn import launch_workspace, prepare_input


def _document(app, ledger):
    # Loading ClaudeLedger deliberately converts in-flight deliveries to uncertain.
    # This live-owner read compares the actual stored bytes without recovery.
    if ledger.dir != app.convo_dir or ledger.file != path_for(app.convo_dir):
        raise ValueError("Claude queue ledger belongs to another conversation")
    raw = work._read(ledger.file, 8 * 1024 * 1024)
    if raw is None:
        raise ValueError("Claude queue ledger is missing")
    document = work.strict_json(raw)
    if document != ledger.document:
        raise ValueError("Claude queue ledger changed outside its live owner")
    return document


@dataclass(frozen=True, eq=False)
class Binding:
    session: ClaudeSession
    owner: object
    ledger: object
    generation: int
    native_session: str
    segment: str
    active_input: str

    @classmethod
    def capture(cls, app):
        backend = app.backend
        session = getattr(backend, "session", None)
        ledger = getattr(app, "_claude_ledger", None)
        active = getattr(app, "_claude_active_input", None) or {}
        if (backend.name != "claude" or not getattr(backend, "owns_native_turns", False)
                or not isinstance(session, ClaudeSession) or session._owner is None or session._owner.done()
                or ledger is None or getattr(app, "_claude_ledger_path", None) != app.convo_dir):
            raise ValueError("Claude queue requires its current captured SDK session")
        document = _document(app, ledger)
        selected = ledger.selected
        native = session.session_id
        if (not isinstance(native, str) or not re.fullmatch(r"[0-9a-f-]{36}", native)
                or str(uuid.UUID(native)) != native or uuid.UUID(native).version != 4 or selected is None
                or not re.fullmatch(r"[0-9a-f]{32}", selected["id"])
                or selected["id"] != backend.segment_id or selected["session_id"] != native
                or selected["workspace"] != launch_workspace(app) or str(session.options.cwd) != selected["workspace"]
                or active.get("_claude_segment") != selected["id"] or active.get("_claude_conversation") != app.convo_id
                or session.lifecycle.state != SessionState.READY or not 1 <= session.lifecycle.generation <= 2**53 - 1
                or session.lifecycle.failure or session.lifecycle.cleanup_errors or session._terminal.is_set()):
            raise ValueError("The captured Claude session is no longer accepting next-turn input")
        entry_id = active.get("_claude_entry", {}).get("id")
        entries = [e for e in selected["entries"] if e["id"] == entry_id]
        if (not isinstance(entry_id, str) or not re.fullmatch(r"[0-9a-f]{32}", entry_id)
                or session.lifecycle.active_turn != entry_id or len(entries) != 1
                or entries[0]["state"] not in {"submitted", "acknowledged"}
                or any(e["state"] not in {"prepared", "terminal"} and e["id"] != entry_id
                       for segment in document["segments"].values() for e in segment["entries"])):
            raise ValueError("Claude queue requires the exact active delivery; resolve uncertain input first")
        return cls(session, session._owner, ledger, session.lifecycle.generation, native, selected["id"], entry_id)

    def value(self):
        return {"provider": "claude", "generation": self.generation, "native_session": self.native_session,
                "segment": self.segment, "active_input": self.active_input}

    def check(self, app):
        current = Binding.capture(app)
        if (current.session is not self.session or current.owner is not self.owner or current.ledger is not self.ledger
                or current.value() != self.value()):
            raise ValueError("The reviewed Claude queue owner changed")

    def prepare(self, app, payload, review):
        self.check(app)
        before = _document(app, self.ledger)
        if any(e.get("operation_id") == review for s in before["segments"].values() for e in s["entries"]):
            raise ValueError("This Claude queue review was already prepared; inspect retained input")
        metadata = prepare_input(app, payload["content"], payload["tool_profile"], payload["source"], review)
        native = metadata["_claude_entry"]
        expected = copy.deepcopy(before)
        expected["segments"][self.segment]["entries"].append(native)
        if (metadata["_claude_segment"] != self.segment or metadata["_claude_conversation"] != app.convo_id
                or not re.fullmatch(r"[0-9a-f]{32}", native["id"]) or native["state"] != "prepared"
                or native["segment_id"] != self.segment or native["content"] != payload["content"]
                or native["profile"] != payload["tool_profile"] or native["source"] != payload["source"]
                or native["operation_id"] != review or _document(app, self.ledger) != expected):
            raise ValueError("Claude prepared input failed exact durable readback")
        self.check(app)
        return metadata

    def receipt(self, app, item, review):
        # The original turn can finish while its exact response is awaiting emission.
        # Queue confirmation still requires the same idle-or-active SDK owner.
        session = self.session
        if (app.backend.session is not session or session._owner is not self.owner or self.owner.done()
                or session.lifecycle.generation != self.generation or session.session_id != self.native_session
                or session.lifecycle.state != SessionState.READY or session.lifecycle.failure or session.lifecycle.cleanup_errors
                or app.backend.segment_id != self.segment or getattr(app, "_claude_ledger", None) is not self.ledger
                or self.ledger.selected["id"] != self.segment):
            raise ValueError("Claude queued input lost its original SDK owner")
        native = item["_claude_entry"]
        entries = _document(app, self.ledger)["segments"][self.segment]["entries"]
        if [e for e in entries if e["id"] == native["id"]] != [native] or native["state"] != "prepared":
            raise ValueError("Claude queued delivery changed before confirmation")
        return {"id": native["id"], "segment_id": self.segment, "state": "prepared", "operation_id": review}



@dataclass(eq=False)
class Publication:
    app: object
    bridge: object
    binding: object
    entry: object
    task: object
    command: bytes
    result: dict
    raw: bytes
    prepared: bool = False
    used: bool = False


def blocking(app):
    return isinstance(getattr(app, "_native_claude_queue_publication", None), Publication)


def stage_publication(app, cmd, result, binding, entry):
    if blocking(app):
        raise ValueError("Another native queue confirmation is still pending")
    ticket = Publication(app, work.bridge(app), binding, entry, asyncio.current_task(),
                         work.encoded(cmd), result, work.encoded(result))
    app._native_claude_queue_publication = ticket


def _check(app, ticket):
    if (ticket.app is not app or ticket.task is not asyncio.current_task() or ticket.used
            or getattr(app, "_native_claude_queue_publication", None) is not ticket
            or work.bridge(app) is not ticket.bridge or work.encoded(ticket.result) != ticket.raw
            or ticket.bridge.inputs.get(ticket.entry.ref["input_id"]) is not ticket.entry
            or ticket.entry.state != "queued" or ticket.entry.admission is not None
            or not any(item is ticket.entry.item for item in app._pending_input)):
        raise ValueError("Native queue publication lost its exact command, result or task")
    ticket.bridge.check()
    if work._read(ticket.bridge.path) != ticket.bridge.raw:
        raise ValueError("Native queued Work journal changed before publication")
    native = ticket.binding.receipt(app, ticket.entry.item, ticket.result["review"])
    if native != ticket.result["native_delivery"]:
        raise ValueError("Native prepared queue receipt changed before publication")


def prepare_publication(app, cmd, result):
    if cmd.get("type") not in {"gui.prompt.native_queue_apply", "gui.prompt.native_send_apply"} or cmd.get("backend") not in {"claude", "codex"}:
        return None
    ticket = getattr(app, "_native_claude_queue_publication", None)
    if (not isinstance(ticket, Publication) or ticket.command != work.encoded(cmd)
            or ticket.result is not result or ticket.prepared):
        raise ValueError("Native queue confirmation is foreign or consumed")
    _check(app, ticket)
    ticket.prepared = True
    return ticket


def finish_publication(app, ticket):
    _check(app, ticket)
    if not ticket.prepared:
        raise ValueError("Native queue confirmation was not prepared")
    ticket.used = True
    app._native_claude_queue_publication = None
    from litetui import native_runtime_controls
    native_runtime_controls.signal_stop(app)
    if app._pending_input and not ticket.bridge.closing and not getattr(app, "_gui_quitting", False):
        app.call_later(app._flush_pending_input)
