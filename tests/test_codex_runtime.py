import asyncio
import json
from types import SimpleNamespace

import pytest

from litetui.codex_app_server import AppServer
from litetui.codex_runtime import RuntimeActivity, restart_readiness


def command(method, *, thread="parent", turn="turn", item="call", **values):
    return {"method": method, "params": {"threadId": thread, "turnId": turn,
            "item": {"id": item, "type": "commandExecution", **values}}}


def test_turn_end_and_item_completion_do_not_claim_background_process_exited():
    state = RuntimeActivity()
    state.observe(command("item/started", processId="opaque"))
    state.observe(command("item/completed", status="completed", processId="opaque", exitCode=None))
    state.observe({"method": "turn/completed", "params": {"threadId": "parent"}})
    assert state.commands == {("parent", "turn", "call"): "opaque"}
    state.observe(command("item/completed", thread="child", item="wait", processId="opaque", exitCode=0))
    assert state.commands  # Identical process strings in another thread are unrelated.
    state.observe(command("item/completed", item="wait", processId="opaque", exitCode=0))
    assert not state.commands
    state.observe(command("item/started", processId="opaque"))
    assert not state.commands  # Late replay cannot resurrect completed work.


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["idle", "active_child", "process", "request", "race", "cursor", "identity", "disconnect"])
async def test_restart_readiness_requires_all_loaded_threads_idle_and_no_pending_work(case):
    state = RuntimeActivity()
    calls = []

    async def request(method, params):
        calls.append((method, params))
        assert method in ("thread/loaded/list", "thread/read")
        if method == "thread/loaded/list":
            if case == "cursor":
                return {"data": ["parent"], "nextCursor": "same"}
            return ({"data": ["parent"], "nextCursor": "next"} if "cursor" not in params
                    else {"data": ["child"]})
        if case == "race":
            state.observe({"method": "turn/started", "params": {"threadId": "parent"}})
        return {"thread": {"id": "wrong" if case == "identity" else params["threadId"],
                           "status": {"type": "active" if case == "active_child" and params["threadId"] == "child" else "idle"}}}

    if case == "process":
        state.observe(command("item/started"))
    if case == "request":
        state.observe({"id": 1, "method": "item/tool/call"})
    if case == "disconnect":
        state.disconnected()
    result = await restart_readiness(SimpleNamespace(runtime_activity=state, request=request))
    assert result["ready"] is (case == "idle")
    if case in ("process", "request", "disconnect"):
        assert calls == []
    if case == "idle":
        assert {params["threadId"] for method, params in calls if method == "thread/read"} == {"parent", "child"}


@pytest.mark.asyncio
async def test_protocol_reader_tracks_late_activity_without_consuming_display_events():
    server = AppServer()
    stream = asyncio.StreamReader()
    events = [command("item/started", thread="child", processId="opaque"),
              {"method": "turn/completed", "params": {"threadId": "child"}},
              {"id": 71, "method": "item/tool/call", "params": {}}]
    stream.feed_data("".join(json.dumps(event) + "\n" for event in events).encode())
    stream.feed_eof()
    server.process = SimpleNamespace(stdout=stream)
    await server._read()
    assert [await server.events.get() for _ in events] == events
    assert server.runtime_activity.commands == {("child", "turn", "call"): "opaque"}
    assert server.runtime_activity.requests == {71}
    server.runtime_activity.replied(71)
    assert not server.runtime_activity.requests
    assert not server.runtime_activity.connected
