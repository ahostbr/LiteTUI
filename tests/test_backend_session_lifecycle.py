"""Disconnected live processes are not initialized provider sessions."""
from types import SimpleNamespace
import asyncio
import pytest
from litetui import codex_app_server as mod


@pytest.mark.asyncio
async def test_live_process_with_dead_reader_is_not_reused(monkeypatch):
    server = mod.AppServer()
    server.process = SimpleNamespace(returncode=None)
    server.reader = asyncio.create_task(asyncio.sleep(0))
    await server.reader
    monkeypatch.setattr(mod.shutil, 'which', lambda name: None)
    with pytest.raises(mod.ProviderError):
        await server.start()


@pytest.mark.asyncio
async def test_failed_initialize_closes_owned_process_and_preserves_error(monkeypatch):
    server = mod.AppServer()
    closed = []
    process = SimpleNamespace(returncode=None, stdin=SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(mod.shutil, 'which', lambda name: 'codex.exe')
    async def spawn(*args, **kwargs): return process
    async def reader(): await asyncio.sleep(100)
    async def request(*args): raise mod.ProviderError('original initialize failure')
    async def close(p): p.returncode = 0
    monkeypatch.setattr(mod.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(server, '_read', reader)
    monkeypatch.setattr(server, 'request', request)
    monkeypatch.setattr(server, '_close_process', close)
    try:
        with pytest.raises(mod.ProviderError, match='original initialize failure'):
            await server.start()
        assert closed
        assert process.returncode == 0
        assert not server.initialized
    finally:
        if server.reader and not server.reader.done():
            server.reader.cancel()
            try: await server.reader
            except asyncio.CancelledError: pass


@pytest.mark.asyncio
async def test_initialized_live_reader_can_be_reused():
    server = mod.AppServer()
    server.process = SimpleNamespace(returncode=None)
    server.initialized = True
    server.reader = asyncio.create_task(asyncio.sleep(100))
    try:
        await server.start()
        assert server.initialized
    finally:
        server.reader.cancel()
        try: await server.reader
        except asyncio.CancelledError: pass
