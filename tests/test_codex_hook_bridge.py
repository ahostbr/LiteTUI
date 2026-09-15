import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from litetui.codex_hook_bridge import NativeHookBridge


@pytest.mark.asyncio
async def test_hook_channel_authentication_and_policy_failure_are_closed():
    invoked = []

    async def handle(event):
        invoked.append(event)
        raise RuntimeError("synthetic sensitive failure")

    bridge = NativeHookBridge(None)
    bridge.policy = NS(handle=handle)
    bridge.listener = await asyncio.start_server(bridge.accept, "127.0.0.1", 0)
    port = bridge.listener.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(json.dumps({"token": "wrong", "event": {}}).encode() + b"\n")
        await writer.drain()
        result = json.loads(await reader.readline())
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert invoked == []
        writer.close()
        await writer.wait_closed()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(json.dumps({"token": bridge.token, "event": {}}).encode() + b"\n")
        await writer.drain()
        assert await reader.readline() == b""
        assert invoked == [{}]
        writer.close()
        await writer.wait_closed()
    finally:
        bridge.close()
        await bridge.listener.wait_closed()


@pytest.mark.asyncio
async def test_native_policy_can_await_a_textual_approval_dialog():
    from textual.app import App
    from textual.screen import ModalScreen

    class Approval(ModalScreen):
        def on_mount(self):
            self.call_after_refresh(self.answer)

        def answer(self):
            self.dismiss(True)

    class Host(App):
        tools_enabled = True
        _stop_requested = False
        settings = NS(tools_disabled=[])
        plugins = NS(policy_for=lambda name: None)

        async def _authorize_action(self, name, args, policy, *, workspace=None):
            assert workspace is not None
            assert await self.push_screen_wait(Approval())

    app = Host()
    async with app.run_test():
        bridge = NativeHookBridge(app)
        bridge.listener = await asyncio.start_server(bridge.accept, "127.0.0.1", 0)
        try:
            port = bridge.listener.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                json.dumps(
                    {
                        "token": bridge.token,
                        "event": {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
            assert json.loads(await asyncio.wait_for(reader.readline(), 3)) == {}
            writer.close()
            await writer.wait_closed()
        finally:
            bridge.close()
            await bridge.listener.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stop", "disconnect", "close"])
async def test_waiting_native_policy_is_cancelled(reason):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handle(event):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    app = NS(_stop_requested=False)
    bridge = NativeHookBridge(app)
    bridge.policy = NS(handle=handle)
    bridge.listener = await asyncio.start_server(bridge.accept, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", bridge.listener.sockets[0].getsockname()[1]
    )
    try:
        writer.write(json.dumps({"token": bridge.token, "event": {}}).encode() + b"\n")
        await writer.drain()
        await asyncio.wait_for(started.wait(), 1)
        if reason == "stop":
            app._stop_requested = True
            result = json.loads(await asyncio.wait_for(reader.readline(), 1))
            assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        elif reason == "disconnect":
            writer.close()
        else:
            bridge.close()
        await asyncio.wait_for(cancelled.wait(), 1)
    finally:
        writer.close()
        await writer.wait_closed()
        bridge.close()
        await bridge.listener.wait_closed()
