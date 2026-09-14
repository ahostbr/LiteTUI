import asyncio
from types import SimpleNamespace as NS

import pytest

from litetui.app import LiteTUI
from litetui.tool_events import native_lifecycle


@pytest.mark.asyncio
async def test_native_event_ownership_is_task_local_and_preserves_local_dispatch():
    events = []
    app = NS(tools_enabled=False, _rpc_emit=events.append)
    entered, other_finished = asyncio.Event(), asyncio.Event()

    async def native():
        with native_lifecycle():
            entered.set()
            await other_finished.wait()
            _, ok = await LiteTUI._execute_tool(app, "native", {})
            assert not ok

    async def local():
        await entered.wait()
        await LiteTUI._execute_tool(app, "local", {})
        other_finished.set()

    await asyncio.gather(native(), local())
    assert events == [{"type": "tool_call", "name": "local", "args": {}}]
    await LiteTUI._execute_tool(app, "after", {})
    assert events[-1]["name"] == "after"
