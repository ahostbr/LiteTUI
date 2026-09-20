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
            await self.events.put({"method": "turn/started", "params": {
                "threadId": "thread-1", "turn": {"id": "compact-1"}}})
            await self.events.put({"method": "item/started", "params": {
                "threadId": "thread-1", "turnId": "compact-1",
                "item": {"id": "compact-item", "type": "contextCompaction"}}})
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
@pytest.mark.parametrize("hosted", [False, True])
async def test_native_workspace_survives_package_location_and_resume(tmp_path, monkeypatch, hosted):
    from litetui import paths
    from litetui.codex_app_server import AppServer
    from litetui.model_transport import collect

    async def forbidden(*args, **kwargs):
        raise AssertionError("Workspace regression must not start native inference")

    monkeypatch.setattr(AppServer, "start", forbidden)
    project = tmp_path / "selected project"
    project.mkdir()
    package = tmp_path / "installed package"
    package.mkdir()
    monkeypatch.setattr(paths, "ROOT", package)
    monkeypatch.chdir(project)
    app = NS(_hook_workspace=project, tools_enabled=False, backend=NS(models={}),
             conversation=[], _rpc=True, _rpc_emit=lambda event: None) if hosted else None
    if hosted:
        # The initialized host workspace remains authoritative if process cwd moves.
        monkeypatch.chdir(package)
    server = Server()
    messages = [{"role": "user", "content": "First"}]
    transport = AppServerTransport(server, app)
    first = await collect(await transport.create(model="gpt-6-astra", messages=messages, stream=True))
    messages += [{"role": "assistant", "content": "OK",
                  "provider_metadata": first.choices[0].message.provider_metadata},
                 {"role": "user", "content": "Second"}]
    reopened = AppServerTransport(server, app)
    await collect(await reopened.create(model="gpt-6-astra", messages=messages, stream=True))
    starts = [p for method, p in server.requests if method == "thread/start"]
    turns = [p for method, p in server.requests if method == "turn/start"]
    assert len(starts) == 1 and starts[0]["cwd"] == str(project.resolve())
    assert len(turns) == 2 and all(p["cwd"] == str(project.resolve()) for p in turns)
    assert all(p["threadId"] == "thread-1" for p in turns)
    assert turns[1]["input"] == [{"type": "text", "text": "Second", "text_elements": []}]


@pytest.mark.asyncio
async def test_usage_reset_and_next_turn_baseline_through_transport(monkeypatch):
    from litetui.codex_app_server import AppServer

    async def forbidden(*args, **kwargs):
        raise AssertionError("Usage regression must not start native inference")

    monkeypatch.setattr(AppServer, "start", forbidden)

    # Unlike a mocked meter, this drives the transport queue, metadata carried
    # between calls, and collected response usage. No real process is allowed.
    class UsageServer(Server):
        async def request(self, method, params):
            if method == "turn/start":
                if self.turn == 0:
                    for usage in ({"last": {"totalTokens": 10000}, "total": {"totalTokens": 10000}}, {}):
                        await self.events.put({"method": "thread/tokenUsage/updated", "params": {
                            "threadId": "thread-1", "tokenUsage": usage}})
                else:
                    await self.events.put({"method": "thread/tokenUsage/updated", "params": {
                        "threadId": "unrelated", "tokenUsage": {"total": {"totalTokens": 999999}}}})
            return await super().request(method, params)

    server = UsageServer()
    transport = AppServerTransport(server)
    assert not isinstance(transport.server, AppServer)
    messages = [{"role": "user", "content": "First"}]
    first = await transport.create(model="gpt-6-astra", messages=messages)
    assert first.usage.total_tokens is None
    assert first.usage.context_tokens == 2005
    metadata = first.choices[0].message.provider_metadata
    assert metadata["native_usage"]["total"]["totalTokens"] == 2005
    messages += [{"role": "assistant", "content": "OK", "provider_metadata": metadata},
                 {"role": "user", "content": "Second"}]
    second = await transport.create(model="gpt-6-astra", messages=messages)
    assert second.usage.total_tokens == 2005
    assert second.usage.cached_tokens == 1800
    assert second.usage.context_tokens == 2005
    assert second.usage.thread_usage["totalTokens"] == 4010
    await transport.server.close()


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
        # Native per-turn usage now supports an elapsed-derived rate.
        # A near-zero fixture duration may still leave the rate unknown.
        assert app.tps is None or app.tps > 0
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

