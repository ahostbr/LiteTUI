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
from litetui.claude_tools import NATIVE_TOOLS
from litetui.claude_turn import effort_for, launch_workspace

#: A reviewed summary may use the native tools, but its receipt must stay bounded and honest:
#: past either cap the compaction fails before the session is closed. A list is never truncated.
MAX_TOOL_CALLS = 64
MAX_STORE_FILES = 32
MAX_PATH_CHARS = 4096
WRITE_TOOLS = ("write", "edit")


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
    tool_calls: int = 0
    store_files: tuple[str, ...] = ()


def _tool_options(app, options, *, rewrite):
    """The tool facts a review binds: what the session offers and whether LiteTUI lets it run."""
    tools, servers = getattr(options, "tools", None), getattr(options, "mcp_servers", None)
    if (not isinstance(tools, list) or not all(isinstance(name, str) for name in tools)
            or not isinstance(servers, dict) or getattr(options, "strict_mcp_config", None) is not True):
        raise ValueError("Claude F6 history actions need a session with a declared, strict tool inventory")
    if rewrite or not app.tools_enabled:
        # Edit/Retry (rewrite) forks keep their tools-off rule; a compaction with tools off must
        # not sit on a session that still offers tools the review never disclosed.
        if app.tools_enabled or tools or servers:
            raise ValueError("Claude F6 history actions require a session opened with tools off")
    elif not set(tools) <= set(NATIVE_TOOLS) or set(servers) - {"litetui"}:
        raise ValueError("Claude's session offers tools LiteTUI cannot attribute to a reviewed compaction")
    return {"tools_enabled": bool(app.tools_enabled), "tools": sorted(tools), "mcp": sorted(servers),
            "strict_mcp_config": True, "permission_mode": getattr(options, "permission_mode", None)}


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
    tools: dict
    wake: bool

    @property
    def tool_work(self):
        """Whether the reviewed summary may legitimately make tool calls."""
        return bool(self.tools["tools_enabled"] and (self.tools["tools"] or self.tools["mcp"]))

    @classmethod
    def capture(cls, app, *, rewrite=False):
        backend = app.backend
        if backend.name != "claude" or not getattr(backend, "owns_native_turns", False):
            raise ValueError("This backend does not own a Claude session")
        if getattr(app, "_claude_compact_resume", None) is not None:
            raise ValueError("Finish the pending Claude input or continuation before using this history action")
        ledger = getattr(app, "_claude_ledger", None)
        session = backend.session
        if (ledger is None or getattr(app, "_claude_ledger_path", None) != app.convo_dir
                or ledger.dir != app.convo_dir or ledger.file != path_for(app.convo_dir)
                or not isinstance(session, ClaudeSession) or session._owner is None or session._owner.done()):
            raise ValueError("Submit a turn on this Claude conversation to establish its live SDK session")
        tools = _tool_options(app, session.options, rewrite=rewrite)
        before = _document(app, ledger)
        selected = ledger.selected
        if (not selected or selected["id"] != backend.segment_id or selected["workspace"] != launch_workspace(app)
                or str(session.options.cwd) != selected["workspace"]
                or not _native_id(selected.get("session_id")) or selected["session_id"] != session.session_id
                or session.lifecycle.generation < 1 or session.lifecycle.state != SessionState.READY
                or session.lifecycle.active_turn is not None or session.lifecycle.failure or session.lifecycle.cleanup_errors
                or session.effort != effort_for(app) or not session._events.empty() or not session._commands.empty()
                or any(ledger.pending(key) for key in before["segments"])):
            raise ValueError("Claude history actions require the current idle SDK session and its saved segment")
        return cls(ledger, session, session._owner, session.lifecycle.generation, selected["id"],
                   session.session_id, selected["workspace"], before, tools, bool(app.settings.wake_after_compact))

    def plan(self, count):
        from litetui.claude_compact import request
        return {"kind": "claude", "messages": count, "segment": self.segment, "native_session": self.native_session,
                "ledger_before": _digest(self.before), "prompt_sha256": hashlib.sha256(request().encode()).hexdigest(),
                "tools_sha256": _digest(self.tools)}

    def check(self, app, *, rewrite=False):
        other = Binding.capture(app, rewrite=rewrite)
        if (self.ledger is not other.ledger or self.session is not other.session or self.owner is not other.owner
                or self.generation != other.generation or self.before != other.before
                or self.tools != other.tools or self.wake != other.wake):
            raise ValueError("The reviewed Claude session, lifecycle, tools or segment changed")

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

    def verify_woken(self, app, receipt):
        """Settlement after a reviewed wake: the strict `verify_final` already ran on the root,
        before the wake could submit. Here the replacement segment may have gained its session and
        the wake's delivery entry, but it must still be the one the receipt named, with the same seed,
        and the old session must still be the retired one."""
        state = self.session.lifecycle
        if (app.backend.session is self.session or not self.owner.done() or self.session._owner is not self.owner
                or state.generation != self.generation or self.session.session_id != self.native_session
                or state.state != SessionState.DISCONNECTED or state.failure or state.cleanup_errors
                or state.active_turn is not None or getattr(app, "_claude_ledger", None) is not self.ledger):
            raise ValueError("Claude's reviewed SDK session did not stay retired after the wake")
        doc = _document(app, self.ledger)
        new = doc["segments"].get(receipt.new_segment)
        old = self.before["segments"]
        if (doc["selected_segment"] != receipt.new_segment or new is None or set(doc["segments"]) != {*old, receipt.new_segment}
                or hashlib.sha256(new["seed"].encode()).hexdigest() != receipt.seed_sha256
                or any(_stripped(doc["segments"][key]) != _stripped(value) for key, value in old.items())):
            raise ValueError("Claude's replacement segment or the retired segment changed after the wake")


