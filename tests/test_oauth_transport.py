import base64
import json
import time

import httpx
import pytest

from litetui import model_transport as mt


def token(exp=None):
    payload = {"exp": exp or time.time() + 3600}
    return (
        "header."
        + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        + ".sig"
    )


def auth_file(tmp_path, **changes):
    obj = {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": token(), "account_id": "test-account"},
    }
    obj.update(changes)
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(obj))
    return path


def test_credentials_subscription_only_and_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "SECRET-KEY")
    path = auth_file(tmp_path)
    before = path.read_bytes()
    assert mt.read_credentials("codex", path).account_id == "test-account"
    assert path.read_bytes() == before
    for obj in (
        {"OPENAI_API_KEY": "SECRET-KEY"},
        {"auth_mode": "apikey"},
        {"tokens": {}},
    ):
        path.write_text(json.dumps(obj))
        with pytest.raises(mt.ProviderError, match="codex login") as error:
            mt.read_credentials("codex", path)
        assert "SECRET-KEY" not in str(error.value)


def test_missing_malformed_expired_credentials(tmp_path):
    path = tmp_path / "missing"
    for content in (
        None,
        "{broken",
        "[]",
        "null",
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": token(1), "account_id": "a"},
            }
        ),
    ):
        if content is not None:
            path.write_text(content)
        with pytest.raises(mt.ProviderError):
            mt.read_credentials("codex", path)


def test_codex_history_conversion_does_not_mutate():
    messages = [
        {"role": "system", "content": "LiteTUI identity"},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AA=="},
                }
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_a",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                }
            ],
            "provider_metadata": {
                "provider": "claude",
                "model": "claude-test",
                "items": [{"type": "thinking", "signature": "FOREIGN"}],
            },
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "result"},
    ]
    before = json.dumps(messages)
    body = mt.codex_request({"model": "gpt-test", "messages": messages})
    assert body["instructions"] == "LiteTUI identity"
    assert body["store"] is False
    assert body["input"][0]["content"][0]["type"] == "input_image"
    assert body["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_a",
        "output": "result",
    }
    assert "FOREIGN" not in json.dumps(body)
    assert json.dumps(messages) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("history_shape", ["later-user-turn", "user-stop"])
async def test_resumed_missing_tool_output_request_is_accepted(tmp_path, history_shape):
    from litetui.conversation import ConversationRepository

    messages = [
        {"role": "system", "content": "Resume diagnostic"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_done", "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_done", "content": "saved result"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_interrupted", "function": {"name": "write", "arguments": "{}"}},
        ], "provider_metadata": {
            "provider": "codex", "model": "gpt-test",
            "items": [{"type": "reasoning", "id": "rs_saved",
                       "encrypted_content": "opaque", "summary": []}],
        }},
    ]
    if history_shape == "later-user-turn":
        messages.append({"role": "user", "content": "Inbox arrived after the saved call"})
    # A user stop returns after persisting the call, leaving no output and no
    # later message. Resume must repair that shape without claiming a crash.
    transcript = tmp_path / "convo.jsonl"
    transcript.write_text(json.dumps({"type": "snapshot", "messages": messages}) + "\n")
    _, resumed = ConversationRepository.read(transcript)
    before = json.dumps(resumed)

    def handle(request):
        items = json.loads(request.content)["input"]
        calls = {i["call_id"] for i in items if i.get("type") == "function_call"}
        outputs = {i["call_id"]: i["output"] for i in items
                   if i.get("type") == "function_call_output"}
        if calls - outputs.keys():
            return httpx.Response(400, json={"error": {"message": "No tool output found"}})
        assert outputs["call_done"] == "saved result"
        annotation = outputs["call_interrupted"]
        assert annotation == (
            "[litetui transport] no tool output was recorded for this call; "
            "outcome UNKNOWN. Verify any side effects before retrying."
        )
        assert not any(cause in annotation.lower()
                       for cause in ("ended", "died", "crash", "killed"))
        interrupted = next(n for n, i in enumerate(items)
                           if i.get("call_id") == "call_interrupted")
        assert items[interrupted + 1]["type"] == "function_call_output"
        assert any(i.get("id") == "rs_saved" for i in items)
        return httpx.Response(200, text='data: {"type":"response.completed",'
                              '"response":{"output":[]}}\n\n')

    transport = mt.OAuthTransport(
        "codex", credential_path=auth_file(tmp_path),
        http_transport=httpx.MockTransport(handle),
    )
    await transport.create(model="gpt-test", messages=resumed, stream=False)
    assert json.dumps(resumed) == before
    assert ConversationRepository.read(transcript)[1] == messages


