"""Completed foreground callbacks release results; accepted lifetimes remain joined."""
from __future__ import annotations

import asyncio
import gc
import threading
import weakref

import pytest
from test_rust_background_input import app as _owned_app
from test_rust_background_input import armed as _armed
from test_rust_background_input import switch_app as _switch_app

from litetui import background_input as work

app = _owned_app
armed = _armed
switch_app = _switch_app


class Result:
    def __init__(self):
        self.payload = bytes(128 * 1024)


async def reached(event):
    async def wait():
        while not event.is_set():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 5)


@pytest.mark.asyncio
async def test_more_than_capacity_sequential_foreground_callbacks_release_large_results(armed):
    values = []
    for _ in range(work.LIMITS["producers"] + 44):
        invoke, call = work.capture_tool(armed, Result)
        result = await work.track_tool(call, asyncio.to_thread(invoke))
        values.append(weakref.ref(result))
        await work.finish_tool(call)
        del result, call, invoke
    await asyncio.sleep(0)
    gc.collect()
    assert not any(reference() is not None for reference in values), "retired tool results remain owned by the bridge"
    assert not work.bridge(armed).calls
    assert not armed._rewrite_test_requests


@pytest.mark.asyncio
async def test_active_capacity_still_refuses_before_call_and_releases_unused_slots(armed):
    invoked = []
    calls = []
    for _ in range(work.LIMITS["producers"]):
        _, call = work.capture_tool(armed, lambda: invoked.append(True))
        calls.append(call)
    with pytest.raises(ValueError, match="capacity"):
        work.capture_tool(armed, lambda: invoked.append(True))
    assert not invoked
    await work.finish_tool(calls.pop())
    _, replacement = work.capture_tool(armed, lambda: invoked.append(True))
    calls.append(replacement)
    for call in calls:
        await work.finish_tool(call)
    assert not invoked and not work.bridge(armed).calls


@pytest.mark.asyncio
async def test_handed_callback_keeps_exact_producer_ownership(armed):
    invoke, call = work.capture_tool(armed, Result)
    record = work.register_producer(armed, "host_task", "controlled-tool", owner_instance="fixture", call=call)
    assert record.call is call and call.handed
    worker = armed.run_worker(work.producer_runner(armed, record, work.track_tool(call, asyncio.to_thread(invoke))),
                              group="controlled-callback", exit_on_error=False)
    work.bind_producer_worker(armed, record, worker)
    await asyncio.wait_for(record.observer, 5)
    await work.finish_tool(call)
    assert record.call is call and any(retained is call for retained in work.bridge(armed).calls)
    assert isinstance(call.future.result(), Result) and call.retired.is_set()
    assert record.phase == "retired"



@pytest.mark.asyncio
async def test_shutdown_keeps_stable_iteration_and_joins_each_actual_callback(armed):
    release = threading.Event()
    started = [threading.Event(), threading.Event()]
    finished = [threading.Event(), threading.Event()]
    calls, running, finishing = [], [], []
    closing = None
    try:
        for start, end in zip(started, finished):
            def callback(start=start, end=end):
                start.set()
                assert release.wait(10)
                end.set()
                return "retired"
            invoke, call = work.capture_tool(armed, callback)
            calls.append(call)
            running.append(asyncio.create_task(work.track_tool(call, asyncio.to_thread(invoke))))
        await asyncio.gather(*(reached(event) for event in started))
        closing = asyncio.create_task(work.settle_before_shutdown(armed))
        await asyncio.sleep(0)
        finishing = [asyncio.create_task(work.finish_tool(call)) for call in calls]
        await asyncio.sleep(0.02)
        assert not closing.done() and not any(item.done() for item in finishing)
        assert not any(event.is_set() for event in finished)
        release.set()
        await asyncio.wait_for(asyncio.gather(*running, *finishing), 5)
        proof = await asyncio.wait_for(closing, 5)
        assert proof["callback_settled"] and proof["worker_settled"]
        assert all(event.is_set() for event in finished)
        assert work.bridge(armed).calls == calls
    finally:
        release.set()
        await asyncio.gather(*running, *finishing, *([closing] if closing is not None else []), return_exceptions=True)
