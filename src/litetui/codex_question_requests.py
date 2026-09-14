"""Turn-owned question workers keep the native event reader responsive."""

import asyncio
from threading import Event

from litetui.question_result import question_lifetime


class QuestionRequests:
    def __init__(self, transport):
        self.transport = transport
        self.pending = {}
        self.seen = set()
        self.closed = False

    def dispatch(self, message):
        if message.get("method") != "item/tool/requestUserInput":
            return False
        ident = message["id"]
        if self.closed or ident in self.seen:
            return True
        self.seen.add(ident)
        cancelled = Event()
        params = message.get("params", {})
        stale = any(
            params.get(key) not in (None, expected)
            for key, expected in (
                ("threadId", self.transport.thread_id),
                ("turnId", self.transport.turn_id),
            )
            if expected is not None
        )

        async def run():
            try:
                with question_lifetime(cancelled):
                    if not cancelled.is_set():
                        if stale:
                            await self.transport.server.send(
                                {"id": ident, "result": {"answers": {}}}
                            )
                        else:
                            await self.transport._server_request(
                                message, interrupt_on_stop=False
                            )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - forward callback failure to its reader
                if not cancelled.is_set():
                    await self.transport.server.events.put(error)

        app = self.transport.app
        worker = None
        if app is not None and hasattr(app, "run_worker"):
            worker = app.run_worker(
                run(), group="codex-questions", exclusive=False, exit_on_error=False
            )

            async def wait():
                try:
                    await worker.wait()
                except Exception as error:  # noqa: BLE001 - worker failure must reach the reader
                    if not cancelled.is_set():
                        await self.transport.server.events.put(error)

            task = asyncio.create_task(wait())
        else:
            task = asyncio.create_task(run())
        self.pending[ident] = (cancelled, task, worker)
        return True

    async def close(self):
        self.closed = True
        pending, self.pending = self.pending, {}
        for cancelled, _, _ in pending.values():
            cancelled.set()
        tasks = [task for _, task, _ in pending.values()]
        if not tasks:
            return
        # Release the shared tool's thread Event before cancelling its awaiter.
        _, unfinished = await asyncio.wait(tasks, timeout=1)
        for _, task, worker in pending.values():
            if task in unfinished:
                if worker is not None:
                    worker.cancel()
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
