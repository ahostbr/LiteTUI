import asyncio
from types import SimpleNamespace as NS

import pytest

from litetui.codex_app_server import AppServerTransport, user_input


class Server:
    def __init__(self):
        self.process = object()
        self.events = asyncio.Queue()
        self.requests = []
        self.replies = []
        self.turn = 0
        self.finish = True

    async def start(self):
        pass

    async def close(self):
        pass

    def shutdown(self):
        pass

    async def send(self, message):
        self.replies.append(message)

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == "account/read":
            return {"account": {"type": "chatgpt"}}
        if method in ("thread/start", "thread/resume"):
            return {"thread": {"id": "thread-1"}}
        if method == "thread/compact/start":
            await self.events.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "compact-1", "status": "completed"},
                    },
                }
            )
            return {}
        if method == "turn/start":
            self.turn += 1
            turn = str(self.turn)
            await self.events.put(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "thread-1", "delta": "OK"},
                }
            )
            await self.events.put(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-1",
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 2000,
                                "outputTokens": 5,
                                "cachedInputTokens": 1800,
                                "totalTokens": 2005,
                            },
                            "total": {
                                "inputTokens": 2000 * self.turn,
                                "outputTokens": 5 * self.turn,
                                "cachedInputTokens": 1800 * self.turn,
                                "totalTokens": 2005 * self.turn,
                            },
                        },
                    },
                }
            )
            if self.finish:
                await self.events.put(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": turn, "status": "completed"},
                        },
                    }
                )
            return {"turn": {"id": turn}}
        return {}


@pytest.mark.asyncio
async def test_ultra_is_owned_by_codex_and_history_is_append_only():
    server = Server()
    transport = AppServerTransport(server)
    messages = [{"role": "user", "content": "First"}]
    first = await transport.create(
        model="gpt-6-astra", messages=messages, extra_body={"reasoning_effort": "ultra"}
    )
    assert first.choices[0].message.content == "OK"
    assert first.usage.cached_tokens == 1800
    assert first.usage.cache_write_tokens is None
    messages += [
        {
            "role": "assistant",
            "content": "OK",
            "provider_metadata": first.choices[0].message.provider_metadata,
        },
        {"role": "user", "content": "Second"},
    ]
    await transport.create(
        model="gpt-6-astra", messages=messages, extra_body={"reasoning_effort": "max"}
    )
    turns = [params for method, params in server.requests if method == "turn/start"]
    assert turns[0]["effort"] == "ultra"
    assert turns[1]["effort"] == "max"
    assert "additionalContext" not in turns[1]
    assert turns[1]["input"] == [
        {"type": "text", "text": "Second", "text_elements": []}
    ]
    assert (
        len([method for method, _ in server.requests if method == "thread/start"]) == 1
    )
    # A reopened GUI/runtime resumes the same Codex thread, never retransmits prior turns.
    reopened = AppServerTransport(server)
    await reopened.create(model="gpt-6-astra", messages=messages)
    assert any(method == "thread/resume" for method, _ in server.requests)


@pytest.mark.asyncio
async def test_close_interrupts_the_owned_turn():
    server = Server()
    server.finish = False
    stream = await AppServerTransport(server).create(
        model="gpt-6-astra",
        messages=[{"role": "user", "content": "Hello"}],
        stream=True,
    )
    iterator = stream.__aiter__()
    await anext(iterator)
    await stream.close()
    assert server.requests[-1] == (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "1"},
    )


@pytest.mark.asyncio
async def test_deferred_tools_are_namespaced_deduplicated_and_dispatch_to_host():
    def spec(name):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": "Synthetic tool",
                "parameters": {"type": "object"},
            },
        }

    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        return "OK", True

    server = Server()
    app = NS(
        plugins=NS(
            tool_specs=lambda: [spec("active")],
            deferred_specs=lambda: [
                spec("active"),
                spec("pccontrol"),
                spec("pccontrol"),
            ]
        ),
        backend=NS(models={}),
        tools_enabled=True,
        conversation=[],
        _execute_tool=execute,
        _rpc_emit=lambda event: None,
    )
    transport = AppServerTransport(server, app)
    stream = await transport.create(
        model="gpt-6-astra",
        messages=[{"role": "user", "content": "Hello"}],
        tools=[spec("active")],
        stream=True,
    )
    async for _ in stream:
        pass
    registered = next(
        params["dynamicTools"]
        for method, params in server.requests
        if method == "thread/start"
    )
    assert registered[0]["name"] == "litetui_active"
    assert not registered[0].get("deferLoading")
    namespace = registered[1]
    assert namespace["type"] == "namespace"
    assert namespace["name"] == "litetui"
    assert len(namespace["tools"]) == 1
    assert namespace["tools"][0]["name"] == "litetui_pccontrol"
    assert namespace["tools"][0]["deferLoading"] is True
    await transport._server_request(
        {
            "id": 99,
            "method": "item/tool/call",
            "params": {
                "namespace": "litetui",
                "tool": "litetui_pccontrol",
                "arguments": {"action": "test"},
            },
        }
    )
    assert calls == [("pccontrol", {"action": "test"})]
    assert server.replies[-1]["result"]["success"] is True


