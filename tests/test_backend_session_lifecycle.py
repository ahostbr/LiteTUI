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


def test_session_ignores_stale_connection_and_turn_completion():
    from litetui.backend_session import BackendSession
    session = BackendSession()
    old = session.begin()
    current = session.begin()
    assert not session.ready(old)
    assert session.ready(current)
    session.start_turn('new-turn')
    assert not session.finish_turn('old-turn')
    assert session.active_turn == 'new-turn'
    assert session.finish_turn('new-turn')


def test_app_server_owns_independent_session_state():
    from litetui.backend_session import BackendSession
    assert isinstance(mod.AppServer().session, BackendSession)


@pytest.mark.asyncio
async def test_cleanup_runs_all_steps_and_preserves_primary_failure():
    from litetui.backend_session import cleanup_steps
    steps = []
    async def first():
        steps.append('first')
        raise RuntimeError('cleanup failed')
    async def second(): steps.append('second')
    errors = await cleanup_steps([first, second])
    assert steps == ['first', 'second']
    assert 'cleanup failed' in errors[0]


@pytest.mark.asyncio
async def test_cleanup_timeout_does_not_skip_remaining_steps():
    from litetui.backend_session import cleanup_steps
    completed = []
    async def stuck(): await asyncio.sleep(100)
    async def finish(): completed.append(True)
    errors = await cleanup_steps([stuck, finish], timeout=0.01)
    assert completed == [True]
    assert errors and 'TimeoutError' in errors[0]