@pytest.mark.asyncio
async def test_compaction_cleanup_retains_no_stale_turn():
    server = Server()
    server.session = NS(cleanup_errors=[])
    messages = [{'role': 'assistant', 'content': 'kept', 'provider_metadata': {'provider': 'codex', 'app_server_thread_id': 'thread-1'}}]
    transport = AppServerTransport(server, NS(conversation=messages))
    await transport.compact()
    assert transport.turn_id is None
    assert server.session.cleanup_errors == []

@pytest.mark.asyncio
async def test_actual_stream_cleanup_failure_still_interrupts_and_clears_turn(monkeypatch):
    from litetui.codex_question_requests import QuestionRequests
    server = Server()
    server.finish = False
    server.session = NS(cleanup_errors=[])
    transport = AppServerTransport(server)
    stream = await transport.create(model='gpt-6-astra', messages=[{'role':'user','content':'hi'}], stream=True)
    await anext(stream.__aiter__())
    async def failed_close(self):
        raise RuntimeError('injected question cleanup failure')
    monkeypatch.setattr(QuestionRequests, 'close', failed_close)
    await asyncio.wait_for(stream.close(), 2)
    assert transport.turn_id is None
    assert any(method == 'turn/interrupt' for method, params in server.requests)
    assert any('injected question cleanup failure' in error for error in server.session.cleanup_errors)

@pytest.mark.asyncio
async def test_stream_disconnect_preserves_original_error_despite_cleanup_failure(monkeypatch):
    from litetui.codex_question_requests import QuestionRequests
    server = Server()
    server.finish = False
    server.session = NS(cleanup_errors=[])
    transport = AppServerTransport(server)
    stream = await transport.create(model='gpt-6-astra', messages=[{'role':'user','content':'hi'}], stream=True)
    iterator = stream.__aiter__()
    await anext(iterator)
    while not server.events.empty(): server.events.get_nowait()
    original = RuntimeError('reader disconnected fixture')
    await server.events.put(original)
    async def failed_close(self): raise RuntimeError('secondary cleanup failure')
    monkeypatch.setattr(QuestionRequests, 'close', failed_close)
    with pytest.raises(RuntimeError, match='reader disconnected fixture') as caught:
        async for _ in iterator: pass
    assert caught.value is original
    assert transport.turn_id is None
    assert any('secondary cleanup failure' in error for error in server.session.cleanup_errors)

@pytest.mark.asyncio
async def test_actual_stream_task_cancellation_interrupts_and_clears_turn(monkeypatch):
    from litetui.codex_question_requests import QuestionRequests
    server = Server()
    server.finish = False
    server.session = NS(cleanup_errors=[])
    transport = AppServerTransport(server)
    stream = await transport.create(model='gpt-6-astra', messages=[{'role': 'user', 'content': 'hi'}], stream=True)
    iterator = stream.__aiter__()
    await anext(iterator)
    while not server.events.empty():
        server.events.get_nowait()
    waiting = asyncio.Event()
    original_get = server.events.get
    async def observed_get():
        waiting.set()
        return await original_get()
    monkeypatch.setattr(server.events, 'get', observed_get)
    async def failed_close(self):
        raise RuntimeError('cancellation cleanup fixture')
    monkeypatch.setattr(QuestionRequests, 'close', failed_close)
    pending = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(waiting.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 2)
    assert pending.cancelled()
    assert transport.turn_id is None
    assert any(method == 'turn/interrupt' for method, params in server.requests)
    assert any('cancellation cleanup fixture' in error for error in server.session.cleanup_errors)

