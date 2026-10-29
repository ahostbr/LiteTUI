import asyncio
from types import SimpleNamespace

import pytest

from litetui import gui_rpc


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [False, True])
async def test_gui_open_waits_for_history_and_rejects_cross_conversation_response(monkeypatch, switch):
    refreshed = asyncio.Event()
    waiting = asyncio.Event()
    async def connected():
        pass

    app = SimpleNamespace(conversation=[], backend=object(), convo_id="original",
                          model_id="saved-model", _connect_settled=True,
                          _gui_connection_success=True,
                          _resume_connect_worker=SimpleNamespace(wait=connected))
    requests = []

    async def wait():
        waiting.set()
        await refreshed.wait()
        if switch:
            app.conversation = []
            app.convo_id = "different"

    app._native_history_worker = SimpleNamespace(wait=wait)

    def dispatch(value, command):
        requests.append(command["type"])
        if command["type"] == "gui.conversations.open":
            return {"messages": ["stale"]}
        assert refreshed.is_set()
        return {"messages": ["reconciled"]}

    monkeypatch.setattr(gui_rpc, "dispatch", dispatch)
    task = asyncio.create_task(gui_rpc._async_dispatch(app, {"type": "gui.conversations.open"}))
    await asyncio.wait_for(waiting.wait(), 1)
    assert not task.done()
    refreshed.set()
    if switch:
        with pytest.raises(ValueError, match="Conversation changed"):
            await task
        assert requests == ["gui.conversations.open"]
    else:
        assert await task == {"messages": ["reconciled"], "connected": True}
        assert requests == ["gui.conversations.open", "gui.conversations.read"]


@pytest.mark.asyncio
async def test_non_native_open_keeps_existing_response(monkeypatch):
    async def connected():
        pass

    app = SimpleNamespace(conversation=[], backend=object(), convo_id="local",
                          model_id="saved-model", _connect_settled=True,
                          _gui_connection_success=True,
                          _resume_connect_worker=SimpleNamespace(wait=connected))
    monkeypatch.setattr(gui_rpc, "dispatch", lambda app, cmd: {"messages": ["local"]})
    assert await gui_rpc._async_dispatch(app, {"type": "gui.conversations.open"}) == {
        "messages": ["local"], "connected": True}
