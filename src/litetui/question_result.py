"""Capture structured question results without changing the legacy text tool API."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import Event

_capture = ContextVar("question_result_capture", default=None)
_lifetime = ContextVar("native_question_lifetime", default=None)
_origin = ContextVar("native_question_origin", default=None)


@contextmanager
def question_origin(thread_id, turn_id, item_id, *, delivery):
    """Keep wire request identity attached across the shared tool's worker thread."""
    token = _origin.set({
        "eventVersion": 1, "provider": "codex", "threadId": thread_id,
        "turnId": turn_id, "itemId": item_id, "delivery": delivery,
    })
    try:
        yield
    finally:
        _origin.reset(token)


def question_origin_fields():
    return dict(_origin.get() or {})


@contextmanager
def question_lifetime(cancelled: Event):
    """Carry native request cancellation into the shared tool's worker thread."""
    token = _lifetime.set(cancelled)
    try:
        yield
    finally:
        _lifetime.reset(token)


def current_lifetime():
    return _lifetime.get()


def question_cancelled():
    lifetime = current_lifetime()
    return lifetime is not None and lifetime.is_set()


@asynccontextmanager
async def question_slot(app):
    """RPC forms have separate IDs; Textual dialogs share one presentation slot."""
    if getattr(app, "_rpc", False):
        yield
        return
    lock = getattr(app, "_codex_question_ui_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app._codex_question_ui_lock = lock
    while True:
        if question_cancelled():
            raise asyncio.CancelledError
        try:
            await asyncio.wait_for(lock.acquire(), 0.1)
            break
        except TimeoutError:
            continue
    try:
        if question_cancelled():
            raise asyncio.CancelledError
        yield
    finally:
        lock.release()


@contextmanager
def capture_answers():
    results = []
    token = _capture.set(results)
    try:
        yield results
    finally:
        _capture.reset(token)


def publish(payload):
    target = _capture.get()
    if target is not None:
        target.append(deepcopy(payload))


def native_answers(question_ids, payload):
    """Only an explicit submit is a native answer, never chat/partial/cancel."""
    if payload.get("action") != "submit":
        return {}
    answers = {}
    for ident, question in zip(question_ids, payload.get("questions", [])):
        if not question.get("answered"):
            continue
        options = question.get("options", [])
        selected = [
            options[index]["title"]
            for index in question.get("selected", [])
            if isinstance(index, int) and 0 <= index < len(options)
        ]
        note = str(question.get("note") or "").strip()
        if note:
            selected.append(note)
        if selected:
            answers[ident] = {"answers": selected}
    return answers