@pytest.mark.asyncio
async def test_compaction_ignores_unrelated_completion():
    server = Server()
    await server.events.put({'method': 'turn/completed', 'params': {
        'threadId': 'thread-1', 'turn': {'id': 'old-turn', 'status': 'failed'}}})
    app = NS(conversation=[{'role': 'assistant', 'content': 'kept',
        'provider_metadata': {'provider': 'codex', 'app_server_thread_id': 'thread-1'}}])
    transport = AppServerTransport(server, app)
    await asyncio.wait_for(transport.compact(), 2)
    assert transport.turn_id is None
    assert server.events.empty()

@pytest.mark.asyncio
async def test_compaction_stale_full_lifecycle_cannot_finish_new_operation():
    server = Server()
    for method, extra in [('turn/started', {'turn': {'id': 'stale'}}),
                          ('item/started', {'turnId': 'stale', 'item': {'type': 'contextCompaction'}}),
                          ('turn/completed', {'turn': {'id': 'stale', 'status': 'completed'}})]:
        await server.events.put({'method': method, 'params': {'threadId': 'thread-1', **extra}})
    app = NS(conversation=[{'provider_metadata': {'app_server_thread_id': 'thread-1'}}])
    await AppServerTransport(server, app).compact()
    assert server.events.empty(), 'returned on stale lifecycle instead of current compaction'


@pytest.mark.asyncio
async def test_missing_compaction_events_fail_bounded_and_release_lock():
    from litetui.model_transport import ProviderError
    server = Server()
    original = server.request
    async def missing(method, params):
        if method == 'thread/compact/start':
            await server.events.put({'method': 'turn/started', 'params': {
                'threadId': 'thread-1', 'turn': {'id': 'missing'}}})
            return {}
        return await original(method, params)
    server.request = missing
    app = NS(conversation=[{'content': 'preserved', 'provider_metadata': {'app_server_thread_id': 'thread-1'}}])
    transport = AppServerTransport(server, app)
    with pytest.raises(ProviderError, match='timed out'):
        await transport.compact(timeout=0.03)
    assert not transport.lock.locked()
    assert transport.turn_id is None
    assert app.conversation[0]['content'] == 'preserved'
    assert ('turn/interrupt', {'threadId': 'thread-1', 'turnId': 'missing'}) in server.requests

@pytest.mark.asyncio
async def test_compaction_failure_before_item_is_reported_without_timeout():
    from litetui.model_transport import ProviderError
    server = Server()
    original = server.request
    async def failed(method, params):
        if method == 'thread/compact/start':
            for event, status in [('turn/started', 'inProgress'), ('turn/completed', 'failed')]:
                await server.events.put({'method': event, 'params': {
                    'threadId': 'thread-1', 'turn': {'id': 'failed-turn', 'status': status}}})
            return {}
        return await original(method, params)
    server.request = failed
    transport = AppServerTransport(server, NS(conversation=[{
        'provider_metadata': {'app_server_thread_id': 'thread-1'}}]))
    with pytest.raises(ProviderError, match='did not complete'):
        await transport.compact(timeout=1)
    assert transport.turn_id is None


@pytest.mark.asyncio
async def test_compaction_preserves_pending_approval_and_does_not_dispatch():
    from litetui.model_transport import ProviderError
    server = Server()
    pending = {'id': 99, 'method': 'item/commandExecution/requestApproval', 'params': {}}
    await server.events.put(pending)
    transport = AppServerTransport(server, NS(conversation=[{
        'provider_metadata': {'app_server_thread_id': 'thread-1'}}]))
    with pytest.raises(ProviderError, match='pending server requests'):
        await transport.compact()
    assert server.events.get_nowait() == pending
    assert not any(method == 'thread/compact/start' for method, _ in server.requests)
