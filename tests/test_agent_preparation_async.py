import asyncio
import threading
import pytest


@pytest.mark.asyncio
async def test_preparation_does_not_block_event_loop():
    from litetui.agent_preparation import await_preparation
    entered, release = threading.Event(), threading.Event()
    def prepare():
        entered.set()
        assert release.wait(3)
        return 'prepared'
    task = asyncio.create_task(await_preparation(prepare))
    try:
        for _ in range(100):
            if entered.is_set(): break
            await asyncio.sleep(.01)
        assert entered.is_set()
        assert not task.done()
    finally:
        release.set()
    assert await task == 'prepared'


@pytest.mark.asyncio
async def test_repeated_cancel_joins_preparation_before_return():
    from litetui.agent_preparation import await_preparation
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def prepare():
        entered.set()
        release.wait(3)
        finished.set()
        return 'prepared'
    task = asyncio.create_task(await_preparation(prepare))
    for _ in range(100):
        if entered.is_set(): break
        await asyncio.sleep(.01)
    assert entered.is_set()
    task.cancel()
    await asyncio.sleep(.02)
    task.cancel()
    await asyncio.sleep(.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_chat_switch_during_preparation_prevents_process_start(tmp_path):
    from types import SimpleNamespace
    from litetui.agent_app_runtime import run_for_app
    from litetui.agent_registry import AgentRegistry
    from litetui.agent_launcher import LaunchBlocked
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    entered, release = threading.Event(), threading.Event()
    app = SimpleNamespace(convo_id='original', store=SimpleNamespace(
        convo_id='original', owned=True, pending=False, loading=False),
        _start_child_delivery=lambda **kw: None)
    def prepare():
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(workspace=tmp_path, data_root=tmp_path, branch=None)
    task = asyncio.create_task(run_for_app(app, None, None, registry=registry,
        inbox=None, receipts=None, parent='parent', child_id='child', workspace=None,
        data_root=None, branch=None, evidence=[], supported_levels=[], prepare=prepare))
    try:
        for _ in range(100):
            if entered.is_set(): break
            await asyncio.sleep(.01)
        assert entered.is_set()
        app.convo_id = 'switched'
    finally:
        release.set()
    with pytest.raises(LaunchBlocked, match='ownership changed'): await task
    assert registry.active('parent')[0]['state'] == 'claimed'