@pytest.mark.asyncio
async def test_codex_stream_tool_and_usage(tmp_path):
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "read",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": '{"path":"a"}',
        },
        {
            "type": "response.completed",
            "response": {
                "usage": {"input_tokens": 20, "output_tokens": 3},
                "output": [],
            },
        },
    ]

    def handle(request):
        assert request.headers["chatgpt-account-id"] == "test-account"
        assert "x-api-key" not in request.headers
        return httpx.Response(
            200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events)
        )

    transport = mt.OAuthTransport(
        "codex",
        credential_path=auth_file(tmp_path),
        http_transport=httpx.MockTransport(handle),
    )
    result = await transport.create(model="gpt-test", messages=[], stream=False)
    call = result.choices[0].message.tool_calls[0]
    assert call.function.name == "read"
    assert call.function.arguments == '{"path":"a"}'
    assert result.usage.total_tokens == 23


@pytest.mark.asyncio
@pytest.mark.parametrize("final_event,prefix", [
    ("arguments", ""), ("item", ""), ("completed", ""),
    ("all", ""), ("all", "partial"), ("all", "full"), ("all", "added"),
])
async def test_parallel_batch_terminal_arguments_reach_turn_engine_once(tmp_path, final_event, prefix):
    from litetui.turn_engine import TurnEngine

    # Synthetic Responses protocol fixture, not a captured incident stream.
    # Include a non-call output so final fallback must retain output indices.
    items = [{"type": "reasoning", "id": "rs_fixture", "summary": []}] + [
        {"type": "function_call", "call_id": "call_read", "name": "read",
         "arguments": '{"path":"fixture.md","offset":2}'},
        {"type": "function_call", "call_id": "call_other", "name": "any_tool",
         "arguments": '{"action":"check","nested":{"value":3}}'},
    ]
    events = []
    for idx, item in enumerate(items[1:], 1):
        arguments = item["arguments"]
        events.append({"type": "response.output_item.added", "output_index": idx,
                       "item": {**item, "arguments": arguments if prefix == "added" else ""}})
        if prefix in ("partial", "full"):
            part = arguments[:10] if prefix == "partial" else arguments
            events.append({"type": "response.function_call_arguments.delta",
                           "output_index": idx, "delta": part})
        if final_event in ("arguments", "all"):
            events.append({"type": "response.function_call_arguments.done",
                           "output_index": idx, "arguments": arguments})
        if final_event in ("item", "all"):
            events.append({"type": "response.output_item.done", "output_index": idx, "item": item})
    final_items = items if final_event in ("completed", "all") else []
    events.append({"type": "response.completed", "response": {"output": final_items}})
    transport = mt.OAuthTransport(
        "codex", credential_path=auth_file(tmp_path),
        http_transport=httpx.MockTransport(lambda request: httpx.Response(
            200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events))),
    )
    stream = await transport.create(model="arbitrary-codex-model", messages=[], stream=True)
    calls = {}
    async for chunk in stream:
        for call in chunk.choices[0].delta.tool_calls or []:
            TurnEngine.accumulate_tool_call(calls, call)
    assert calls == {idx: {"id": item["call_id"], "name": item["name"],
                           "arguments": item["arguments"]}
                     for idx, item in enumerate(items[1:], 1)}


@pytest.mark.asyncio
async def test_conflicting_terminal_arguments_never_release_calls(tmp_path):
    events = [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"path":"a"}'},
        {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": '{"path":"b"}'},
        {"type": "response.completed", "response": {"output": []}},
    ]
    transport = mt.OAuthTransport(
        "codex", credential_path=auth_file(tmp_path),
        http_transport=httpx.MockTransport(lambda request: httpx.Response(
            200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events))),
    )
    stream = await transport.create(model="fixture", messages=[], stream=True)
    released = []
    with pytest.raises(mt.ProviderError, match="conflicting tool arguments"):
        async for chunk in stream:
            released.extend(chunk.choices[0].delta.tool_calls or [])
    assert released == []


