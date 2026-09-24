"""Shutdown-onset admission lifecycle: block NEW model loads on every retained
backend session, synchronously, before teardown can yield the loop.

This does NOT drain capacity — it is begin_close ONLY (set the closing flag), so a
load dispatched during async teardown cannot reserve capacity we are about to stop
tracking. No release, no unload, no physical-cleanup claim; every lease/claim is
retained for a later drain/reconcile with real evidence. The broader quiescence
work stays blocked.
"""
from litetui import runtime_log


def _safe_error(exc: BaseException) -> str:
    """The exception TYPE name only — never str(exc)/repr(exc). Bounded is not
    redacted: an exception's message (not just repr's args) can carry a path, a
    token, or user text, and this string reaches the shutdown log and the report.
    The stable operation label at each call site says WHICH step failed; the type
    says WHAT kind of failure, and that is all a shutdown diagnostic needs."""
    return type(exc).__name__


def begin_shutdown(app) -> dict:
    """begin_close() every retained backend admission session at shutdown onset.

    Collects the current backend's session plus every ``app._admission_sessions``
    entry, deduped by object identity. Returns an inspectable report, never a bool:
    ``id(session) -> {closing, leases, active_loads, unsettled_loads, unload_claims}``
    for each session closed, and ``{"error": "<type: bounded msg>"}`` for a malformed
    registry entry or a session whose begin_close raised. No sessions -> empty report.

    A malformed registry entry or a failing begin_close NEVER aborts the rest: each
    failure is captured in the report AND surfaced to the shutdown log (record_error),
    so it is a visible unresolved state, not a swallowed green. Nothing is released or
    unloaded, and the registry is retained for later cleanup.
    """
    report: dict = {}
    candidates = []

    current = getattr(getattr(app, "backend", None), "_admission_session", None)
    if current is not None:
        candidates.append(current)

    registry = getattr(app, "_admission_sessions", None) or []
    for index, entry in enumerate(registry):
        try:
            _backend, session = entry
        except (TypeError, ValueError) as exc:
            # A malformed entry must not stop the valid current/other sessions.
            key = f"registry[{index}]"
            report[key] = {"error": _safe_error(exc)}
            runtime_log.record_error(
                "admission.shutdown_registry_malformed",
                detail=f"{key}: {_safe_error(exc)}",
                site="resource_session_lifecycle")
            continue
        candidates.append(session)

    seen = set()
    for session in candidates:
        if id(session) in seen:
            continue
        seen.add(id(session))
        try:
            session.begin_close()
            report[id(session)] = {
                "closing": bool(getattr(session, "_closing", False)),
                "leases": len(session.leases),
                "active_loads": len(session.active_loads),
                "unsettled_loads": len(session.unsettled_loads),
                "unload_claims": len(session.unload_claims),
            }
        except Exception as exc:  # noqa: BLE001 - one session must not skip the others
            report[id(session)] = {"error": _safe_error(exc)}
            runtime_log.record_error(
                "admission.shutdown_begin_close_failed",
                detail=f"begin_close: {_safe_error(exc)}",   # stable label + type only
                site="resource_session_lifecycle")
    return report
