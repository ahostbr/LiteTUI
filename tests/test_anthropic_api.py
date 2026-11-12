"""Native Anthropic API-key contracts; no real credentials or paid requests."""
import copy
import json
from types import SimpleNamespace as NS

import httpx
import pytest

from litetui import model_transport as mt
from litetui.llm_backend import make_backend
from litetui.settings import Settings

MODEL = "claude-api-fixture"
KEY = "fixture-api-credential-do-not-log"
MODEL_INFO = {
    "id": MODEL, "type": "model", "max_input_tokens": 200000, "max_tokens": 8192,
    "capabilities": {
        "image_input": {"supported": True},
        "thinking": {"supported": True, "types": {
            "adaptive": {"supported": True}, "disabled": {"supported": True}}},
        "effort": {"supported": True, "low": {"supported": True},
                   "high": {"supported": True}},
    },
}
TOOL = {"type": "function", "function": {"name": "read", "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}


def sse(*events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events)


def completed(text="Hello"):
    return sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 7,
         "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    )


def backend(handler, *, catalog=True, **settings):
    from litetui.anthropic_backend import AnthropicBackend
    def route(request):
        if catalog and request.method == "GET":
            return httpx.Response(200, json={"data": [MODEL_INFO], "has_more": False})
        return handler(request)

    instance = AnthropicBackend(Settings(backend="anthropic", anthropic_api_key=KEY, **settings),
                                http_transport=httpx.MockTransport(route))
    return instance


def test_factory_and_credentials_are_separate_from_oauth(monkeypatch):
    from litetui.anthropic_backend import AnthropicBackend, api_key
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-key")
    assert api_key(Settings()) == "environment-key"
    settings = Settings(backend="anthropic", anthropic_api_key=KEY)
    assert api_key(settings) == KEY
    assert KEY not in repr(settings)
    assert isinstance(make_backend(settings), AnthropicBackend)
    claude = make_backend(Settings(backend="claude"))
    assert claude.owns_native_turns
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(mt.ProviderError, match="ANTHROPIC_API_KEY"):
        api_key(Settings())


@pytest.mark.asyncio
async def test_models_are_paginated_and_use_live_capabilities():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url).startswith("https://api.anthropic.com/v1/models")
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.headers["anthropic-version"] == "2026-10-21"
        assert request.headers["anthropic-workspace-id"] == "wrkspc_fixture"
        assert "anthropic-beta" not in request.headers
        if len(requests) == 1:
            return httpx.Response(200, json={"data": [MODEL_INFO], "has_more": True, "last_id": MODEL})
        assert request.url.params["after_id"] == MODEL
        return httpx.Response(200, json={"data": [{"id": "unknown-metadata"}], "has_more": False})

    b = backend(handle, catalog=False, anthropic_workspace_id="wrkspc_fixture")
    rows = await b.list_models()
    assert [row.key for row in rows] == [MODEL, "unknown-metadata"]
    assert rows[0].loaded and "image" in rows[0].modalities
    assert await b.model_info(MODEL) == (200000, "vlm", True)
    assert await b.model_info("unknown-metadata") == (None, "llm", True)
    assert b.reasoning_levels(MODEL) == ["none", "low", "high"]
    assert b.reasoning_levels("unknown-metadata") == []
    with pytest.raises(mt.ProviderError, match="/model"):
        await b.ensure_chat_ready("not-advertised")


@pytest.mark.asyncio
async def test_messages_route_stream_collection_and_sidecall(monkeypatch):
    bodies = []
    monkeypatch.setattr(mt, "read_credentials", lambda *a: pytest.fail("must not read OAuth"))

    def handle(request):
        assert request.method == "POST"
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert "anthropic-beta" not in request.headers
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=completed())

    b = backend(handle)
    app = NS(backend=b)
    transport = mt.for_app(app)
    result = await transport.create(purpose="compaction", model=MODEL,
        messages=[{"role": "system", "content": "Host identity"}, {"role": "user", "content": "Hi"}],
        stream=False, temperature=0.2, max_tokens=2048, extra_body={"reasoning_effort": "high"})
    assert result.choices[0].message.content == "Hello"
    assert result.usage.total_tokens == 25
    assert result.usage.cached_tokens == 10
    assert result.usage.cache_write_tokens == 5
    assert bodies[0]["output_config"] == {"effort": "high"}
    assert bodies[0]["thinking"] == {"type": "adaptive"}
    assert bodies[0]["system"] == [{"type": "text", "text": "Host identity"}]
    assert bodies[0]["stream"] is True
    assert bodies[0]["max_tokens"] == 2048
    assert "purpose" not in bodies[0] and "temperature" not in bodies[0]
    payload = {"model": MODEL, "messages": [{"role": "user", "content": "summarize"}],
               "reasoning_effort": "none", "max_tokens": 1024}
    import asyncio
    side = await asyncio.to_thread(mt.complete_sidecall, app, payload)
    assert side["choices"][0]["message"]["content"] == "Hello"
    assert bodies[-1]["thinking"] == {"type": "disabled"}
    from litetui.subagent_dispatch import _execute
    child = await _execute(b, payload)
    assert child["choices"][0]["message"]["content"] == "Hello"


