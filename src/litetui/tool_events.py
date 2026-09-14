"""Task-local ownership of tool presentation events."""

from contextlib import contextmanager
from contextvars import ContextVar

_external = ContextVar("external_tool_lifecycle", default=False)


def external_lifecycle():
    return _external.get()


@contextmanager
def native_lifecycle():
    token = _external.set(True)
    try:
        yield
    finally:
        _external.reset(token)
