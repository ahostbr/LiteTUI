import asyncio
import json
import threading
import pytest
from textual.app import App
from litetui.agent_tool_bridge import make_runner


@pytest.mark.asyncio
async def test_real_textual_thread_dispatch_and_status():
    app = App()
    started, release = asyncio.Event(), asyncio.Event()
    owner = threading.get_ident()
    async def launch(args):
        assert threading.get_ident() == owner
        started.set()
        await release.wait()
        return {'completion_id': 'completed', 'prompt': args['prompt']}
    runner = make_runner(app, launch=launch)
    async with app.run_test() as pilot:
        accepted = json.loads(await asyncio.to_thread(runner, {'prompt': 'task'}))
        await started.wait()
        assert accepted['status'] == 'accepted'
        args = {'action': 'status', 'operation_id': accepted['operation_id']}
        assert json.loads(await asyncio.to_thread(runner, args))['status'] == 'running'
        release.set()
        await pilot.pause(0.05)
        result = json.loads(await asyncio.to_thread(runner, args))
        assert result['result']['completion_id'] == 'completed'
        await app._agent_operations.close()


@pytest.mark.asyncio
async def test_tool_cancel_waits_for_cleanup_on_app_loop():
    app = App()
    started, cleaned = asyncio.Event(), asyncio.Event()
    async def launch(args):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            cleaned.set()
    runner = make_runner(app, launch=launch)
    async with app.run_test():
        result = json.loads(await asyncio.to_thread(runner, {'prompt': 'task'}))
        await started.wait()
        cancelled = json.loads(await asyncio.to_thread(runner, {
            'action': 'cancel', 'operation_id': result['operation_id']}))
        assert cancelled['status'] == 'cancelled'
        assert cleaned.is_set()
        await app._agent_operations.close()