@pytest.mark.asyncio
async def test_dynamic_tools_use_host_authorization_and_approval_cannot_autogrant():
    from test_codex_inventory import app as inventory_app
    from test_codex_inventory import spec

    from litetui.codex_inventory import build_inventory

    calls = []

    async def execute(name, args):
        calls.append((name, args))
        return "denied", False

    app = inventory_app([])
    app.plugins.tool_specs = lambda: [spec("write")]
    app._execute_tool = execute
    app._rpc = True
    server = Server()
    transport = AppServerTransport(server, app)
    transport.registered_inventory = build_inventory(app.plugins.tool_specs(), app)[1]
    await transport._server_request(
        {
            "id": 100,
            "method": "item/tool/call",
            "params": {"tool": "litetui_write", "arguments": {"path": "x"}},
        }
    )
    assert calls == [("write", {"path": "x"})]
    assert server.replies[-1]["result"]["success"] is False
    app.tools_enabled = False
    await transport._server_request(
        {"id": 101, "method": "item/commandExecution/requestApproval", "params": {}}
    )
    assert server.replies[-1]["result"] == {"decision": "cancel"}
    permission_request = {
        "id": 102,
        "method": "item/permissions/requestApproval",
        "params": {"permissions": {"network": {"enabled": True}}},
    }
    await transport._server_request(permission_request)
    assert server.replies[-1]["result"] == {"permissions": {}, "scope": "turn"}

    async def approve(*args, **kwargs):
        return None

    app.tools_enabled = True
    app._authorize_action = approve
    await transport._server_request(permission_request)
    assert server.replies[-1]["result"] == {
        "permissions": permission_request["params"]["permissions"],
        "scope": "turn",
    }


def test_images_survive_user_input_conversion():
    assert user_input(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AA=="},
                    }
                ],
            }
        ]
    ) == [{"type": "image", "url": "data:image/png;base64,AA=="}]


@pytest.mark.asyncio
async def test_real_tui_stream_persists_codex_reference_and_cache_usage():
    from test_deny_stops_the_turn import _app, _turn

    from litetui.oauth_backend import OAuthBackend
    from litetui.settings import Settings

    app = _app(None)
    app.backend = OAuthBackend(Settings(backend="codex"))
    app.backend.app_server = Server()
    app.backend.models = {"gpt-6-astra": {"default_reasoning_level": "medium"}}
    app.model_id = "gpt-6-astra"
    app.thinking_level = "ultra"
    async with app.run_test(size=(100, 35)) as pilot:
        await _turn(app, pilot)
        replies = [m for m in app.conversation if m.get("role") == "assistant"]
        assert replies[-1]["content"] == "OK"
        assert replies[-1]["provider_metadata"]["app_server_thread_id"] == "thread-1"
        assert app.last_usage["cached_tokens"] == 1800
        assert app.ctx_used == 2005
        assert app.tps is None
        assert app.last_usage["thread_usage"]["totalTokens"] == 2005
        assert app._autocompact_due() is None


@pytest.mark.asyncio
async def test_native_compaction_preserves_the_displayed_history():
    messages = [
        {
            "role": "assistant",
            "content": "kept",
            "provider_metadata": {
                "provider": "codex",
                "app_server_thread_id": "thread-1",
            },
        }
    ]
    server = Server()
    transport = AppServerTransport(server, NS(conversation=messages))
    await transport.compact()
    assert server.requests == [
        ("thread/resume", {"threadId": "thread-1"}),
        ("thread/compact/start", {"threadId": "thread-1"}),
    ]
    assert messages[0]["content"] == "kept"