def _stripped(segment):
    """A segment without the cache clock fields the summary loop may legitimately refresh."""
    return {key: value for key, value in segment.items() if key not in ("cache_used_at", "cache_model")}


class Progress:
    def __init__(self, binding, origin=None):
        self.binding = binding
        #: The root compaction execution that owns every callback this summary turn raises.
        self.origin = origin
        self.tool_ids = set()
        self.attempted = {}   # tool id -> the store path a Write/Edit asked for
        self.written = []     # those whose result came back without an error
        self.result_id = None
        self.summary = None
        self.before_selection = None
        self.stage = "SDK summary request"

    def tool(self, event):
        """Admit one tool event of the summary turn, or refuse the whole compaction."""
        if event.kind == "compaction" or not self.binding.tool_work:
            raise ValueError("Unexpected work in the reviewed Claude summary")
        if event.kind in {"tool_use", "tool_result"} and isinstance(event.tool_id, str) and event.tool_id:
            self.tool_ids.add(event.tool_id)
            if len(self.tool_ids) > MAX_TOOL_CALLS:
                raise ValueError("The reviewed Claude summary exceeded its tool-call budget")
        if event.kind == "tool_use" and event.complete and isinstance(event.tool_input, dict) \
                and any(word in (event.tool_name or "").lower() for word in WRITE_TOOLS):
            path = event.tool_input.get("file_path") or event.tool_input.get("path")
            if isinstance(path, str) and path:
                self.attempted[event.tool_id] = path
        elif event.kind == "tool_result" and not event.is_error and event.tool_id in self.attempted:
            # Only a write whose result did not fail is a store file; a denied one is not.
            self.written.append(self.attempted.pop(event.tool_id))

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
        action._stable(app, action._required_attempt(app).review, original=True, admitted=True,
                       audited=self.binding.tool_work)
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
        action._stable(app, action._required_attempt(app).review, original=True, admitted=True,
                       audited=self.binding.tool_work)
        self.binding.retired(app)
        if _document(app, self.binding.ledger) != self.before_selection:
            raise ValueError("Claude ledger changed during session retirement")
        self.stage = "replacement segment selection"

    def selected(self, app, new):
        binding = self.binding
        files = tuple(dict.fromkeys(self.written))
        if len(files) > MAX_STORE_FILES or any(not path or len(path) > MAX_PATH_CHARS for path in files):
            raise ValueError("The reviewed Claude summary wrote more store files than its receipt can name")
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
                              hashlib.sha256(self.summary.encode()).hexdigest(), _digest(binding.before), _digest(doc),
                              len(self.tool_ids), files)
