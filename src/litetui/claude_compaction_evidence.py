"""Bind reviewed Claude maintenance to the live SDK owner and its durable segment."""
from __future__ import annotations

import copy
import hashlib
import math
import uuid
from dataclasses import dataclass

from litetui import conversation_action as action
from litetui import history_projection as projection
from litetui import restored_claude
from litetui.backend_session import SessionState
from litetui.claude_persistence import path_for
from litetui.claude_session import ClaudeSession
from litetui.claude_turn import effort_for, launch_workspace


def _digest(value):
    return hashlib.sha256(projection.encoded(value)).hexdigest()


def _native_id(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _document(app, ledger):
    current = restored_claude._read(app.store).document
    if current != ledger.document:
        raise ValueError("Claude's live and durable ledgers disagree")
    return current


@dataclass(frozen=True)
class SegmentReceipt:
    old_segment: str
    native_session: str
    result_id: str
    new_segment: str
    seed_sha256: str
    ledger_before: str
    ledger_after: str


@dataclass(frozen=True, eq=False)
class Binding:
    ledger: object
    session: ClaudeSession
    owner: object
    generation: int
    segment: str
    native_session: str
    workspace: str
    before: dict

    @classmethod
    def capture(cls, app):
        backend = app.backend
        if backend.name != "claude" or not getattr(backend, "owns_native_turns", False):
            raise ValueError("This backend does not own a Claude session")
        if app.settings.wake_after_compact:
            raise ValueError("Turn off Wake after compacting before reviewing a Claude compaction")
        if getattr(app, "_claude_compact_resume", None) is not None:
            raise ValueError("Finish the pending Claude input or continuation before compacting")
        ledger = getattr(app, "_claude_ledger", None)
        session = backend.session
        if (ledger is None or getattr(app, "_claude_ledger_path", None) != app.convo_dir
                or ledger.dir != app.convo_dir or ledger.file != path_for(app.convo_dir)
                or not isinstance(session, ClaudeSession) or session._owner is None or session._owner.done()):
            raise ValueError("Submit a turn on this Claude conversation to establish its live SDK session")
        if (app.tools_enabled or session.options.tools != [] or session.options.mcp_servers != {}
                or not session.options.strict_mcp_config):
            raise ValueError("Claude F6 compaction requires a session opened with tools off")
        before = _document(app, ledger)
        selected = ledger.selected
        if (not selected or selected["id"] != backend.segment_id or selected["workspace"] != launch_workspace(app)
                or str(session.options.cwd) != selected["workspace"]
                or not _native_id(selected.get("session_id")) or selected["session_id"] != session.session_id
                or session.lifecycle.generation < 1 or session.lifecycle.state != SessionState.READY
                or session.lifecycle.active_turn is not None or session.lifecycle.failure or session.lifecycle.cleanup_errors
                or session.effort != effort_for(app) or not session._events.empty() or not session._commands.empty()
                or any(ledger.pending(key) for key in before["segments"])):
            raise ValueError("Claude compaction requires the current idle SDK session and its saved segment")
        return cls(ledger, session, session._owner, session.lifecycle.generation, selected["id"],
                   session.session_id, selected["workspace"], before)

    def plan(self, count):
        from litetui.claude_compact import request
        return {"kind": "claude", "messages": count, "segment": self.segment, "native_session": self.native_session,
                "ledger_before": _digest(self.before), "prompt_sha256": hashlib.sha256(request().encode()).hexdigest()}

    def check(self, app):
        other = Binding.capture(app)
        if (self.ledger is not other.ledger or self.session is not other.session or self.owner is not other.owner
                or self.generation != other.generation or self.before != other.before):
            raise ValueError("The reviewed Claude session, lifecycle or segment changed")

    def retired(self, app):
        state = self.session.lifecycle
        if (app.backend.session is not None or app.backend.segment_id is not None or not self.owner.done()
                or self.session._owner is not self.owner or state.generation != self.generation
                or self.session.session_id != self.native_session or state.state != SessionState.DISCONNECTED
                or state.failure or state.cleanup_errors or state.active_turn is not None
                or getattr(app, "_claude_ledger", None) is not self.ledger or getattr(app, "_claude_closing", None)):
            raise ValueError("Claude's reviewed SDK session did not retire cleanly")

    def verify_final(self, app, receipt):
        self.retired(app)
        doc = _document(app, self.ledger)
        selected = self.ledger.selected
        if (selected is None or selected["id"] != receipt.new_segment or _digest(doc) != receipt.ledger_after
                or selected["session_id"] is not None or selected["entries"]
                or hashlib.sha256(selected["seed"].encode()).hexdigest() != receipt.seed_sha256):
            raise ValueError("Claude's selected seed or saved ledger changed before settlement")


class Progress:
    def __init__(self, binding):
        self.binding = binding
        self.result_id = None
        self.summary = None
        self.before_selection = None
        self.stage = "SDK summary request"

    def terminal(self, raw, event):
        session = self.binding.session
        if (self.result_id is not None or type(raw).__name__ != "ResultMessage"
                or getattr(raw, "session_id", None) != self.binding.native_session
                or event.session_id != self.binding.native_session or session.session_id != event.session_id
                or not _native_id(event.uuid) or event.uuid != getattr(raw, "uuid", None) or event.is_error
                or getattr(raw, "is_error", None) is not False
                or getattr(raw, "subtype", None) != "success" or getattr(raw, "terminal_reason", None) not in (None, "completed")
                or not isinstance(event.text, str) or event.text != getattr(raw, "result", None) or not event.text.strip()):
            raise ValueError("Claude did not return a successful identified summary in the reviewed session")
        self.result_id = event.uuid
        self.summary = event.text.strip()
        if len(self.summary.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Claude summary exceeds the 1 MiB seed limit")

    def before_close(self, app, summary):
        action._stable(app, action._required_attempt(app).review, original=True, admitted=True)
        binding = self.binding
        if (not self.result_id or self.summary != summary or app.backend.session is not binding.session
                or app.backend.segment_id != binding.segment or binding.session._owner is not binding.owner
                or binding.owner.done() or binding.session.lifecycle.active_turn is not None
                or binding.session.lifecycle.generation != binding.generation or not binding.session._events.empty()):
            raise ValueError("Claude's summary or session changed before retirement")
        doc = _document(app, binding.ledger)
        expected = copy.deepcopy(binding.before)
        # The canonical summary loop may persist fresh cache usage on this segment.
        for key in ("cache_used_at", "cache_model"):
            segment = doc["segments"][binding.segment]
            if key in segment:
                expected["segments"][binding.segment][key] = segment[key]
        if doc != expected:
            raise ValueError("Claude ledger changed during the summary turn")
        self.before_selection = doc
        self.stage = "SDK session retirement"

    def before_select(self, app):
        action._stable(app, action._required_attempt(app).review, original=True, admitted=True)
        self.binding.retired(app)
        if _document(app, self.binding.ledger) != self.before_selection:
            raise ValueError("Claude ledger changed during session retirement")
        self.stage = "replacement segment selection"

    def selected(self, app, new):
        binding = self.binding
        doc = _document(app, binding.ledger)
        ident = new["id"]
        if (not isinstance(ident, str) or len(ident) != 32 or uuid.UUID(ident).hex != ident
                or ident in self.before_selection["segments"] or set(new) != {"id","session_id","workspace","created_at","entries","seed"}
                or new["session_id"] is not None or new["entries"] or new["seed"] != self.summary
                or new["workspace"] != binding.workspace or not math.isfinite(new["created_at"])):
            raise ValueError("Claude replacement segment does not match the reviewed seed")
        expected = copy.deepcopy(self.before_selection)
        expected["selected_segment"] = ident
        expected["segments"][ident] = new
        if doc != expected:
            raise ValueError("Claude replacement segment failed independent ledger readback")
        self.stage = "transcript publication after verified segment selection"
        return SegmentReceipt(binding.segment, binding.native_session, self.result_id, ident,
                              hashlib.sha256(self.summary.encode()).hexdigest(), _digest(binding.before), _digest(doc))
