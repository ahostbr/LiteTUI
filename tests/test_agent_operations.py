import asyncio
import pytest
from litetui.agent_operations import AgentOperations
from litetui.agent_launcher import LaunchBlocked


@pytest.mark.asyncio
async def test_operation_result_and_failure_remain_queryable():
    operations = AgentOperations()
    async def success(): return {'completion_id': 'completion'}
    async def fail(): raise OSError('launch failed')
    good, bad = operations.start(success), operations.start(fail)
    await asyncio.sleep(0)
    assert operations.status(good)['result'] == {'completion_id': 'completion'}
    assert operations.status(bad)['status'] == 'failed'
    assert 'launch failed' in operations.status(bad)['error']
    with pytest.raises(LaunchBlocked): operations.status('unknown')
    await operations.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_owned_cleanup():
    operations = AgentOperations()
    entered, closed = asyncio.Event(), asyncio.Event()
    async def launch():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            closed.set()
    ident = operations.start(launch)
    await entered.wait()
    assert operations.status(ident)['status'] == 'running'
    assert (await operations.cancel(ident))['status'] == 'cancelled'
    assert closed.is_set()
    await operations.close()
    with pytest.raises(LaunchBlocked): operations.start(launch)


@pytest.mark.asyncio
async def test_shutdown_cancels_and_joins_all_operations():
    operations = AgentOperations()
    closed = []
    async def launch():
        try: await asyncio.Event().wait()
        finally: closed.append(True)
    ids = [operations.start(launch) for _ in range(3)]
    await asyncio.sleep(0)
    await operations.close()
    assert len(closed) == 3
    assert all(operations.status(ident)['status'] == 'cancelled' for ident in ids)


@pytest.mark.asyncio
async def test_app_shutdown_joins_children_before_backend_and_loop(monkeypatch):
    from types import SimpleNamespace
    from textual.app import App
    from litetui.app import LiteTUI
    calls = []
    async def close(): calls.append('children')
    async def settle(): calls.append('settle')
    async def shutdown(self): calls.append('textual')
    monkeypatch.setattr(App, '_shutdown', shutdown)
    app = SimpleNamespace(
        _agent_operations=SimpleNamespace(close=close),
        _child_delivery_timer=SimpleNamespace(stop=lambda: calls.append('timer')),
        _settle_before_teardown=settle,
        _backend=SimpleNamespace(name='codex', shutdown=lambda: calls.append('backend')))
    # super() requires an actual LiteTUI instance, but construction need not run
    # for this narrowly isolated shutdown ordering test.
    actual = object.__new__(LiteTUI)
    actual.__dict__.update(app.__dict__)
    await LiteTUI._shutdown(actual)
    assert actual._gui_quitting is True
    assert calls == ['timer', 'children', 'settle', 'backend', 'textual']