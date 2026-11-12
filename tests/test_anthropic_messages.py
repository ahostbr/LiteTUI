"""Failure, interruption and history contracts for the native Messages adapter."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS

import httpx
import pytest
from test_anthropic_api import KEY, MODEL, MODEL_INFO, TOOL, backend, sse

from litetui import anthropic_messages
from litetui import model_transport as mt
from litetui.anthropic_backend import AnthropicBackend
from litetui.launch_options import LaunchOptions
from litetui.settings import Settings


def tool_events(*, name="read", initial=None, arguments='{"path":"a"}', stop="tool_use"):
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "toolu_1", "name": name, "input": initial or {}}},
    ]
    if arguments is not None:
        events.append({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "input_json_delta", "partial_json": arguments}})
    events += [{"type": "content_block_stop", "index": 0},
               {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 2}},
               {"type": "message_stop"}]
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["open-block", "bad-json", "array-input", "unknown-name", "case-name", "wrong-stop", "truncated", "missing-terminal", "wrong-delta", "unknown-tool-delta"])
async def test_invalid_stream_never_releases_tools(fault):
    events = tool_events()
    if fault == "open-block":
        events.pop(3)
    elif fault == "bad-json":
        events[2]["delta"]["partial_json"] = '{"path":'
    elif fault == "array-input":
        events[2]["delta"]["partial_json"] = '[]'
    elif fault in ("unknown-name", "case-name"):
        events[1]["content_block"]["name"] = "unknown" if fault == "unknown-name" else "Read"
    elif fault == "wrong-stop":
        events[-2]["delta"]["stop_reason"] = "end_turn"
    elif fault == "truncated":
        events[-2]["delta"]["stop_reason"] = "max_tokens"
    elif fault == "missing-terminal":
        events.pop()
    elif fault == "wrong-delta":
        events[2]["delta"] = []
    elif fault == "unknown-tool-delta":
        events[2]["delta"]["type"] = "future_input_delta"
    b = backend(lambda _: httpx.Response(200, text=sse(*events)))
    stream = await mt.for_app(NS(backend=b)).create(model=MODEL, messages=[], tools=[TOOL], stream=True)
    calls = []
    with pytest.raises(mt.ProviderError, match="Anthropic"):
        async for chunk in stream:
            calls.extend(chunk.choices[0].delta.tool_calls)
    assert calls == []
    assert stream.client.is_closed


@pytest.mark.asyncio
async def test_initial_tool_input_is_not_lost():
    events = tool_events(initial={"path": "initial"}, arguments=None)
    b = backend(lambda _: httpx.Response(200, text=sse(*events)))
    result = await mt.for_app(NS(backend=b)).create(model=MODEL, messages=[], tools=[TOOL])
    assert json.loads(result.choices[0].message.tool_calls[0].function.arguments) == {"path": "initial"}


def test_missing_tool_result_repair_does_not_mutate_or_claim_execution():
    messages = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "toolu_1", "function": {"name": "read", "arguments": "{}"}},
        {"id": "toolu_2", "function": {"name": "read", "arguments": "{}"}},
    ]}, {"role": "user", "content": "resume"},
       {"role": "tool", "tool_call_id": "toolu_1", "content": "recorded result"}]
    before = copy.deepcopy(messages)
    result = anthropic_messages.request({"model": MODEL, "messages": messages})
    blocks = result["messages"][1]["content"]
    assert [block["type"] for block in blocks] == ["tool_result", "tool_result", "text"]
    missing = next(block for block in blocks if block.get("tool_use_id") == "toolu_2")
    assert missing["is_error"] and "outcome UNKNOWN" in missing["content"]
    assert messages == before


@pytest.mark.parametrize("messages", [
    [{"role": "tool", "tool_call_id": "orphan", "content": "result"}],
    [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "function": {"name": "read", "arguments": "[]"}}]}],
    [{"role": "assistant", "provider_metadata": ["malformed"]}],
])
def test_malformed_history_refuses(messages):
    with pytest.raises(mt.ProviderError, match="history"):
        anthropic_messages.request({"model": MODEL, "messages": messages})


@pytest.mark.asyncio
async def test_sse_error_sanitizes_key_and_closes():
    b = backend(lambda _: httpx.Response(200, text=sse({"type": "error", "error": {"message": KEY}})))
    stream = await mt.for_app(NS(backend=b)).create(model=MODEL, messages=[], stream=True)
    with pytest.raises(mt.ProviderError) as error:
        await mt.collect(stream)
    assert KEY not in str(error.value)
    assert stream.client.is_closed


@pytest.mark.asyncio
async def test_cancellation_closes_stream_and_client():
    waiting = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield sse({"type": "message_start", "message": {"usage": {}}}).encode()
            waiting.set()
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    response_stream = SlowStream()
    b = backend(lambda _: httpx.Response(200, stream=response_stream))
    stream = await mt.for_app(NS(backend=b)).create(model=MODEL, messages=[], stream=True)
    task = asyncio.create_task(mt.collect(stream))
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert response_stream.closed and stream.client.is_closed


@pytest.mark.asyncio
async def test_catalog_invalidated_by_environment_and_in_place_key_changes(monkeypatch):
    keys = []

    def handle(request):
        keys.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer rejected":
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [MODEL_INFO], "has_more": False})

    settings = Settings(backend="anthropic")
    b = AnthropicBackend(settings, http_transport=httpx.MockTransport(handle))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "first")
    await b.ensure_chat_ready(MODEL)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "second")
    await b.ensure_chat_ready(MODEL)
    settings.anthropic_api_key = "rejected"
    with pytest.raises(mt.ProviderError, match="API key"):
        await b.ensure_chat_ready(MODEL)
    assert keys == ["Bearer first", "Bearer second", "Bearer rejected"]
    assert b.models == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [[], {"data": [{}]}, {"data": [], "has_more": True},
                                  {"data": [], "has_more": False}])
async def test_malformed_or_empty_catalog_fails_safely(page):
    b = backend(lambda _: httpx.Response(200, json=page), catalog=False)
    with pytest.raises(mt.ProviderError, match="Anthropic"):
        await b.list_models()


@pytest.mark.asyncio
async def test_malformed_capabilities_do_not_advertise_effort_or_crash():
    row = {**MODEL_INFO, "capabilities": {"thinking": ["bad"], "effort": ["bad"]}}
    b = backend(lambda _: httpx.Response(200, json={"data": [row]}), catalog=False)
    await b.list_models()
    assert b.reasoning_levels(MODEL) == []


@pytest.mark.parametrize("options", [
    {"base_url": "http://localhost:1"}, {"context_length": 1024},
    {"server_mode": "start"}, {"load_model": True}, {"model_path": "fixture.gguf"},
    {"api_key_env": "OTHER_KEY"}, {"server_executable": "fixture.exe"},
])
def test_local_launch_options_refused_without_keyerror(options):
    with pytest.raises(ValueError, match="Anthropic"):
        LaunchOptions(**options).overrides(Settings(), "anthropic", MODEL)


def test_output_budget_and_stop_supported():
    values = LaunchOptions(max_tokens=2048).overrides(Settings(), "anthropic", MODEL)
    assert values["max_tokens_chat"] == 2048
    body = anthropic_messages.request({"model": MODEL, "messages": [], "stop": "END", "tool_choice": "none"})
    assert body["stop_sequences"] == ["END"]
    assert "tool_choice" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["required", {"type": "function", "function": {"name": "read"}}])
async def test_forced_tool_choice_with_thinking_refuses_before_http(choice):
    b = backend(lambda _: pytest.fail("incompatible tool choice reached HTTP"))
    with pytest.raises(mt.ProviderError, match="tool choice"):
        await b.transport().create(model=MODEL, messages=[], tools=[TOOL],
                                   tool_choice=choice, extra_body={"reasoning_effort": "high"})


@pytest.mark.asyncio
async def test_catalog_refresh_keeps_valid_snapshot_available_to_active_requests():
    refresh_started, finish_refresh = asyncio.Event(), asyncio.Event()
    catalog_calls = 0

    async def handle(request):
        nonlocal catalog_calls
        if request.method == "POST":
            from test_anthropic_api import completed
            return httpx.Response(200, text=completed())
        catalog_calls += 1
        if catalog_calls == 2:
            refresh_started.set()
            await finish_refresh.wait()
        return httpx.Response(200, json={"data": [MODEL_INFO]})

    b = backend(handle, catalog=False)
    await b.list_models()
    refresh = asyncio.create_task(b.list_models())
    try:
        await asyncio.wait_for(refresh_started.wait(), 2)
        result = await b.transport().create(model=MODEL, messages=[])
        assert result.choices[0].message.content == "Hello"
        assert catalog_calls == 2, "active request lost its valid catalog during refresh"
    finally:
        finish_refresh.set()
        await refresh
