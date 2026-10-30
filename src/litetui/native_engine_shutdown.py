"""Join native engine actions before owned cleanup and RPC shutdown acknowledgement.

The supervisor keeps its existing hard shutdown deadline and whole-job containment.
A timed out join or retained handle is never converted to successful cleanup here.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict

from litetui.agent_preparation import await_preparation
from litetui.llm_backend import TerminalShutdown
from litetui.shutdown_wait import join_task as _join


async def _settle(app, current):
    signal = getattr(current, 'cancel_start', None)
    if signal is not None:
        signal.set()
    action = getattr(current, 'action_task', None)
    if current.pending:
        if not isinstance(action, asyncio.Task) or action is asyncio.current_task():
            raise RuntimeError('Engine action has no joinable shutdown rendezvous')
        if not action.done():
            action.cancel()
        try:
            await _join(action)
        except Exception as exc:  # noqa: BLE001 - join failed action before retained-handle cleanup
            # An action exception does not invalidate its retained process handle.
            # Verify the lifecycle claims below before attempting owned cleanup.
            from litetui import runtime_log
            runtime_log.record_error('native_engine_action_failed_during_shutdown', detail=type(exc).__name__)
    backend = current.backend
    if current.pending or backend._starting or backend._stopping:
        raise RuntimeError('Engine action did not settle before shutdown')
    proof = TerminalShutdown(owned=False)
    if backend._owned is not None:
        if not backend.begin_stop():
            raise RuntimeError('Engine shutdown could not claim its retained handle')
        try:
            proof = await await_preparation(backend.shutdown_owned)
        finally:
            backend.end_stop()
    if not isinstance(proof, TerminalShutdown):
        raise TypeError('Engine shutdown returned no canonical terminal proof')
    if (current.pending or backend._starting or backend._stopping
            or proof.retained != (backend._owned is not None)
            or proof.owned and not proof.retained and not proof.main_exited):
        raise RuntimeError('Engine shutdown proof contradicts retained lifecycle state')
    if proof.retained:
        from litetui import runtime_log
        runtime_log.record_error('native_engine_shutdown_retained', detail='Owned engine handle remains unconfirmed')
    current.admission = 'retained_recovery' if proof.retained else 'management_only'
    current.effective_model = None
    app._gui_connection_success = False
    return {'version': 1, 'management_instance': current.instance,
            'session_id': current.session_id, 'backend': backend.name,
            'worker_settled': True, 'shutdown': asdict(proof),
            'resource_release_verified': False}


async def settle_before_shutdown(app):
    """Single-flight settlement shared by RPC, EOF and Textual teardown."""
    current = getattr(app, '_native_engine_state', None)
    if current is None:
        return None
    current.shutdown_requested = True
    app._gui_quitting = True
    action = getattr(current, 'action_task', None)
    if current.pending and action is asyncio.current_task():
        raise RuntimeError('An engine action cannot join itself during shutdown')
    task = getattr(app, '_native_engine_settlement_task', None)
    if task is None:
        task = asyncio.create_task(_settle(app, current))
        app._native_engine_settlement_task = task
    report = await _join(task)
    if report is None:
        raise RuntimeError('Engine shutdown settlement was cancelled without a result')
    app._native_engine_shutdown_report = copy.deepcopy(report)
    return copy.deepcopy(report)


def _clean(report):
    proof = report['shutdown']
    return (report['worker_settled'] is True and proof['retained'] is False
            and (proof['owned'] is False or proof['main_exited'] is True))


async def _finish_exit(app):
    report = None
    try:
        report = await settle_before_shutdown(app)
        clean = report is not None and _clean(report)
        error = None if clean else 'Engine shutdown retained an unconfirmed owned handle'
    except Exception as exc:  # noqa: BLE001 - retain uncertainty, never acknowledge a failed join
        clean = False
        error = 'Engine shutdown is unproven: ' + type(exc).__name__
    callbacks = getattr(app, '_native_engine_shutdown_callbacks', [])
    app._native_engine_shutdown_callbacks = []
    for callback in callbacks:
        callback(ok=clean, result={'ok': True, 'engine_shutdown': report} if clean else None,
                 error=error)
    if not clean:
        from litetui import runtime_log
        runtime_log.record_error('native_engine_shutdown_unproven', detail=error or 'Unknown')
    # A settled but retained handle still reaches normal final cleanup/atexit.
    # An unjoined worker must retain the live app until the supervisor's hard bound.
    if report is not None and report['worker_settled'] is True:
        app.exit()


def request_shutdown(app, respond=None):
    """Return False for legacy runtimes; otherwise acknowledge only after joining."""
    if getattr(app, '_native_engine_state', None) is None:
        return False
    current = app._native_engine_state
    current.shutdown_requested = True
    app._gui_quitting = True
    callbacks = getattr(app, '_native_engine_shutdown_callbacks', None)
    if callbacks is None:
        callbacks = app._native_engine_shutdown_callbacks = []
    if respond is not None:
        callbacks.append(respond)
    task = getattr(app, '_native_engine_exit_task', None)
    if task is None or task.done():
        app._native_engine_exit_task = asyncio.create_task(_finish_exit(app))
    return True
