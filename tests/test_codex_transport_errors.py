"""Codex failure reasons and bounded PRE-OUTPUT retries; no real HTTP/auth."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from test_oauth_transport import auth_file

from litetui import model_transport as mt
from litetui import runtime_log


def sse(*events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()


DONE = {"type": "response.completed", "response": {"usage": {}, "output": []}}
TEXT = {"type": "response.output_text.delta", "delta": "hello"}


def transport(tmp_path, handler):
    return mt.OAuthTransport("codex", credential_path=auth_file(tmp_path),
                             http_transport=httpx.MockTransport(handler))


@pytest.fixture
def diagnostics(monkeypatch):
    records = []
    monkeypatch.setattr(runtime_log, "record_error", lambda event, **kw: records.append((event, kw)))
    return records


@pytest.fixture
def pauses(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(mt.asyncio, "sleep", sleep)
    return delays


@pytest.mark.asyncio
@pytest.mark.parametrize("event,details", [
    ({"type": "error", "code": "server_error", "message": "backend unavailable"},
     ["server_error", "backend unavailable"]),
    ({"type": "response.failed", "response": {"error": {"code": "context_length_exceeded", "message": "context too long"}}},
     ["context_length_exceeded", "context too long"]),
    ({"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}},
     ["max_output_tokens"]),
    ({"type": "response.incomplete", "response": {"incomplete_details": {"reason": "content_filter"}}},
     ["content_filter"]),
])
async def test_failure_retains_reason_in_error_and_log(tmp_path, diagnostics, event, details):
    client = transport(tmp_path, lambda r: httpx.Response(200, content=sse(event)))
    with pytest.raises(mt.ProviderError) as exc:
        await client.create(model="gpt-test", messages=[])
    for detail in [event["type"], *details]:
        assert detail in str(exc.value)
        assert detail in str(diagnostics)
    assert "Retry or reduce context" not in str(exc.value)


@pytest.mark.asyncio
async def test_failure_does_not_dump_event_or_credentials(tmp_path, diagnostics):
    auth = auth_file(tmp_path)
    secret = json.loads(auth.read_text())["tokens"]["access_token"]
    event = {"type": "response.failed", "response": {
        "input": "PRIVATE PROMPT BODY", "output": [{"text": "PRIVATE OUTPUT BODY"}],
        "error": {"code": "server_error", "message": f"failure {secret} password=HIDDEN"}}}
    client = mt.OAuthTransport("codex", credential_path=auth,
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200, content=sse(event))))
    with pytest.raises(mt.ProviderError) as exc:
        await client.create(model="gpt-test", messages=[])
    combined = str(exc.value) + str(diagnostics)
    assert "server_error" in combined and "[redacted]" in combined
    assert all(value not in combined for value in (secret, "PRIVATE PROMPT BODY", "PRIVATE OUTPUT BODY", "HIDDEN"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_transient_status_retries_then_success(tmp_path, pauses, status):
    requests, responses = [], []

    def handle(request):
        requests.append(request)
        response = httpx.Response(status if len(requests) == 1 else 200,
                                  content=b"PRIVATE HTTP BODY" if len(requests) == 1 else sse(TEXT, DONE))
        responses.append(response)
        return response

    stream = await transport(tmp_path, handle).create(model="gpt-test", messages=[], stream=True)
    chunks = [chunk async for chunk in stream]
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert [chunk.choices[0].delta.content for chunk in chunks if chunk.choices[0].delta.content] == ["hello"]
    assert len(pauses) == 1 and 0 < pauses[0] <= 2
    assert all(response.is_closed for response in responses)
    assert stream.client.is_closed


@pytest.mark.asyncio
async def test_exhausted_503_is_honest_bounded_and_logged(tmp_path, pauses, diagnostics):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(503, text="PRIVATE HTTP BODY")

    with pytest.raises(mt.ProviderError, match=r"Codex server error \(HTTP 503\), retried 2 times") as exc:
        await transport(tmp_path, handle).create(model="gpt-test", messages=[])
    assert count == 3 and len(pauses) == 2
    assert "account access" not in str(exc.value)
    assert "HTTP 503" in str(diagnostics) and "PRIVATE HTTP BODY" not in str(diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 400, 501])
async def test_nontransient_status_never_retries(tmp_path, pauses, status):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(status, text="PRIVATE BODY")

    with pytest.raises(mt.ProviderError):
        await transport(tmp_path, handle).create(model="gpt-test", messages=[])
    assert count == 1 and pauses == []


@pytest.mark.asyncio
async def test_connection_reset_before_headers_retries(tmp_path, pauses):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ReadError("PRIVATE RESET DETAILS", request=request)
        return httpx.Response(200, content=sse(TEXT, DONE))

    result = await transport(tmp_path, handle).create(model="gpt-test", messages=[])
    assert result.choices[0].message.content == "hello"
    assert count == 2 and len(pauses) == 1


class ResetStream(httpx.AsyncByteStream):
    def __init__(self, before=()):
        self.before = before
        self.closed = False

    async def __aiter__(self):
        if self.before:
            yield sse(*self.before)
        raise httpx.ReadError("PRIVATE RESET DETAILS")

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_connection_reset_after_headers_but_before_output_retries(tmp_path, pauses):
    failed = ResetStream(({"type": "response.created"},))
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(200, stream=failed) if count == 1 else httpx.Response(200, content=sse(TEXT, DONE))

    result = await transport(tmp_path, handle).create(model="gpt-test", messages=[])
    assert result.choices[0].message.content == "hello"
    assert count == 2 and len(pauses) == 1 and failed.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [TEXT,
    {"type": "response.reasoning_summary_text.delta", "delta": "thinking"},
    {"type": "response.output_item.added", "output_index": 0,
     "item": {"type": "function_call", "call_id": "call1", "name": "write", "arguments": "{}"}},
])
async def test_never_replays_after_any_output(tmp_path, pauses, diagnostics, event):
    failed = ResetStream((event,))
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=failed)

    stream = await transport(tmp_path, handle).create(model="gpt-test", messages=[], stream=True)
    chunks = []
    with pytest.raises(mt.ProviderError, match="interrupted"):
        async for chunk in stream:
            chunks.append(chunk)
    assert len(chunks) == 1 and len(requests) == 1
    assert pauses == [] and failed.closed and stream.client.is_closed
    assert "PRIVATE RESET DETAILS" not in str(diagnostics)


@pytest.mark.asyncio
async def test_midstream_failure_reason_no_retry(tmp_path, pauses, diagnostics):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(200, content=sse(TEXT, {
            "type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}))

    with pytest.raises(mt.ProviderError, match="max_output_tokens"):
        await transport(tmp_path, handle).create(model="gpt-test", messages=[])
    assert count == 1 and not pauses
    assert "max_output_tokens" in str(diagnostics)


@pytest.mark.asyncio
async def test_cancel_during_backoff_closes_client(tmp_path, monkeypatch):
    sleeping = asyncio.Event()
    original_sleep = asyncio.sleep
    clients = []
    original_client = httpx.AsyncClient

    def client(**kwargs):
        value = original_client(**kwargs)
        clients.append(value)
        return value

    async def sleep(delay):
        sleeping.set()
        await original_sleep(60)

    monkeypatch.setattr(mt.httpx, "AsyncClient", client)
    monkeypatch.setattr(mt.asyncio, "sleep", sleep)
    task = asyncio.create_task(transport(tmp_path, lambda r: httpx.Response(503)).create(model="gpt-test", messages=[]))
    # If it fails instead of entering backoff, expose the wrong exception.
    ready = asyncio.create_task(sleeping.wait())
    done, _ = await asyncio.wait((task, ready), timeout=2, return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        ready.cancel()
        await task
    assert sleeping.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ready
    assert clients[0].is_closed



@pytest.mark.asyncio
async def test_retry_budget_shared_between_status_headers_and_body(tmp_path, pauses, diagnostics):
    count = 0
    failed = ResetStream()

    def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(503)
        if count == 2:
            return httpx.Response(200, stream=failed)
        raise httpx.ReadError("private reset", request=request)

    with pytest.raises(mt.ProviderError, match="retried 2 times"):
        await transport(tmp_path, handle).create(model="gpt-test", messages=[])
    assert count == 3 and pauses == [.5, 1.] and failed.closed


@pytest.mark.asyncio
async def test_auth_refresh_does_not_consume_or_reset_transient_budget(tmp_path, pauses):
    from test_oauth_transport import token

    path = auth_file(tmp_path)
    requests = []
    failed = ResetStream()

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        if len(requests) == 2:
            obj = json.loads(path.read_text())
            import time
            obj["tokens"]["access_token"] = token(time.time() + 7200)
            path.write_text(json.dumps(obj))
            return httpx.Response(401)
        if len(requests) == 3:
            return httpx.Response(200, stream=failed)
        return httpx.Response(200, content=sse(TEXT, DONE))

    client = mt.OAuthTransport("codex", credential_path=path,
                              http_transport=httpx.MockTransport(handle))
    result = await client.create(model="gpt-test", messages=[])
    assert result.choices[0].message.content == "hello"
    assert len(requests) == 4 and pauses == [.5, 1.]
    assert requests[0].headers["authorization"] == requests[1].headers["authorization"]
    assert requests[1].headers["authorization"] != requests[2].headers["authorization"]
    assert len({req.content for req in requests}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type,retry", [
    (httpx.ConnectError, True), (httpx.WriteError, True),
    (httpx.RemoteProtocolError, True), (httpx.ReadTimeout, False),
    (httpx.ConnectTimeout, False),
])
async def test_connection_retry_allowlist(tmp_path, pauses, error_type, retry):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            raise error_type("PRIVATE NETWORK DETAILS", request=request)
        return httpx.Response(200, content=sse(DONE))

    client = transport(tmp_path, handle)
    if retry:
        await client.create(model="gpt-test", messages=[])
        assert count == 2 and len(pauses) == 1
    else:
        with pytest.raises(mt.ProviderError):
            await client.create(model="gpt-test", messages=[])
        assert count == 1 and not pauses


@pytest.mark.asyncio
async def test_partial_sse_before_first_chunk_is_discarded_on_retry(tmp_path, pauses):
    class PartialStream(ResetStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"par'
            raise httpx.ReadError("reset")

    failed = PartialStream()
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(200, stream=failed) if count == 1 else httpx.Response(200, content=sse(TEXT, DONE))

    stream = await transport(tmp_path, handle).create(model="gpt-test", messages=[], stream=True)
    result = await mt.collect(stream)
    assert result.choices[0].message.content == "hello"
    assert count == 2 and failed.closed


@pytest.mark.asyncio
async def test_usage_chunk_also_closes_retry_window(tmp_path, pauses):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(200, stream=ResetStream((DONE,)))

    with pytest.raises(mt.ProviderError, match="interrupted"):
        await transport(tmp_path, handle).create(model="gpt-test", messages=[])
    assert count == 1 and not pauses


@pytest.mark.asyncio
async def test_sse_failure_before_output_is_not_retried(tmp_path, pauses, diagnostics):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(200, content=sse({"type": "error", "code": "server_error", "message": "unavailable"}))

    with pytest.raises(mt.ProviderError, match="server_error"):
        await transport(tmp_path, handle).create(model="gpt-test", messages=[])
    assert count == 1 and not pauses
    assert len(diagnostics) == 1


@pytest.mark.asyncio
async def test_untrusted_error_fields_are_bounded_and_single_line(tmp_path, diagnostics):
    event = {"type": "response.failed", "response": {"error": {
        "code": {"private": "NOT A CODE"},
        "message": "\x1b[31mline1\nline2\x00" + "x" * 5000,
    }}}
    with pytest.raises(mt.ProviderError) as exc:
        await transport(tmp_path, lambda r: httpx.Response(200, content=sse(event))).create(model="gpt-test", messages=[])
    text = str(exc.value)
    assert len(text) < 650 and "line1 line2" in text
    assert "NOT A CODE" not in text
    assert not any(ord(char) < 32 for char in text)
    assert str(diagnostics[0][1]["detail"]) == text


@pytest.mark.asyncio
async def test_cancel_during_send_closes_client_without_retry(tmp_path, monkeypatch):
    sending = asyncio.Event()
    clients = []
    original_client = httpx.AsyncClient

    def client(**kwargs):
        value = original_client(**kwargs)
        clients.append(value)
        return value

    async def handle(request):
        sending.set()
        await asyncio.sleep(60)
        return httpx.Response(200, content=sse(DONE))

    monkeypatch.setattr(mt.httpx, "AsyncClient", client)
    task = asyncio.create_task(transport(tmp_path, handle).create(model="gpt-test", messages=[]))
    await sending.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_consumer_explicit_close_releases_partial_stream(tmp_path):
    failed = ResetStream((TEXT,))
    stream = await transport(tmp_path, lambda r: httpx.Response(200, stream=failed)).create(model="gpt-test", messages=[], stream=True)
    iterator = stream.__aiter__()
    chunk = await anext(iterator)
    assert chunk.choices[0].delta.content == "hello"
    await stream.close()
    await iterator.aclose()
    assert failed.closed and stream.client.is_closed



@pytest.mark.asyncio
async def test_cleanup_error_cannot_replay_or_replace_terminal_reason(tmp_path, pauses, diagnostics):
    class CloseFailure(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse({"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}})

        async def aclose(self):
            raise httpx.ReadError("private cleanup error")

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=CloseFailure())

    stream = await transport(tmp_path, handle).create(model="gpt-test", messages=[], stream=True)
    with pytest.raises(mt.ProviderError, match="max_output_tokens"):
        await mt.collect(stream)
    assert len(requests) == 1 and not pauses
    assert stream.client.is_closed
    assert "private cleanup error" not in str(diagnostics)


@pytest.mark.asyncio
async def test_explicit_close_failure_still_closes_client(tmp_path, diagnostics):
    class CloseFailure(ResetStream):
        async def aclose(self):
            raise httpx.ReadError("private cleanup error")

    stream = await transport(tmp_path, lambda r: httpx.Response(200, stream=CloseFailure())).create(model="gpt-test", messages=[], stream=True)
    with pytest.raises(mt.ProviderError, match="close"):
        await stream.close()
    assert stream.client.is_closed



@pytest.mark.asyncio
@pytest.mark.parametrize("with_output", [False, True])
async def test_cancel_during_body_closes_without_retry(tmp_path, with_output):
    waiting = asyncio.Event()

    class SlowStream(ResetStream):
        async def __aiter__(self):
            if with_output:
                yield sse(TEXT)
            waiting.set()
            await asyncio.sleep(60)
            yield b""

    response = SlowStream()
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=response)

    stream = await transport(tmp_path, handle).create(model="gpt-test", messages=[], stream=True)
    task = asyncio.create_task(mt.collect(stream))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert response.closed and stream.client.is_closed and len(requests) == 1


@pytest.mark.asyncio
async def test_cancel_during_body_retry_backoff(tmp_path, monkeypatch):
    sleeping = asyncio.Event()
    original_sleep = asyncio.sleep

    async def sleep(delay):
        sleeping.set()
        await original_sleep(60)

    failed = ResetStream()
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=failed)

    monkeypatch.setattr(mt.asyncio, "sleep", sleep)
    stream = await transport(tmp_path, handle).create(model="gpt-test", messages=[], stream=True)
    task = asyncio.create_task(mt.collect(stream))
    await sleeping.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert failed.closed and stream.client.is_closed and len(requests) == 1


@pytest.mark.asyncio
async def test_auth_refresh_once_and_scrubs_old_and_new_credentials(tmp_path, diagnostics):
    import time

    from test_oauth_transport import token

    path = auth_file(tmp_path)
    old = json.loads(path.read_text())["tokens"]["access_token"]
    new = token(time.time() + 7200)
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            obj = json.loads(path.read_text())
            obj["tokens"] = {"access_token": new, "account_id": "second-account"}
            path.write_text(json.dumps(obj))
            return httpx.Response(401)
        return httpx.Response(200, content=sse({"type": "error", "code": "failure",
            "message": f"bad {old} {new} test-account second-account"}))

    client = mt.OAuthTransport("codex", credential_path=path, http_transport=httpx.MockTransport(handle))
    with pytest.raises(mt.ProviderError) as exc:
        await client.create(model="gpt-test", messages=[])
    combined = str(exc.value) + str(diagnostics)
    assert len(requests) == 2
    assert all(value not in combined for value in (old, new, "test-account", "second-account"))


@pytest.mark.asyncio
async def test_second_401_does_not_refresh_again(tmp_path, pauses):
    import time

    from test_oauth_transport import token

    path = auth_file(tmp_path)
    requests = []

    def handle(request):
        requests.append(request)
        obj = json.loads(path.read_text())
        obj["tokens"]["access_token"] = token(time.time() + 7200 * len(requests))
        path.write_text(json.dumps(obj))
        return httpx.Response(401)

    client = mt.OAuthTransport("codex", credential_path=path, http_transport=httpx.MockTransport(handle))
    with pytest.raises(mt.ProviderError, match="codex login"):
        await client.create(model="gpt-test", messages=[])
    assert len(requests) == 2 and not pauses


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [
    (403, "Codex refused the request (HTTP 403). Check account access and the selected model."),
    (429, "Codex usage limit reached. Wait and retry; no fallback was used."),
])
async def test_access_and_limit_messages_unchanged(tmp_path, pauses, status, expected):
    response = httpx.Response(status, text="PRIVATE BODY")
    with pytest.raises(mt.ProviderError) as exc:
        await transport(tmp_path, lambda r: response).create(model="gpt-test", messages=[])
    assert str(exc.value) == expected and response.is_closed and not pauses


@pytest.mark.asyncio
async def test_each_request_has_fresh_budget_without_replaying_prior_tool_outputs(tmp_path, pauses):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(503) if len(requests) % 2 else httpx.Response(200, content=sse(DONE))

    client = transport(tmp_path, handle)
    messages = [{"role": "assistant", "tool_calls": [{"id": "already-ran", "function": {"name": "write", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "already-ran", "content": "completed earlier"}]
    for _ in range(2):
        await client.create(model="gpt-test", messages=messages)
    assert len(requests) == 4 and pauses == [.5, .5]
    assert all(request == requests[0] for request in requests)
    assert requests[0]["input"][-1]["output"] == "completed earlier"



@pytest.mark.asyncio
async def test_reason_is_written_to_runtime_error_file(tmp_path, monkeypatch):
    error_path = tmp_path / "runtime-errors.log"
    recorder = runtime_log.RuntimeRecorder(tmp_path / "runtime.jsonl", errors_path=error_path)
    monkeypatch.setattr(runtime_log, "_ACTIVE", recorder)
    try:
        event = {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}
        with pytest.raises(mt.ProviderError, match="max_output_tokens"):
            await transport(tmp_path, lambda r: httpx.Response(200, content=sse(event))).create(model="gpt-test", messages=[])
    finally:
        recorder.close()
    body = error_path.read_text(encoding="utf-8")
    assert "oauth.request_failed" in body
    assert "response.incomplete" in body and "max_output_tokens" in body



@pytest.mark.asyncio
async def test_client_cleanup_does_not_replace_503_reason(tmp_path, pauses, diagnostics):
    class CloseFailureTransport(httpx.MockTransport):
        async def aclose(self):
            raise httpx.ReadError("private cleanup failure")

    client = mt.OAuthTransport("codex", credential_path=auth_file(tmp_path),
        http_transport=CloseFailureTransport(lambda r: httpx.Response(503)))
    with pytest.raises(mt.ProviderError, match=r"HTTP 503.*retried 2 times"):
        await client.create(model="gpt-test", messages=[])
    assert "private cleanup failure" not in str(diagnostics)
