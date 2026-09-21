"""A turn that must not start yet, raised from _stream's pre-generation gate.

Distinct from a normal return on purpose. A parent-wake caller awaits the
_stream worker and, on normal completion, FINISHES its receipt — so a gate that
silently returned would mark a turn that never ran as done. Raising instead
propagates a typed outcome the caller can act on.

Because it is raised strictly BEFORE any generation or tool call, it is also the
one case where a claimed wake is provably side-effect free: the caller may
release it back to `pending` for a clean re-fire, rather than leaving it
`claimed`/uncertain (the state reserved for a turn that MAY have run tools).
"""
from __future__ import annotations


class TurnDeferred(RuntimeError):
    """Raised by the pre-generation gate when the turn cannot start now (MCP
    maintenance in progress, or its context changed during the wait)."""


#: _stream's result when it deferred at the gate instead of running a turn.
#: _stream CATCHES TurnDeferred and returns this rather than letting it escape
#: the Textual worker: @work defaults to exit_on_error=True, so a raised
#: exception would reach app._handle_exception and crash the TUI. A sentinel
#: result lets the worker end SUCCESS (→ on_worker_state_changed flushes/retries)
#: while a parent wake still distinguishes "deferred" from "ran".
STREAM_DEFERRED = object()