@pytest.mark.asyncio
async def test_changed_instructions_are_an_appended_application_context():
    server = Server()
    transport = AppServerTransport(server)
    messages = [
        {"role": "system", "content": "Original instruction"},
        {"role": "user", "content": "First"},
    ]
    first = await transport.create(model="gpt-6-astra", messages=messages)
    messages.extend(
        [
            {
                "role": "assistant",
                "content": "OK",
                "provider_metadata": first.choices[0].message.provider_metadata,
            },
            {"role": "user", "content": "Next"},
        ]
    )
    messages[0]["content"] = "Changed instruction"
    await transport.create(model="gpt-6-astra", messages=messages)
    turns = [params for method, params in server.requests if method == "turn/start"]
    assert turns[-1]["additionalContext"] == {
        "litetui-instructions": {"kind": "application", "value": "Changed instruction"}
    }


@pytest.mark.asyncio
async def test_host_output_cleanup_and_staged_images(monkeypatch):
    from test_codex_inventory import app as inventory_app
    from test_codex_inventory import spec

    from litetui.codex_inventory import build_inventory

    events = []
    calls = []

    async def execute(name, args):
        calls.append((name, args))
        app._pending_tool_images.append(("test.png", "aW1hZ2U="))
        return "\x1b[31mOPENAI_API_KEY=synthetic-secret\x1b[0m", True

    monkeypatch.setattr("litetui.sanitize.reset_terminal_modes", lambda: None)
    app = inventory_app([])
    app.plugins.tool_specs = lambda: [spec("view_image")]
    app._execute_tool = execute
    app._rpc_emit = events.append
    app._pending_tool_images = []
    app._rpc = True
    server = Server()
    transport = AppServerTransport(server, app)
    transport.registered_inventory = build_inventory(app.plugins.tool_specs(), app)[1]
    await transport._server_request(
        {
            "id": 1,
            "method": "item/tool/call",
            "params": {"tool": "litetui_view_image", "arguments": {}},
        }
    )
    result = server.replies[-1]["result"]
    assert result["success"] is True
    assert calls == [("view_image", {})]
    text = result["contentItems"][0]["text"]
    assert "synthetic-secret" not in text and "\x1b" not in text
    assert events == []  # Native item notifications own the RPC lifecycle.
    from litetui.codex_tool_ui import CodexToolUI

    ui = CodexToolUI(app, thread_id="thread", turn_id="turn")
    await ui.item(
        {
            "type": "dynamicToolCall",
            "id": "call",
            "tool": "litetui_view_image",
            "arguments": {},
            "status": "completed",
            **result,
        },
        True,
    )
    assert text in events[-1]["result"]
    assert "synthetic-secret" not in events[-1]["result"]
    assert result["contentItems"][1] == {
        "type": "inputImage",
        "imageUrl": "data:image/png;base64,aW1hZ2U=",
    }
    assert app._pending_tool_images == []


@pytest.mark.asyncio
async def test_stop_interrupts_even_when_server_has_no_text_events():
    server = Server()
    server.finish = False
    app = NS(
        plugins=NS(deferred_specs=list),
        backend=NS(models={}),
        tools_enabled=True,
        conversation=[],
        _stop_requested=False,
        _rpc=True,
    )
    transport = AppServerTransport(server, app)
    stream = await transport.create(
        model="gpt-6-astra", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    iterator = stream.__aiter__()
    await anext(iterator)
    while not server.events.empty():
        server.events.get_nowait()
    app._stop_requested = True
    pending = asyncio.create_task(anext(iterator))
    for _ in range(30):
        if any(method == "turn/interrupt" for method, _ in server.requests):
            break
        await asyncio.sleep(0.01)
    assert any(method == "turn/interrupt" for method, _ in server.requests)
    await server.events.put(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "1", "status": "interrupted"},
            },
        }
    )
    await asyncio.wait_for(pending, 1)
    await stream.close()
    assert transport.turn_id is None


@pytest.mark.asyncio
async def test_separate_assistant_items_keep_paragraph_boundaries():
    server = Server()
    for ident, text in [("one", "First."), ("one", " More."), ("two", "Next.")]:
        await server.events.put(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "itemId": ident, "delta": text},
            }
        )
    response = await AppServerTransport(server).create(
        model="gpt-6-astra", messages=[{"role": "user", "content": "hi"}]
    )
    assert response.choices[0].message.content.startswith("First. More.\n\nNext.")
