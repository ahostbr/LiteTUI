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
    backend = make_backend(Settings(backend="codex"))
    from test_codex_app_server import Server
    backend.app_server = Server()
    assert await backend.ensure_running() == "ok"
    assert [m.key for m in await backend.list_models()] == ["gpt-test"]
    assert await backend.model_info("gpt-test") == (90000, "vlm", True)
    with pytest.raises(BackendError):
        await backend.load("gpt-test")
    with pytest.raises(BackendError):
        make_backend(Settings(backend="claude"))
    with pytest.raises(mt.ProviderError, match="reasoning effort"):
        await mt.OAuthTransport("codex", models=backend.models).create(
            model="gpt-test", messages=[], extra_body={"reasoning_effort": "ultra"}
        )
