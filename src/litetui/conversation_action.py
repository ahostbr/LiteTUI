"""Exact ownership and retirement for reviewed conversation actions.

The source owns selection, admission, the append-only snapshot, and the exact
execution worker. A displayed history fragment is never execution authority.
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import uuid
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import partial, wraps
from typing import Any

from litetui import (
    backend_catalog,
    conversation_transition,
    native_history,
)
from litetui import history_projection as projection
from litetui.event_origin import EventOrigin
from litetui.shutdown_wait import join_task

VERSION = 1
MAX_TEXT = 65536
MAX_SEGMENTS = 100
MAX_CALLBACKS = 512
MAX_WORKERS = 512
PREFIX = "gui.conversations.native_rewrite_"
COMMON = ("version", "session_id", "agent_id", "backend", "model")
SELECTION = ("history_snapshot", "context_sha256", "message_index", "message_sha256")
REVIEW_FIELDS = (*COMMON, *SELECTION, "snapshot", "review", "action_id", "action", "text", "text_sha256",
                 "prepared_context_sha256", "prepared_messages", "prefix_context_sha256", "prefix_messages",
                 "discarded_messages")
CLOSE_REQUIRED = "Conversation rewrite is uncertain; close this runtime and reopen explicitly."
_SCOPE: ContextVar[Any] = ContextVar("conversation_rewrite_scope", default=None)
_SCHEDULE: ContextVar[Any] = ContextVar("conversation_rewrite_schedule", default=None)
_INLINE: ContextVar[Any] = ContextVar("conversation_rewrite_inline", default=None)
_EVENT_SCOPE: ContextVar[Any] = ContextVar("conversation_rewrite_events", default=None)
_THREAD_EVENT: ContextVar[Any] = ContextVar("conversation_rewrite_thread_events", default=None)
_READBACK: ContextVar[Any] = ContextVar("conversation_action_readback", default=None)


@dataclass(frozen=True)
class Review:
    wire: bytes
    objects: tuple[Any, ...]
    protected: bytes
    files: tuple[Any, ...]
    hooks: Any
    registry: tuple[Any, ...]
    history: bytes
    raw: bytes
    conversation: Any
    profile: str | None
    prior_profile: Any
    receipts: bytes


@dataclass(frozen=True)
class Execution:
    worker: Any
    origin: EventOrigin
    task: Any = None


@dataclass(frozen=True)
class OwnedWork:
    worker: Any
    role: str
    origin: EventOrigin
    task: Any = None
    predecessor: str | None = None
    root: bool = False


@dataclass(frozen=True)
class ThreadEvent:
    app: Any
    attempt: Any
    origin: EventOrigin
    thread_id: int
    retired: Any


@dataclass(frozen=True)
class QuestionOrigin:
    pending: tuple[Any, Any, Any]
    origin: EventOrigin


@dataclass
class ToolCall:
    origin: EventOrigin
    cancelled: Any
    future: Any = None
    producer: Any = None
    retired: Any = None


@dataclass
class Attempt:
    review: Review
    operation: str
    owner: Any
    kind: str = "rewrite"
    root_outcome: Any = None
    root_history: bytes | None = None
    root_started: bool = False
    publication: Any = None
    execution: Execution | None = None
    descendants: list[Any] | None = None
    outcome: str = "refused"
    mutated: bool = False
    written: bool = False
    verified: bool = False
    pending: bool = True
    reason: str | None = None
    refusal: Review | None = None
    refusal_history: bytes | None = None
    owned: list[OwnedWork] | None = None
    callbacks: list[Any] | None = None
    drain: Any = None
    segments: list[str] | None = None
    receipts: bytes | None = None
    stopped: bool = False
    scheduled_callbacks: int = 0
    tool_calls: list[ToolCall] | None = None
    questions: dict[str, QuestionOrigin] = field(default_factory=dict)
    question_lock: Any = field(default_factory=threading.RLock)


def _task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _attempt(app) -> Attempt | None:
    value = getattr(app, "_conversation_rewrite_attempt", None)
    return value if isinstance(value, Attempt) else None


def register_attempt(app, attempt):
    require_open(app)
    app._conversation_rewrite_attempt = attempt  # compatibility: the single captured-action slot
    setattr(app, "_conversation_" + attempt.kind + "_last_attempt", attempt)


def _required_attempt(app) -> Attempt:
    attempt = _attempt(app)
    if attempt is None:
        raise ValueError("Rewrite has no captured action")
    return attempt


def _authorized(app) -> bool:
    scope = _SCOPE.get()
    attempt = _attempt(app)
    if scope is None or attempt is None or scope[0] is not app or scope[1] is not attempt or scope[2] is not _task():
        return False
    if scope[3] == "admission":
        return attempt.owner is scope[2] and attempt.execution is None
    if scope[3] == "settled":
        return attempt.execution is None or attempt.execution.worker.is_finished
    if scope[3] == "aftercare":
        return attempt.drain is scope[2]
    if scope[3] == "descendant":
        from textual.worker import NoActiveWorker, get_current_worker
        binding = scope[4]
        try:
            return (binding.role in {"compaction", "continuation", "card-summary"}
                    and binding.task is scope[2] and get_current_worker() is binding.worker
                    and any(item is binding for item in attempt.owned or ()))
        except NoActiveWorker:
            return False
    if scope[3] == "execution":
        from textual.worker import NoActiveWorker, get_current_worker
        try:
            return (attempt.execution is not None and attempt.execution.task is scope[2]
                    and get_current_worker() is attempt.execution.worker)
        except NoActiveWorker:
            return False
    return False


@contextmanager
def _scope(app, attempt, role, binding=None):
    token = _SCOPE.set((app, attempt, _task(), role, binding))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def submission_refusal(app):
    if getattr(app, "_conversation_rewrite_closing", False):
        return "Conversation rewrite runtime is shutting down."
    if getattr(app, "_conversation_rewrite_error", None):
        return CLOSE_REQUIRED
    attempt = _attempt(app)
    if attempt is not None and attempt.pending and not _authorized(app):
        return "An edited/retried turn awaits exact worker settlement; wait before sending or changing history."
    return None


def require_open(app):
    if why := submission_refusal(app):
        raise ValueError(why)


def event_origin(app):
    """Only an exact captured producer may attribute maintenance to this action."""
    if not _authorized(app):
        attempt = _attempt(app)
        if attempt is None:
            return None
        thread = _THREAD_EVENT.get()
        if (thread is not None and thread.app is app and thread.attempt is attempt
                and attempt.pending and thread.thread_id == threading.get_ident()
                and _task() is None and not thread.retired.is_set()):
            return thread.origin
        scope = _EVENT_SCOPE.get()
        if (scope is not None and scope[0] is app and scope[1] is attempt
                and attempt.pending and scope[2] is _task()
                and any(call is scope[3] for call in attempt.tool_calls or ())
                and scope[3].producer is scope[2]):
            return scope[3].origin
        return None
    attempt = _required_attempt(app)
    scope = _SCOPE.get()
    inline = _INLINE.get()
    if inline is not None and inline[0] is app and inline[1] is attempt and inline[2] is _task():
        return inline[3]
    if scope[3] == "execution":
        return attempt.execution.origin if attempt.execution is not None else None
    if scope[3] == "descendant":
        return scope[4].origin
    if scope[3] == "aftercare":
        return EventOrigin(session_id=app.convo_id, operation_id=attempt.operation, source="rpc",
                           execution_id=attempt.segments[-1] if attempt.segments else None)
    return None


@contextmanager
def readback(app):
    """Synchronous owned-history read only; never admission for a foreign task."""
    if not _authorized(app):
        raise ValueError("History readback lacks its exact captured producer")
    token = _READBACK.set((app, _required_attempt(app), _task()))
    try:
        yield
    finally:
        _READBACK.reset(token)


def readback_allowed(app):
    value = _READBACK.get()
    return (value is not None and value[0] is app and value[1] is _attempt(app)
            and value[2] is _task() and _authorized(app))


def stream_origin(app, origin):
    captured = event_origin(app)
    if captured is not None:
        return EventOrigin(session_id=captured.session_id, operation_id=captured.operation_id,
                           source="rpc", hook_id=origin.hook_id if origin else None)
    require_open(app)
    return origin or EventOrigin(session_id=app.convo_id)


def inline_runner(app, origin, callback):
    if not _authorized(app):
        return callback()
    attempt = _required_attempt(app)
    if len(attempt.segments or ()) >= MAX_SEGMENTS:
        raise ValueError("Rewrite exceeded its continuation budget; close the runtime")
    async def run():
        if not _authorized(app) or _attempt(app) is not attempt:
            raise ValueError("Unowned inline rewrite continuation")
        token = _INLINE.set((app, attempt, _task(), origin))
        try:
            return await callback()
        finally:
            _INLINE.reset(token)
    return run()


def stop(app):
    attempt = _attempt(app)
    if attempt is None or not attempt.pending:
        return False
    attempt.stopped = True
    for call in attempt.tool_calls or ():
        call.cancelled.set()
    return True


def stopped(app):
    attempt = _attempt(app)
    return attempt is not None and attempt.pending and attempt.stopped


def _continuation(callback):
    return getattr(callback, "__name__", "") in {
        "_maybe_autocompact", "_run_self_compaction", "_continue_interrupted_compact", "_wake_after_compact"}


def segment(app, origin):
    if not _authorized(app) or origin is None:
        return {}
    attempt = _required_attempt(app)
    if origin.operation_id != attempt.operation or origin.session_id != app.convo_id or origin.source != "rpc":
        raise ValueError("Rewrite continuation changed its execution identity")
    if len(attempt.segments or ()) >= MAX_SEGMENTS:
        raise ValueError("Rewrite exceeded the bounded continuation segment budget; close the runtime")
    if not origin.execution_id or origin.execution_id in (attempt.segments or ()):
        raise ValueError("Rewrite repeated an execution segment")
    if attempt.segments is None:
        attempt.segments = []
    previous = attempt.segments[-1] if attempt.segments else None
    index = len(attempt.segments)
    attempt.segments.append(origin.execution_id)
    return {attempt.kind + "_segment": {"action_id": json.loads(attempt.review.wire)["action_id"],
                                "index": index, "previous_execution_id": previous}}


def after_refresh(app, callback, *args):
    """Retain canonical aftercare until its preceding chat worker has retired."""
    attempt = _attempt(app)
    if attempt is None or not _authorized(app):
        require_open(app)
        return app.call_after_refresh(callback, *args)
    if (getattr(app, "_conversation_rewrite_closing", False)
            or attempt.stopped and _continuation(callback)):
        return False  # Explicit shutdown/Stop cancels continuation, never ordinary settlement.
    if attempt.scheduled_callbacks >= MAX_CALLBACKS:
        raise ValueError("Rewrite exceeded its captured callback budget; close the runtime")
    attempt.scheduled_callbacks += 1
    if attempt.callbacks is None:
        attempt.callbacks = []
    attempt.callbacks.append((callback, args))
    return True


def _work_budget(app):
    if len(_required_attempt(app).owned or ()) >= MAX_WORKERS:
        raise ValueError("Rewrite exceeded its captured worker budget; close the runtime")


def _owned_runner(app, attempt: Attempt, role, origin, callback):
    async def run():
        from textual.worker import get_current_worker
        worker = get_current_worker()
        prior = next((item for item in attempt.owned or () if item.worker is worker), None)
        if (prior is None or prior.task is not None or prior.role != role or prior.origin != origin
                or _attempt(app) is not attempt):
            raise ValueError("Unregistered rewrite descendant")
        binding = replace(prior, task=_task())
        assert attempt.owned is not None
        attempt.owned[attempt.owned.index(prior)] = binding
        with _scope(app, attempt, "descendant", binding):
            if attempt.stopped and role in {"compaction", "continuation"} and not binding.root:
                return  # Stop retires scheduled descendants before a new boundary/effect.
            marker = {"type": "rewrite_aftercare", "action_id": json.loads(attempt.review.wire)["action_id"],
                      "aftercare_id": origin.execution_id, "kind": "compaction",
                      "after_execution_id": binding.predecessor}
            if attempt.kind == "compact":
                marker.update(type="compact_maintenance", root=binding.root)
            if role == "compaction":
                if binding.root:
                    attempt.root_started = True
                app._rpc_emit({**marker, "phase": "start"}, origin=origin)
            try:
                if role != "context":
                    _stable(app, attempt.review, admitted=True)
                if binding.root:
                    from litetui import conversation_compact
                    if attempt.stopped:
                        result = conversation_compact.CompactOutcome("cancelled", "cancelled")
                    else:
                        _stable(app, attempt.review, original=True, admitted=True)
                        result = await callback()
                    conversation_compact.capture_root_result(app, attempt, result)
                    return result
                return await callback()
            finally:
                if role == "compaction":
                    app._rpc_emit({**marker, "phase": "end"}, origin=origin)
    return run


def _track_owned(app, worker, role, origin, *, root=False):
    attempt = _required_attempt(app)
    if attempt.owned is None:
        attempt.owned = []
    if any(item.worker is worker for item in attempt.owned):
        raise ValueError("Rewrite descendant was already registered")
    attempt.owned.append(OwnedWork(worker, role, origin,
        predecessor=attempt.segments[-1] if attempt.segments else None, root=root))


def captured_work(role, **options):
    """Preserve Textual work semantics; bind only canonical decorated descendants."""
    from textual import work
    def decorate(function):
        legacy = work(**options)(function)
        @wraps(function)
        def start(app, *args, **kwargs):
            if not _authorized(app):
                require_open(app)
                return legacy(app, *args, **kwargs)
            _work_budget(app)
            attempt = _required_attempt(app)
            scheduling = _SCHEDULE.get()
            root = (attempt.kind == "compact" and role == "compaction" and function.__name__ == "_compact"
                    and scheduling is not None and scheduling == (app, attempt, _task()))
            if root and attempt.execution is not None:
                raise ValueError("Compaction root was already scheduled")
            execution_id = (json.loads(attempt.review.wire)["root_execution_id"] if root else str(uuid.uuid4()))
            origin = EventOrigin(session_id=app.convo_id, operation_id=attempt.operation, source="rpc",
                                 execution_id=execution_id if role == "compaction" else None)
            runner = _owned_runner(app, _required_attempt(app), role, origin, partial(function, app, *args, **kwargs))
            worker = app.run_worker(runner, name=function.__name__, **{**options, "exit_on_error": False})
            _track_owned(app, worker, role, origin, root=root)
            if root:
                attempt.execution = Execution(worker, origin)
            return worker
        return start
    return decorate


def card_summary(app, callback):
    options = {"group": "card-summary", "exclusive": False, "exit_on_error": False}
    if not _authorized(app):
        require_open(app)
        return app.run_worker(callback(), **options)
    _work_budget(app)
    origin = EventOrigin(session_id=app.convo_id, operation_id=_required_attempt(app).operation, source="rpc")
    worker = app.run_worker(_owned_runner(app, _required_attempt(app), "card-summary", origin, callback), **options)
    _track_owned(app, worker, "card-summary", origin)
    return worker


def advance_receipts(app):
    """Canonical injection/compaction may change only the boolean receipt."""
    if not _authorized(app):
        return
    attempt = _required_attempt(app)
    before = json.loads(attempt.receipts or attempt.review.receipts)
    after = json.loads(_receipts(app))
    if before["store_identity"] != after["store_identity"] or type(after["store_injected"]) is not bool:
        raise ValueError("Rewrite changed its store authority")
    captured = native_history.capture_file(app.convo_path)
    if _saved_receipts(captured) != projection.encoded(after):
        raise ValueError("Canonical rewrite receipt update was not durable")
    attempt.receipts = projection.encoded(after)


def operation_allowed(app, operation):
    if native_history.enabled(app) and operation in {"gui.conversations.edit", "gui.conversations.retry", "gui.conversations.compact"}:
        raise ValueError("Edit, Retry and Compact require their reviewed native action protocols")
    if operation in {PREFIX + "settled", "gui.conversations.native_compact_settled",
                     "gui.hello", "gui.state", "gui.tool_policy.get"}:
        return
    require_open(app)


def stream_runner(app, origin, callback):
    """Bind initial admission or an exact canonical continuation, never ambient state."""
    scheduling = _SCHEDULE.get()
    initial = scheduling is not None and scheduling[0] is app and scheduling[2] is _task()
    if not initial:
        if _authorized(app):
            _work_budget(app)
            if len(_required_attempt(app).segments or ()) >= MAX_SEGMENTS:
                raise ValueError("Rewrite exceeded its continuation budget; close the runtime")
            return _owned_runner(app, _required_attempt(app), "continuation", origin, callback)
        return callback
    attempt: Attempt = scheduling[1]
    if _attempt(app) is not attempt or attempt.execution is not None or not attempt.verified:
        raise ValueError("Rewrite execution was already scheduled or lacks durable verification")
    if origin.operation_id != attempt.operation or origin.session_id != app.convo_id or origin.source != "rpc":
        raise ValueError("Rewrite execution changed its admitted origin")

    async def run():
        from textual.worker import get_current_worker
        binding = attempt.execution
        if (binding is None or binding.worker is not get_current_worker() or binding.origin != origin
                or binding.task is not None or _attempt(app) is not attempt):
            raise ValueError("Unregistered or replayed rewrite worker")
        attempt.execution = replace(binding, task=_task())
        with _scope(app, attempt, "execution"):
            _stable(app, attempt.review, admitted=True)
            await callback()
    return run


def track_stream(app, worker, origin):
    scheduling = _SCHEDULE.get()
    if scheduling is None or scheduling[0] is not app or scheduling[2] is not _task():
        if _authorized(app):
            _track_owned(app, worker, "continuation", origin)
        return
    attempt: Attempt = scheduling[1]
    if _attempt(app) is not attempt or attempt.execution is not None:
        raise ValueError("Rewrite already has an execution worker")
    attempt.execution = Execution(worker, origin)


def track_descendant(app, worker):
    """Cleanup ownership only; descendants never receive execution admission."""
    attempt = _attempt(app)
    if worker is not None and attempt is not None and _authorized(app):
        if attempt.descendants is None:
            attempt.descendants = []
        if not any(value is worker for value in attempt.descendants):
            attempt.descendants.append(worker)


def tool_refusal(app, name, args):
    """Background results need a separate admission chain; start no work here."""
    attempt = _attempt(app)
    if attempt is None or not attempt.pending:
        return None
    from litetui import tasks
    if tasks.backgroundable(name) and (
            isinstance(args, dict) and bool(args.get("background"))
            or int(getattr(app.settings, "tool_auto_background_s", 0) or 0) > 0):
        return ("[denied] background or auto-promoted execution is unavailable during reviewed "
                "conversation actions; no work started. Use foreground execution with auto-promotion disabled.")
    return None


def capture_tool(app, callback):
    """Capture event attribution only at the admitted async tool invocation seam."""
    if not _authorized(app):
        require_open(app)
        return callback, None
    attempt = _required_attempt(app)
    if len(attempt.tool_calls or ()) >= MAX_WORKERS:
        raise ValueError("Rewrite exceeded its captured tool-call budget; close the runtime")
    origin = event_origin(app)
    if origin is None or origin.execution_id is None:
        raise ValueError("Rewrite tool lacks its exact execution producer")
    call = ToolCall(origin, threading.Event(), retired=threading.Event())
    lock = threading.Lock()
    claimed = False

    def invoke(*args, **kwargs):
        nonlocal claimed
        with lock:
            if claimed:
                raise ValueError("Rewrite tool callback cannot be replayed")
            claimed = True
        if _task() is not None or call.cancelled.is_set():
            call.retired.set()
            raise asyncio.CancelledError
        retired = call.retired
        binding = ThreadEvent(app, attempt, origin, threading.get_ident(), retired)
        token = _THREAD_EVENT.set(binding)
        from litetui.question_result import question_lifetime
        try:
            with question_lifetime(call.cancelled):
                return callback(*args, **kwargs)
        finally:
            retired.set()
            _THREAD_EVENT.reset(token)
    return invoke, call


def track_tool(app, call, awaitable):
    """Track the actual tool future (and after-hook), without admitting its task."""
    if call is None:
        return awaitable
    if not _authorized(app):
        raise ValueError("Unowned tool future registration")
    attempt = _required_attempt(app)
    if call.future is not None:
        raise ValueError("Tool future was already registered")
    if attempt.tool_calls is None:
        attempt.tool_calls = []

    async def run():
        if call.producer is not _task() or _attempt(app) is not attempt:
            raise ValueError("Tool future changed its captured owner")
        token = _EVENT_SCOPE.set((app, attempt, _task(), call))
        try:
            return await awaitable
        finally:
            _EVENT_SCOPE.reset(token)

    async def start():
        # Start beneath the canonical CURRENT/PROCESS_SLOT handoff, not in the
        # capturing turn task: background and promoted shells rely on these.
        if call.future is not None or _attempt(app) is not attempt:
            raise ValueError("Tool awaitable was replayed or changed owner")
        if call.cancelled.is_set():
            call.retired.set()
            raise asyncio.CancelledError
        call.producer = asyncio.create_task(run())
        async def retire():
            try:
                return await asyncio.shield(call.producer)
            except asyncio.CancelledError:
                call.cancelled.set()
                await join_task(call.producer)
                raise
        call.future = asyncio.create_task(retire())
        try:
            return await asyncio.shield(call.future)
        except asyncio.CancelledError:
            call.cancelled.set()
            await join_task(call.future)
            raise
    attempt.tool_calls.append(call)
    return start()


async def _join_tools(attempt, *, cancel=False):
    for call in attempt.tool_calls or ():
        if cancel:
            call.cancelled.set()
    for call in attempt.tool_calls or ():
        if call.future is None:
            continue  # Its canonical handoff worker retired before starting it.
        try:
            await join_task(call.future)
        except Exception as exc:  # noqa: BLE001 - ordinary tool failures remain canonical results
            from litetui import runtime_log
            runtime_log.record("rewrite.tool_settled_error", error=type(exc).__name__)
        if not call.future.done() or not call.producer.done() or not call.retired.is_set():
            raise RuntimeError("Rewrite tool callback did not retire")


def question_registry_guard(app):
    """Serialize only source question publication/cancellation, granting no admission."""
    attempt = _attempt(app)
    return attempt.question_lock if attempt is not None and attempt.pending else nullcontext()


def capture_question(app, ask_id, pending):
    attempt = _attempt(app)
    if attempt is None or not attempt.pending:
        return
    origin = event_origin(app)
    if origin is None or origin.execution_id is None:
        raise ValueError("Rewrite question lacks its exact event producer")
    if ask_id in attempt.questions or len(attempt.questions) >= MAX_WORKERS:
        raise ValueError("Rewrite question identity was reused or exceeded its bound")
    attempt.questions[ask_id] = QuestionOrigin(pending, origin)


def question_abort_origins(app):
    """Capture origins before releasing exact waiters; the abort task gets no scope."""
    attempt = _attempt(app)
    if attempt is None or not attempt.pending:
        return {}
    registry = getattr(app, "_rpc_pending_asks", {})
    return {ask_id: binding.origin for ask_id, binding in attempt.questions.items()
            if registry.get(ask_id) is binding.pending and not binding.pending[0].is_set()}


def forget_question(app, ask_id, pending):
    attempt = _attempt(app)
    if attempt is not None:
        binding = attempt.questions.get(ask_id)
        if binding is not None and binding.pending is pending:
            attempt.questions.pop(ask_id)


def tool_after_allow_prompt(app):
    attempt = _attempt(app)
    return not (attempt is not None and attempt.pending and
                (attempt.stopped or getattr(app, "_conversation_rewrite_closing", False)))


def hook_cleanup_scope(app):
    attempt = _attempt(app)
    if attempt is None or not attempt.pending:
        return False
    if _authorized(app):
        return True
    scope = _EVENT_SCOPE.get()
    return (scope is not None and scope[0] is app and scope[1] is attempt and scope[2] is _task()
            and scope[3].producer is scope[2] and any(call is scope[3] for call in attempt.tool_calls or ()))


def _operation(cmd):
    value = cmd.get("id")
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 128:
        raise ValueError("Rewrite request needs a nonempty string id of at most 128 bytes")
    return value


def _identity(app):
    return {"version": VERSION, "session_id": app.convo_id,
            "agent_id": app._agent_session.authority.agent_id, "backend": app.backend.name, "model": app.model_id}


def _history_command(app):
    return {**_identity(app), "from_session_id": app.convo_id, "scope": "current", "catalog_snapshot": None}


def _objects(app):
    return (app._agent_session, app.store, app.backend, app.settings, getattr(app, "client", None),
            getattr(app, "_client_binding", None))


def _protected(app):
    facts = json.loads(conversation_transition._protected(app))
    # Admission canonically stamps these two transient turn fields. They are
    # checked separately against the reviewed old/new profile, not ignored.
    facts.pop("hook_source", None)
    facts["policy"].pop("tool_profile", None)
    return projection.encoded(facts)


def _files(app):
    session = app._agent_session
    return tuple(backend_catalog._disk_hash(path, 1024 * 1024) for path in (
        session.store.data_root / "settings.json", session.memory_root / "settings.json",
        app.convo_dir / "settings.json"))


def _receipts(app):
    return projection.encoded({"store_identity": getattr(app, "_prompt_store_identity", None),
                               "store_injected": getattr(app, "_store_injected", None)})


def _saved_receipts(captured):
    return projection.encoded({key: captured.meta.get(key) for key in ("store_identity", "store_injected")})


def _prefix(app, messages):
    seen = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ValueError("Retained prefix has an unsupported message shape")
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list) or calls and message.get("role") != "assistant":
            raise ValueError("Retained prefix has malformed tool calls")
        for call in calls:
            ident = call.get("id") if isinstance(call, dict) else None
            if not isinstance(ident, str) or not ident or ident in seen:
                raise ValueError("Retained prefix has missing or duplicated tool call identities")
            seen.add(ident)
        if message.get("role") == "tool" and (not isinstance(message.get("tool_call_id"), str)
                                                or not message["tool_call_id"]):
            raise ValueError("Retained prefix has an invalid tool result identity")
    if messages and app._safe_tail(messages, len(messages)) != messages:
        raise ValueError("Retained prefix is not a complete tool boundary")


def _stable(app, review, *, original=False, admitted=False):
    if getattr(app, "_conversation_transition_cleanup_error", None):
        raise ValueError("Rewrite hook subprocess cleanup is unproven")
    if any(current is not prior for current, prior in zip(_objects(app), review.objects, strict=True)):
        raise ValueError("Rewrite runtime objects changed")
    if (_protected(app) != review.protected or _files(app) != review.files
            or conversation_transition._hooks(app) != review.hooks
            or conversation_transition._registry(app) != review.registry
            or _receipts(app) != ((_required_attempt(app).receipts or review.receipts) if admitted else review.receipts)):
        raise ValueError("Rewrite settings, authority, policy, hooks or store receipts changed")
    if getattr(app, "_active_tool_profile", None) != (review.profile if admitted else review.prior_profile):
        raise ValueError("Rewrite effective tool profile changed")
    if getattr(app, "_pending_input", ()) or getattr(app, "_restored_input_error", False):
        raise ValueError("Rewrite acquired queued or uncertain input")
    if getattr(app, "_gui_quitting", False) or getattr(app, "_conversation_rewrite_closing", False):
        raise ValueError("Rewrite shutdown requested")
    app._validate_owned_execution()
    if original and (app.conversation is not review.conversation or projection.encoded(app.conversation) != review.history
                     or native_history.capture_file(app.convo_path).raw != review.raw):
        raise ValueError("Rewrite history changed before publication")


def _echo(cmd, wire, keys):
    return all(key in cmd and projection.encoded(cmd[key]) == projection.encoded(wire[key]) for key in keys)


def _refusal_checkpoint(app, attempt):
    wire = json.loads(attempt.review.wire)
    if not _echo(_identity(app), wire, COMMON):
        raise ValueError("Rewrite owned authority changed; close the runtime")
    original = attempt.review.objects
    current = _objects(app)
    if any(current[index] is not original[index] for index in (0, 1, 2, 4, 5)):
        raise ValueError("Rewrite runtime ownership changed before refusal")
    descriptor, captured, _index = native_history._collect(app, _history_command(app))
    if _saved_receipts(captured) != _receipts(app):
        raise ValueError("Refusal cannot prove coherent durable store receipts")
    attempt.refusal = replace(attempt.review, objects=current, protected=_protected(app), files=_files(app),
        hooks=conversation_transition._hooks(app), registry=conversation_transition._registry(app),
        history=captured.messages_bytes, raw=captured.raw or b"", conversation=app.conversation,
        prior_profile=getattr(app, "_active_tool_profile", None), receipts=_receipts(app))
    _stable(app, attempt.refusal, original=True)
    attempt.refusal_history = projection.encoded(descriptor)


async def _join_workers(attempt: Attempt, *, cancel=False):
    from textual.worker import WorkerCancelled, WorkerFailed
    joined: list[Any] = []
    while True:
        workers = ([attempt.execution.worker] if attempt.execution is not None else []) + [item.worker for item in attempt.owned or ()] + (attempt.descendants or [])
        worker = next((value for value in workers if not any(value is done for done in joined)), None)
        if worker is None:
            await _join_tools(attempt, cancel=cancel)
            return
        if cancel and not worker.is_finished:
            worker.cancel()
        try:
            await asyncio.shield(worker.wait())
        except WorkerCancelled:
            pass
        except WorkerFailed:
            if not cancel:
                raise
        if not worker.is_finished:
            raise RuntimeError("Rewrite worker did not retire")
        joined.append(worker)


async def _drain(app, attempt: Attempt):
    # This task owns scheduling only; every privileged worker has its own exact binding.
    from textual.worker import WorkerCancelled
    try:
        assert attempt.execution is not None
        try:
            await attempt.execution.worker.wait()
        except WorkerCancelled:
            pass
        with _scope(app, attempt, "aftercare"):
            while True:
                await _join_workers(attempt)
                if getattr(app, "_conversation_rewrite_closing", False):
                    attempt.callbacks = []
                    return
                if not attempt.callbacks:
                    break
                _stable(app, attempt.review, admitted=True)
                callback, args = attempt.callbacks.pop(0)
                if not attempt.stopped or not _continuation(callback):
                    callback(*args)
    except BaseException:
        app._conversation_rewrite_error = CLOSE_REQUIRED
        raise


async def _settle_shutdown(app):
    attempt = _attempt(app)
    pending = bool(attempt is not None and attempt.pending)
    if pending and attempt is not None:
        for call in attempt.tool_calls or ():
            call.cancelled.set()
            # Only the exact producer whose OS callback has retired can be
            # cancelled: this releases an already-open after-hook approval or
            # runs canonical subprocess cleanup, without abandoning a thread.
            if call.retired.is_set() and call.producer is not None and not call.producer.done():
                call.producer.cancel()
        if attempt.owner is _task():
            raise RuntimeError("Rewrite admission cannot join itself")
        if attempt.owner is not None and not attempt.owner.done():
            attempt.owner.cancel()
            await join_task(attempt.owner)
        await _join_workers(attempt, cancel=True)
        if attempt.drain is not None:
            try:
                await join_task(attempt.drain)
            except Exception as exc:  # noqa: BLE001 - shutdown proves retirement, not effects
                from litetui import runtime_log
                runtime_log.record("rewrite.aftercare_failed_at_shutdown", error=type(exc).__name__)
        await _join_workers(attempt, cancel=True)
        if getattr(app, "_conversation_transition_cleanup_error", None):
            raise RuntimeError("Rewrite hook cleanup remains unproven")
        if getattr(app, "_hook_lifecycle", ()) or app._hook_lock.locked():
            raise RuntimeError("Rewrite hooks did not settle")
        attempt.pending = False
    wire = json.loads(attempt.review.wire) if attempt is not None else None
    return {"version": VERSION, "worker_settled": True, "admission_settled": True, "hooks_settled": True,
        "effects_verified": False, "pending_at_close": pending, "mutated": bool(attempt and attempt.mutated),
        **{key: wire[key] if wire else None for key in ("action_id", "action", "session_id", "agent_id", "backend", "model")},
        "operation_id": attempt.operation if attempt else None, "current_session_id": app.convo_id}


async def _shutdown_proofs(app):
    active = _attempt(app)
    active_proof = await _settle_shutdown(app)
    proofs = {}
    for kind in ("rewrite", "compact"):
        if getattr(app, "_conversation_" + kind + "_protocol", False) is not True:
            continue
        prior = getattr(app, "_conversation_" + kind + "_last_attempt", None)
        if prior is None and active is not None and active.kind == kind:
            prior = active
        wire = json.loads(prior.review.wire) if prior is not None else None
        proofs["conversation_" + kind + "_shutdown"] = {
            **active_proof, "pending_at_close": bool(prior is active and active_proof["pending_at_close"]),
            "mutated": bool(prior and prior.mutated),
            **{key: wire[key] if wire else None for key in
               ("action_id", "action", "session_id", "agent_id", "backend", "model")},
            "operation_id": prior.operation if prior else None}
    return proofs


async def settle_all_before_shutdown(app):
    kinds = [kind for kind in ("rewrite", "compact")
             if getattr(app, "_conversation_" + kind + "_protocol", False) is True]
    if not kinds:
        return {}
    app._gui_quitting = True
    app._conversation_rewrite_closing = True
    task = getattr(app, "_conversation_action_shutdown_task", None)
    if task is None:
        task = asyncio.create_task(_shutdown_proofs(app))
        app._conversation_action_shutdown_task = task
    async def select(key):
        return (await join_task(task))[key]
    for kind in kinds:
        attribute = "_conversation_" + kind + "_shutdown_task"
        if getattr(app, attribute, None) is None:
            setattr(app, attribute, asyncio.create_task(select("conversation_" + kind + "_shutdown")))
    result = await join_task(task)
    if result is None:
        raise RuntimeError("Reviewed action shutdown did not prove settlement")
    for kind in kinds:
        await join_task(getattr(app, "_conversation_" + kind + "_shutdown_task"))
    return copy.deepcopy(result)


async def settle_before_shutdown(app):
    proofs = await settle_all_before_shutdown(app)
    return proofs.get("conversation_rewrite_shutdown", proofs.get("conversation_compact_shutdown"))


async def _finish_exit(app):
    result = None
    error = None
    try:
        actions = await settle_all_before_shutdown(app)
        from litetui import conversation_shutdown, native_engine_shutdown
        conversation = await conversation_shutdown.settle_before_shutdown(app)
        engine = await native_engine_shutdown.settle_before_shutdown(app)
        if engine is not None:
            proof = engine["shutdown"]
            if (engine["worker_settled"] is not True or proof["retained"] is not False
                    or proof["owned"] is not False and proof["main_exited"] is not True):
                raise RuntimeError("Engine cleanup remains unproven")
        result = {"ok": True, **actions}
        if conversation is not None:
            result["conversation_shutdown"] = conversation
        if engine is not None:
            result["engine_shutdown"] = engine
    except Exception as exc:  # noqa: BLE001 - no acknowledgement or lease release without joins
        error = "Edit/Retry shutdown is unproven: " + type(exc).__name__
    callbacks = getattr(app, "_conversation_rewrite_shutdown_callbacks", [])
    app._conversation_rewrite_shutdown_callbacks = []
    for callback in callbacks:
        callback(ok=result is not None, result=result, error=error)
    if result is not None:
        app.exit()
    else:
        from litetui import runtime_log
        runtime_log.record_error("conversation_rewrite_shutdown_unproven", detail=error or "Unknown")


def request_shutdown(app, respond=None):
    if not any(getattr(app, "_conversation_" + kind + "_protocol", False) is True
               for kind in ("rewrite", "compact")):
        return False
    app._gui_quitting = True
    app._conversation_rewrite_closing = True
    if not hasattr(app, "_conversation_rewrite_shutdown_callbacks"):
        app._conversation_rewrite_shutdown_callbacks = []
    if respond is not None:
        app._conversation_rewrite_shutdown_callbacks.append(respond)
    task = getattr(app, "_conversation_rewrite_exit_task", None)
    if task is None or task.done():
        app._conversation_rewrite_exit_task = asyncio.create_task(_finish_exit(app))
    return True