@pytest.fixture
def owned_capture_app(tmp_path):
    from types import SimpleNamespace
    from litetui.agent_ownership import AgentSession
    from litetui.agent_store import AgentStore

    session = AgentSession.create_fresh(
        AgentStore(tmp_path), name="CaptureFixture",
        agent_id="11111111-1111-4111-8111-111111111111",
        backend="codex", model="fixture", thinking_level="high",
    )
    try:
        yield SimpleNamespace(_agent_session=session, convo_id=session.initial_conversation_id)
    finally:
        session.release()


def test_stream_capture_disabled_by_default_and_unowned_refused(owned_capture_app, monkeypatch):
    from types import SimpleNamespace
    from litetui.tool_stream_capture import CAPTURE_ENV, CAPTURE_FILE, ToolStreamCapture

    monkeypatch.delenv(CAPTURE_ENV, raising=False)
    assert ToolStreamCapture.for_app(owned_capture_app) is None
    directory = owned_capture_app._agent_session.conversation_directory(owned_capture_app.convo_id)
    assert not (directory / CAPTURE_FILE).exists()
    monkeypatch.setenv(CAPTURE_ENV, "1")
    assert ToolStreamCapture.for_app(SimpleNamespace(convo_id=owned_capture_app.convo_id)) is None


@pytest.mark.asyncio
async def test_real_transport_capture_metadata_only(owned_capture_app, tmp_path, monkeypatch):
    from litetui.tool_stream_capture import CAPTURE_ENV, CAPTURE_FILE, ToolStreamCapture

    monkeypatch.setenv(CAPTURE_ENV, "1")
    capture = ToolStreamCapture.for_app(owned_capture_app)
    payload = '{"password":"DO-NOT-SAVE","path":"sensitive.md"}'
    item = {"type": "function_call", "call_id": "call_fixture", "name": "read", "arguments": payload}
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
        {"type": "response.output_text.delta", "delta": "PRIVATE-TEXT"},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": payload[:10]},
        {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": payload},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"output": [item, {
            "type": "reasoning", "encrypted_content": "PRIVATE-REASONING"}]}},
    ]
    transport = mt.OAuthTransport(
        "codex", credential_path=auth_file(tmp_path), tool_capture=capture,
        http_transport=httpx.MockTransport(lambda request: httpx.Response(
            200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events))),
    )
    result = await transport.create(model="fixture", messages=[], stream=False)
    assert result.choices[0].message.tool_calls[0].function.arguments == payload
    directory = owned_capture_app._agent_session.conversation_directory(owned_capture_app.convo_id)
    raw = (directory / CAPTURE_FILE).read_text()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 5
    assert [r["sequence"] for r in rows] == [1, 2, 3, 4, 5]
    assert [r["argument_chars"] for r in rows] == [0, 10, len(payload), len(payload), len(payload)]
    assert all(set(r) == {"type", "sequence", "call_id", "name", "argument_chars"} for r in rows)
    assert all(r["call_id"] == "call_fixture" and r["name"] == "read" for r in rows)
    for sensitive in ("DO-NOT-SAVE", "sensitive.md", "PRIVATE-TEXT", "PRIVATE-REASONING", "Authorization"):
        assert sensitive not in raw


def test_capture_cap_and_released_ownership(owned_capture_app, monkeypatch):
    from litetui import tool_stream_capture as capture_mod

    monkeypatch.setenv(capture_mod.CAPTURE_ENV, "1")
    capture = capture_mod.ToolStreamCapture.for_app(owned_capture_app)
    directory = owned_capture_app._agent_session.conversation_directory(owned_capture_app.convo_id)
    target = directory / capture_mod.CAPTURE_FILE
    # Existing full capture is retained verbatim; no rotation/deletion/growth.
    target.write_bytes(b"x" * capture_mod.MAX_BYTES)
    capture.record({"type": "response.function_call_arguments.done", "arguments": "secret"})
    assert capture.stopped
    assert target.stat().st_size == capture_mod.MAX_BYTES
    target.write_bytes(b"")
    second = capture_mod.ToolStreamCapture.for_app(owned_capture_app)
    owned_capture_app._agent_session.release()
    second.record({"type": "response.function_call_arguments.done", "arguments": "secret"})
    assert second.stopped
    assert target.read_bytes() == b""


