"""App-loop operation ownership for full child launches and cancellation."""
import asyncio
from uuid import uuid4
from litetui.agent_launcher import LaunchBlocked


class AgentOperations:
    def __init__(self):
        self.tasks = {}
        self.closing = False

    def start(self, factory):
        """Factory runs on the owning App loop, not the tool executor thread."""
        if self.closing:
            raise LaunchBlocked('Child operation manager is closing')
        ident = uuid4().hex
        task = asyncio.create_task(factory(), name='full-child-' + ident)
        self.tasks[ident] = task
        # Retrieve exceptions even when no status caller polls. The task retains
        # the exception for result(); no silent background exception warnings.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return ident

    def status(self, ident):
        task = self.tasks.get(ident)
        if task is None:
            raise LaunchBlocked('Unknown child operation in this parent process')
        if task.cancelled():
            return {'operation_id': ident, 'status': 'cancelled'}
        if not task.done():
            return {'operation_id': ident, 'status': 'running'}
        if task.exception() is not None:
            return {'operation_id': ident, 'status': 'failed', 'error': str(task.exception())}
        return {'operation_id': ident, 'status': 'finished', 'result': task.result()}

    async def cancel(self, ident):
        task = self.tasks.get(ident)
        if task is None:
            raise LaunchBlocked('Unknown child operation in this parent process')
        if not task.done():
            task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        except Exception:
            pass  # status exposes failure; do not turn it into cancelled success
        return self.status(ident)

    async def close(self):
        self.closing = True
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        # Runtime's bounded cleanup retains its own cancellation shielding.
        # Caller awaits this before destroying the App loop.
        await asyncio.gather(*tasks, return_exceptions=True)
