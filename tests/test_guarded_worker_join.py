"""Real Textual worker lifecycle with fake blocking callbacks only."""
import asyncio
import threading

import pytest
from textual.app import App

from litetui.agent_preparation import await_preparation, run_guarded


@pytest.mark.asyncio
async def test_real_worker_retains_claim_through_repeated_cancel_join():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    cleaned = asyncio.Event()
    observations = []

    def blocking():
        started.set()
        if not release.wait(5):
            raise TimeoutError('test release missing')
        finished.set()

    async def op():
        await await_preparation(blocking)

    def cleanup():
        observations.append(finished.is_set())
        cleaned.set()

    async with App().run_test() as pilot:
        worker = run_guarded(pilot.app, op(), group='fake-stop', cleanup=cleanup)
        try:
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(.001)
            assert worker in list(pilot.app.workers)
            for _ in range(3):
                worker.cancel()
                await asyncio.sleep(0)
                assert not cleaned.is_set()
                assert not worker._task.done()
        finally:
            release.set()
        await asyncio.wait_for(cleaned.wait(), 2)
        assert observations == [True]
        assert worker._task.done()


@pytest.mark.asyncio
async def test_real_worker_cancel_before_start_closes_original():
    import inspect
    ran = []
    clean = asyncio.Event()
    async def op():
        ran.append(True)
    original = op()
    async with App().run_test() as pilot:
        worker = run_guarded(pilot.app, original, group='fake-stop', cleanup=clean.set)
        worker.cancel()
        await asyncio.wait_for(clean.wait(), 2)
        assert not ran
        assert inspect.getcoroutinestate(original) == inspect.CORO_CLOSED
