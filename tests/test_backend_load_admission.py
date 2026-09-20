import asyncio
import pytest
from litetui.llm_backend import _VramGate, VramRefused


@pytest.mark.asyncio
async def test_concurrent_call_does_not_bypass_pending_gate():
    backend = _VramGate()
    entered = asyncio.Event()
    release = asyncio.Event()
    called, loaded = [], []
    async def gate(key):
        called.append(key)
        entered.set()
        await release.wait()
        return False
    backend.vram_gate = gate
    async def load(key):
        try:
            async with backend.vram_guard(key): loaded.append(key)
        except VramRefused: pass
    first = asyncio.create_task(load('a'))
    await entered.wait()
    second = asyncio.create_task(load('b'))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert loaded == []
    assert sorted(called) == ['a', 'b']


@pytest.mark.asyncio
async def test_nested_same_task_asks_once_and_cancel_releases_gate():
    backend = _VramGate()
    called = []
    async def gate(key): called.append(key); return True
    backend.vram_gate = gate
    async with backend.vram_guard('a'):
        async with backend.vram_guard('a'):
            assert called == ['a']
    entered = asyncio.Event()
    async def blocked(key): entered.set(); await asyncio.sleep(100)
    backend.vram_gate = blocked
    async def run():
        async with backend.vram_guard('b'): pass
    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    backend.vram_gate = gate
    async with backend.vram_guard('c'): pass
    assert called == ['a', 'c']