def test_capture_cannot_follow_hardlink_or_cross_conversation(owned_capture_app, tmp_path, monkeypatch):
    import os
    from litetui import tool_stream_capture as capture_mod

    monkeypatch.setenv(capture_mod.CAPTURE_ENV, "1")
    directory = owned_capture_app._agent_session.conversation_directory(owned_capture_app.convo_id)
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_bytes(b"unchanged")
    os.link(foreign, directory / capture_mod.CAPTURE_FILE)
    capture = capture_mod.ToolStreamCapture.for_app(owned_capture_app)
    capture.record({"type": "response.function_call_arguments.done", "arguments": "secret"})
    assert capture.stopped
    assert foreign.read_bytes() == b"unchanged"
    invalid = capture_mod.ToolStreamCapture(owned_capture_app._agent_session, "../other-seat")
    invalid.record({"type": "response.function_call_arguments.done", "arguments": "secret"})
    assert invalid.stopped
    assert not (directory.parent / "other-seat").exists()


@pytest.mark.asyncio
async def test_error_body_never_exposed(tmp_path):
    transport = mt.OAuthTransport(
        "codex",
        credential_path=auth_file(tmp_path),
        http_transport=httpx.MockTransport(
            lambda r: httpx.Response(403, text="SECRET TOKEN BODY")
        ),
    )
    with pytest.raises(mt.ProviderError) as error:
        await transport.create(model="gpt-test", messages=[], stream=False)
    assert "SECRET" not in str(error.value)


@pytest.mark.asyncio
async def test_truncated_stream_is_not_success(tmp_path):
    transport = mt.OAuthTransport(
        "codex",
        credential_path=auth_file(tmp_path),
        http_transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                text='data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
            )
        ),
    )
    with pytest.raises(mt.ProviderError, match="ended"):
        await transport.create(model="gpt-test", messages=[], stream=False)


def test_metadata_only_replayed_for_same_provider_and_model():
    item = {
        "type": "reasoning",
        "id": "rs_test",
        "encrypted_content": "opaque",
        "summary": [],
    }
    msg = {
        "role": "assistant",
        "content": "hello",
        "provider_metadata": {
            "provider": "codex",
            "model": "gpt-test",
            "items": [item],
        },
    }
    assert item in mt.codex_request({"model": "gpt-test", "messages": [msg]})["input"]
    assert "opaque" not in json.dumps(
        mt.codex_request({"model": "other", "messages": [msg]})
    )
    assert "opaque" not in json.dumps(
        mt.claude_request({"model": "claude-test", "messages": [msg]})
    )


@pytest.mark.asyncio
async def test_auth_failure_rereads_changed_credentials(tmp_path):
    path = auth_file(tmp_path)
    calls = []

    def handle(request):
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            obj = json.loads(path.read_text())
            obj["tokens"]["access_token"] = token(time.time() + 7200)
            path.write_text(json.dumps(obj))
            return httpx.Response(401, text="SECRET")
        return httpx.Response(
            200,
            text='data: {"type":"response.completed","response":{"usage":{},"output":[]}}\n\n',
        )

    await mt.OAuthTransport(
        "codex", credential_path=path, http_transport=httpx.MockTransport(handle)
    ).create(model="gpt-test", messages=[])
    assert len(calls) == 2 and calls[0] != calls[1]


@pytest.mark.asyncio
async def test_auth_failure_does_not_retry_same_credentials(tmp_path):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(401)

    with pytest.raises(mt.ProviderError, match="codex login"):
        await mt.OAuthTransport(
            "codex",
            credential_path=auth_file(tmp_path),
            http_transport=httpx.MockTransport(handle),
        ).create(model="gpt-test", messages=[])
    assert count == 1


