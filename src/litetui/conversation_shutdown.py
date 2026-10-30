"""Join a reviewed conversation mutation before RPC exit or ownership release.

Settlement is not persistence verification or successful mutation. Native keeps
its bounded shutdown deadline and contained-process fallback when proof fails.
"""
from __future__ import annotations

import asyncio
import copy

from litetui.shutdown_wait import join_task

IDENTITY = ("action_id", "action", "from_session_id", "target_session_id",
            "agent_id", "backend", "model")


def _enabled(app):
    return getattr(app, "_conversation_transition_protocol", False) is True


def _closing(app):
    app._gui_quitting = True
    app._conversation_transition_shutdown_requested = True


async def _settle(app):
    pending = getattr(app, "_conversation_transition_pending", False)
    action = getattr(app, "_conversation_transition_task", None)
    identity = copy.deepcopy(getattr(app, "_conversation_transition_shutdown_identity", None))
    if pending:
        if not isinstance(action, asyncio.Task) or action is asyncio.current_task():
            raise RuntimeError("Conversation action has no joinable shutdown rendezvous")
        if not action.done():
            action.cancel()
        try:
            await join_task(action)
        except Exception as exc:  # noqa: BLE001 - action failure is separate from worker settlement
            from litetui import runtime_log
            runtime_log.record_error("conversation_action_failed_during_shutdown", detail=type(exc).__name__)
    if getattr(app, "_conversation_transition_pending", False):
        raise RuntimeError("Conversation action remains pending after its shutdown rendezvous")
    if getattr(app, "_conversation_transition_cleanup_error", None):
        raise RuntimeError("Conversation action retains unproven process cleanup")
    # These global handles belong to this transition only while it was pending.
    # A later ordinary turn can have its own hooks/cleanup; normal app teardown
    # remains their owner, even after a previously completed New or Rename.
    if pending:
        lock = getattr(app, "_hook_lock", None)
        if getattr(app, "_hook_lifecycle", ()) or lock is not None and lock.locked():
            raise RuntimeError("Conversation lifecycle hooks did not settle")
        closing = getattr(app, "_claude_closing", None)
        handles = closing if isinstance(closing, list) else ([] if closing is None else [closing])
        for handle in handles:
            if (not isinstance(handle, asyncio.Task) or not handle.done()
                    or handle.cancelled() or handle.exception() is not None):
                raise RuntimeError("Conversation provider cleanup did not settle successfully")
        if not isinstance(identity, dict):
            raise RuntimeError("Pending conversation action lost its reviewed identity")
    if identity is not None and (not isinstance(identity, dict)
            or any(not isinstance(identity.get(key), str) or not identity[key] for key in IDENTITY)):
        raise RuntimeError("Conversation action has invalid shutdown identity")
    return {"version": 1, "worker_settled": True, "lifecycle_settled": True,
            "native_cleanup_settled": True, "effects_verified": False,
            "pending_at_close": bool(pending),
            **{key: identity[key] if identity is not None else None for key in IDENTITY},
            "current_session_id": app.convo_id}


async def settle_before_shutdown(app):
    """Single-flight join shared by RPC, pipe EOF and app teardown."""
    if not _enabled(app):
        return None
    _closing(app)
    if (getattr(app, "_conversation_transition_pending", False)
            and getattr(app, "_conversation_transition_task", None) is asyncio.current_task()):
        raise RuntimeError("A conversation action cannot join itself during shutdown")
    task = getattr(app, "_conversation_shutdown_settlement_task", None)
    if task is None:
        task = asyncio.create_task(_settle(app))
        app._conversation_shutdown_settlement_task = task
    report = await join_task(task)
    if report is None:
        raise RuntimeError("Conversation shutdown was cancelled without settlement proof")
    app._conversation_shutdown_report = copy.deepcopy(report)
    return copy.deepcopy(report)


async def _finish_exit(app):
    result = None
    try:
        report = await settle_before_shutdown(app)
        if report is None:
            raise RuntimeError("Conversation shutdown protocol disappeared")
        # An engine session can inspect disabled conversation actions. Preserve
        # its negotiated engine proof as well as this read-only capability.
        from litetui import native_engine_shutdown
        engine = await native_engine_shutdown.settle_before_shutdown(app)
        if engine is not None:
            proof = engine["shutdown"]
            if (engine["worker_settled"] is not True or proof["retained"] is not False
                    or proof["owned"] is not False and proof["main_exited"] is not True):
                raise RuntimeError("Engine cleanup remains unproven")
        result = {"ok": True, "conversation_shutdown": report}
        if engine is not None:
            result["engine_shutdown"] = engine
        error = None
    except Exception as exc:  # noqa: BLE001 - failed join cannot acknowledge cleanup or release ownership
        error = "Conversation shutdown is unproven: " + type(exc).__name__
    callbacks = getattr(app, "_conversation_shutdown_callbacks", [])
    app._conversation_shutdown_callbacks = []
    for callback in callbacks:
        callback(ok=result is not None, result=result, error=error)
    if result is not None:
        app.exit()
    else:
        from litetui import runtime_log
        runtime_log.record_error("conversation_shutdown_unproven", detail=error or "Unknown")


def request_shutdown(app, respond=None):
    """Legacy runtimes return False; negotiated ones exit only after joining."""
    if not _enabled(app):
        return False
    _closing(app)
    callbacks = getattr(app, "_conversation_shutdown_callbacks", None)
    if callbacks is None:
        callbacks = app._conversation_shutdown_callbacks = []
    if respond is not None:
        callbacks.append(respond)
    task = getattr(app, "_conversation_shutdown_exit_task", None)
    if task is None or task.done():
        app._conversation_shutdown_exit_task = asyncio.create_task(_finish_exit(app))
    return True