@pytest.mark.asyncio
async def test_tool_thinking_and_image_roundtrip_is_provider_scoped():
    bodies = []
    events = [
        {"type": "message_start", "message": {"usage": {}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Plan"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "opaque"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"path":"a"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    ]

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=sse(*events) if len(bodies) == 1 else completed())

    transport = mt.for_app(NS(backend=backend(handle)))
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}]
    first = await transport.create(model=MODEL, messages=messages, tools=[TOOL])
    msg = first.choices[0].message
    assert msg.reasoning_content == "Plan"
    assert msg.provider_metadata["provider"] == "anthropic"
    assert json.loads(msg.tool_calls[0].function.arguments) == {"path": "a"}
    messages += [{"role": "assistant", "content": None, "provider_metadata": msg.provider_metadata,
                  "tool_calls": [{"id": "toolu_1", "function": {"name": "read", "arguments": '{"path":"a"}'}}]},
                 {"role": "tool", "tool_call_id": "toolu_1", "content": [
                     {"type": "text", "text": "result"},
                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}]
    before = copy.deepcopy(messages)
    await transport.create(model=MODEL, messages=messages, tools=[TOOL])
    assert messages == before
    body = bodies[-1]
    assert body["tools"][0]["name"] == "read"
    assert body["messages"][0]["content"][0]["type"] == "image"
    assert body["messages"][1]["content"][0]["signature"] == "opaque"
    assert body["messages"][2]["content"][0]["content"][1]["type"] == "image"
    messages[1]["provider_metadata"]["provider"] = "claude"
    await transport.create(model=MODEL, messages=messages, tools=[TOOL])
    assert "opaque" not in json.dumps(bodies[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("status,match", [(401, "API key"), (403, "access"), (429, "rate"), (529, "unavailable"), (400, "HTTP 400")])
async def test_http_errors_are_safe_and_never_fall_back(status, match):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(status, json={"error": {"message": KEY}})

    b = backend(handle)
    with pytest.raises(mt.ProviderError, match=match) as error:
        await mt.for_app(NS(backend=b)).create(model=MODEL, messages=[])
    assert KEY not in str(error.value)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_unsupported_effort_refuses_before_http():
    b = backend(lambda request: pytest.fail("unsupported effort sent"))
    with pytest.raises(mt.ProviderError, match="effort"):
        await mt.for_app(NS(backend=b)).create(model=MODEL, messages=[], extra_body={"reasoning_effort": "xhigh"})


def test_key_scope_and_public_projection(tmp_path):
    from litetui.settings_scope import SETTING_SPECS, SettingScope, validate_registry
    from litetui.settings_service import SettingsService
    from litetui.sidecar_settings import public_snapshot
    validate_registry()
    assert SETTING_SPECS["anthropic_api_key"].scope == SettingScope.DEVICE
    service = SettingsService(tmp_path)
    # Read a real settings snapshot; secret values never go to the native preview.
    (tmp_path / "settings.json").write_text(json.dumps({"anthropic_api_key": KEY}))
    public = public_snapshot(service.snapshot("fixture-conversation"))
    assert public["fields"]["anthropic_api_key"]["set"] is True
    assert KEY not in json.dumps(public)
