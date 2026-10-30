"""Controlled shutdown ordering; no real engine process, hardware or network."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace as NS

import pytest

from litetui import native_engine_shutdown as shutdown
from litetui.agent_preparation import await_preparation
from litetui.llm_backend import TerminalShutdown


class Backend:
    name = 'ninfer'

    def __init__(self, effects, *, owned=True, retained=False, invalid=False):
        self.effects = effects
        self._owned = object() if owned else None
        self._starting = False
        self._stopping = False
        self.retained = retained
        self.invalid = invalid

    def begin_stop(self):
        assert not self._starting and not self._stopping
        self._stopping = True
        return True

    def end_stop(self):
        self._stopping = False

    def shutdown_owned(self):
        assert self._stopping and not self._starting
        assert self._owned is not None
        self.effects.append('owned-stop')
        if not self.retained:
            self._owned = None
        return TerminalShutdown(owned=True, attempted=True,
                                main_exited=not self.retained and not self.invalid,
                                pid=2_000_000_000, retained=self.retained)


def app_for(*, owned=True, retained=False, invalid=False):
    effects: list[str] = []
    backend = Backend(effects, owned=owned, retained=retained, invalid=invalid)
    current = NS(backend=backend, pending=False, action_task=None,
                 cancel_start=threading.Event(), shutdown_requested=False,
                 instance='a' * 64, session_id='11111111-1111-4111-8111-111111111111',
                 admission='prompt_ready', effective_model='selected')
    app = NS(_native_engine_state=current, _gui_connection_success=True,
             exit=lambda: effects.append('exit'))
    return app, current, effects


@pytest.fixture(autouse=True)
def capture_shutdown_diagnostics(monkeypatch):
    from litetui import runtime_log
    monkeypatch.setattr(runtime_log, 'record_error', lambda *_args, **_kwargs: None)


def test_legacy_runtime_keeps_its_existing_shutdown_owner():
    app = NS()
    assert shutdown.request_shutdown(app) is False
    assert asyncio.run(shutdown.settle_before_shutdown(app)) is None
    assert vars(app) == {}


@pytest.mark.parametrize('owned', [True, False])
def test_ack_follows_proof_and_concurrent_requests_share_cleanup(owned):
    async def run():
        app, current, effects = app_for(owned=owned)
        replies = []

        def respond(**reply):
            effects.append('ack')
            replies.append(reply)

        assert shutdown.request_shutdown(app, respond)
        assert shutdown.request_shutdown(app, respond)
        assert current.shutdown_requested and app._gui_quitting
        assert not effects
        await app._native_engine_exit_task
        assert effects == (['owned-stop'] if owned else []) + ['ack', 'ack', 'exit']
        assert all(reply['ok'] is True for reply in replies)
        proof = replies[0]['result']['engine_shutdown']
        assert proof['worker_settled'] is True and proof['resource_release_verified'] is False
        assert proof['shutdown']['owned'] is owned and proof['shutdown']['retained'] is False
        assert current.admission == 'management_only' and current.effective_model is None
        assert app._gui_connection_success is False
        proof['shutdown']['retained'] = True
        assert (await shutdown.settle_before_shutdown(app))['shutdown']['retained'] is False
        assert effects.count('owned-stop') == int(owned)

    asyncio.run(run())


def test_retained_cleanup_is_a_negative_ack_and_never_claims_release():
    async def run():
        app, current, effects = app_for(retained=True)
        replies = []
        shutdown.request_shutdown(app, lambda **reply: replies.append(reply))
        await app._native_engine_exit_task
        assert replies[0]['ok'] is False and replies[0]['result'] is None
        assert 'unconfirmed' in replies[0]['error']
        assert current.backend._owned is not None and current.admission == 'retained_recovery'
        assert effects == ['owned-stop', 'exit']
        report = await shutdown.settle_before_shutdown(app)
        assert report['shutdown']['retained'] is True and report['resource_release_verified'] is False
        assert effects.count('owned-stop') == 1

    asyncio.run(run())


@pytest.mark.parametrize('fault', ['missing-task', 'contradictory-proof', 'unsettled-claim'])
def test_unproven_worker_or_proof_never_acknowledges_clean_exit(fault):
    async def run():
        app, current, effects = app_for(invalid=fault == 'contradictory-proof')
        if fault == 'missing-task':
            current.pending = True
        if fault == 'unsettled-claim':
            current.backend._starting = True
        replies = []
        shutdown.request_shutdown(app, lambda **reply: replies.append(reply))
        await app._native_engine_exit_task
        assert replies[0]['ok'] is False and replies[0]['result'] is None
        assert 'unproven' in replies[0]['error']
        assert 'exit' not in effects
        if fault != 'contradictory-proof':
            assert 'owned-stop' not in effects and current.backend._owned is not None

    asyncio.run(run())


def test_completed_serial_caller_task_is_not_cancelled_or_joined():
    async def run():
        app, current, effects = app_for()
        current.action_task = asyncio.current_task()
        report = await shutdown.settle_before_shutdown(app)
        assert report['worker_settled'] is True and effects == ['owned-stop']
        assert not current.action_task.cancelling()

    asyncio.run(run())


def test_pending_action_cannot_join_itself_or_stop_before_settling():
    async def run():
        app, current, effects = app_for()
        current.pending = True
        current.action_task = asyncio.current_task()
        with pytest.raises(RuntimeError, match='cannot join itself'):
            await shutdown.settle_before_shutdown(app)
        assert effects == [] and current.backend._owned is not None

    asyncio.run(run())


@pytest.mark.parametrize('cancel_waiter', [False, True])
def test_cancelled_start_publishes_late_handle_before_one_owned_stop(cancel_waiter):
    async def run():
        app, current, effects = app_for(owned=False)
        entered, release = threading.Event(), threading.Event()

        def spawn():
            entered.set()
            assert release.wait(3), 'test must release its controlled worker'
            current.backend._owned = object()
            effects.append('late-publish')

        async def action():
            current.pending = True
            current.backend._starting = True
            try:
                await await_preparation(spawn)
            except asyncio.CancelledError:
                effects.append('action-cancelled-after-join')
            finally:
                current.backend._starting = False
                current.pending = False

        current.action_task = asyncio.create_task(action())
        assert await asyncio.to_thread(entered.wait, 2)
        waiter = asyncio.create_task(shutdown.settle_before_shutdown(app))
        sibling = asyncio.create_task(shutdown.settle_before_shutdown(app))
        for _ in range(100):
            if current.cancel_start.is_set():
                break
            await asyncio.sleep(0)
        assert current.cancel_start.is_set() and current.shutdown_requested
        assert not waiter.done() and 'owned-stop' not in effects
        if cancel_waiter:
            waiter.cancel()
            await asyncio.sleep(0)
            assert not waiter.done()
        release.set()
        first, second = await asyncio.gather(waiter, sibling)
        assert first == second and first['worker_settled'] is True
        assert effects == ['late-publish', 'action-cancelled-after-join', 'owned-stop']
        assert current.backend._owned is None and not current.pending

    asyncio.run(run())