@pytest.mark.asyncio
async def test_local_client_never_receives_opaque_metadata():
    from types import SimpleNamespace

    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    await mt.OpenAITransport(client).create(
        messages=[
            {
                "role": "assistant",
                "content": "hi",
                "provider_metadata": {"items": ["opaque"]},
                "codex_delivery": {"id": "private-client-id", "state": "accepted"},
            }
        ]
    )
    assert captured["messages"] == [{"role": "assistant", "content": "hi"}]


@pytest.mark.asyncio
async def test_cancellation_closes_http_stream(tmp_path):
    import asyncio

    closed = asyncio.Event()
    waiting = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            waiting.set()
            await asyncio.sleep(60)
            yield b""

        async def aclose(self):
            closed.set()

    transport = mt.OAuthTransport(
        "codex",
        credential_path=auth_file(tmp_path),
        http_transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=SlowStream())
        ),
    )
    task = asyncio.create_task(transport.create(model="gpt-test", messages=[]))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


def test_remote_subagent_uses_oauth_not_local_endpoint(monkeypatch):
    from types import SimpleNamespace

    captured = {}

    async def create(self, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="answer", reasoning_content="")
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(mt.OAuthTransport, "create", create)
    app = SimpleNamespace(
        backend=SimpleNamespace(
            remote=True, name="codex", reasoning_levels=lambda m: ["low", "medium"]
        )
    )
    result = mt.complete_sidecall(
        app,
        {
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "none",
        },
    )
    assert result["choices"][0]["message"]["content"] == "answer"
    assert captured["extra_body"]["reasoning_effort"] == "low"
    assert "tools" not in captured


def test_claude_subscription_credentials_and_custom_prompt(tmp_path):
    path = tmp_path / "claude.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-test",
                    "expiresAt": (time.time() + 3600) * 1000,
                    "subscriptionType": "max",
                    "scopes": ["user:inference"],
                }
            }
        )
    )
    assert mt.read_credentials("claude", path).access == "sk-ant-oat-test"
    body = mt.claude_request(
        {
            "model": "claude-test",
            "messages": [{"role": "system", "content": "LiteTUI identity"}],
        }
    )
    assert body["system"] == [{"type": "text", "text": "LiteTUI identity"}]
    assert "claude" not in mt.OAUTH_PROVIDERS, "Unverified Claude must not be offered"


@pytest.mark.asyncio
async def test_cloud_backend_uses_cli_capabilities_and_rejects_local_controls(
    tmp_path, monkeypatch
):
    from litetui.llm_backend import BackendError, make_backend
    from litetui.oauth_backend import OAuthBackend
    from litetui.settings import Settings

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = auth_file(tmp_path)
    assert path.exists()
    (tmp_path / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-test",
                        "context_window": 100000,
                        "effective_context_window_percent": 90,
                        "visibility": "list",
                        "input_modalities": ["text", "image"],
                        "supported_reasoning_levels": [{"effort": "low"}],
                    },
                    {"slug": "hidden", "context_window": 100000, "visibility": "hide"},
                ]
            }
        )
    )
    # 0.23.1: the default codex backend is LiteTUI's own loop — no app_server,
    # capabilities come from the CLI's models_cache.json exactly as asserted here.
    backend = make_backend(Settings(backend="codex"))
    assert not hasattr(backend, "app_server")
    assert await backend.ensure_running() == "ok"
    assert [m.key for m in await backend.list_models()] == ["gpt-test"]
    assert await backend.model_info("gpt-test") == (90000, "vlm", True)
    with pytest.raises(BackendError):
        await backend.load("gpt-test")
    # Claude became a first-class SDK backend in 67a1469, so the factory no longer
    # refuses it; the contract that survives is that it is never the Codex OAuth path.
    assert not isinstance(make_backend(Settings(backend="claude")), OAuthBackend)
    with pytest.raises(mt.ProviderError, match="reasoning effort"):
        await mt.OAuthTransport("codex", models=backend.models).create(
            model="gpt-test", messages=[], extra_body={"reasoning_effort": "ultra"}
        )
