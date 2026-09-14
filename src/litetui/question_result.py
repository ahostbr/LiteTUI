"""Capture structured question results without changing the legacy text tool API."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

_capture = ContextVar("question_result_capture", default=None)


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
