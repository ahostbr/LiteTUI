"""Request-level LM Studio JIT guard in model_transport (WS3).

Fake client/opener spies, real backend objects (no __init__, no engine, no HTTP).
Proves a LOCAL LM Studio inference is refused BEFORE any request (nonstream AND
stream AND sync sidecall => zero HTTP), that a trusted-remote LM Studio and other
backends pass, an unknown-locality LM Studio blocks, and a standalone
OpenAITransport(client) stays compatible.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from litetui.model_transport import OpenAITransport, complete_sidecall, _refuse_unsupported_local_lm
from litetui.model_resource_session import AdmissionBlocked
from litetui.llm_backend import LMStudioBackend, LlamaCppBackend


def _bare(cls, **attrs):
    backend = object.__new__(cls)
    for key, value in attrs.items():
        setattr(backend, key, value)
    return backend


def _lmstudio(host="http://127.0.0.1:1234"):
    return _bare(LMStudioBackend, _host=host)


def _llama(host="http://127.0.0.1:7470"):
    return _bare(LlamaCppBackend, _host=host, _attached_host=None)


class _SpyClient:
    def __init__(self):
        self.calls = []

        async def _create(**kwargs):
            self.calls.append(kwargs)
            return "RESP"

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_local_lmstudio_refused_before_any_http(stream):
    spy = _SpyClient()
    transport = OpenAITransport(spy, backend=_lmstudio("http://127.0.0.1:1234"))
    with pytest.raises(AdmissionBlocked):
        await transport.create(model="m", messages=[], stream=stream)
    assert spy.calls == []   # no request sent


@pytest.mark.asyncio
async def test_unknown_locality_lmstudio_blocks(spy=None):
    spy = _SpyClient()
    transport = OpenAITransport(spy, backend=_lmstudio("http://10.0.0.5:1234"))  # non-loopback, no marker
    with pytest.raises(AdmissionBlocked):
        await transport.create(model="m", messages=[])
    assert spy.calls == []


@pytest.mark.asyncio
async def test_explicit_remote_lmstudio_passes():
    spy = _SpyClient()
    transport = OpenAITransport(spy, backend=_lmstudio("http://10.0.0.5:1234"),
                                remote_marker=lambda b: True)
    await transport.create(model="m", messages=[])
    assert len(spy.calls) == 1


@pytest.mark.asyncio
async def test_llama_local_is_not_request_jit_and_passes():
    spy = _SpyClient()
    transport = OpenAITransport(spy, backend=_llama("http://127.0.0.1:7470"))
    await transport.create(model="m", messages=[])
    assert len(spy.calls) == 1


@pytest.mark.asyncio
async def test_standalone_transport_without_backend_passes():
    spy = _SpyClient()
    transport = OpenAITransport(spy)   # compatibility: no backend, no guard
    await transport.create(model="m", messages=[])
    assert len(spy.calls) == 1


def test_sidecall_local_lmstudio_refused_before_urllib():
    called = []

    def opener(req, timeout=None):
        called.append(req)
        raise AssertionError("urllib must not be reached on a blocked local LM Studio sidecall")

    app = SimpleNamespace(backend=_lmstudio("http://127.0.0.1:1234"),
                          settings=SimpleNamespace(lm_host="http://127.0.0.1:1234"))
    with pytest.raises(AdmissionBlocked):
        complete_sidecall(app, {"model": "m", "messages": []}, opener=opener)
    assert called == []


def test_guard_ignores_non_lmstudio_and_missing_backend():
    _refuse_unsupported_local_lm(None)                          # no backend -> no-op
    _refuse_unsupported_local_lm(_llama("http://127.0.0.1:7470"))  # llama -> no-op
    with pytest.raises(AdmissionBlocked):
        _refuse_unsupported_local_lm(_lmstudio("http://127.0.0.1:1234"))


# ── stale client<->backend pair refusal in for_app ──────────────────────────

from litetui import model_transport  # noqa: E402


def _app(backend, endpoint):
    # A fake owner exposing exactly what for_app reads. object() stands in for the
    # AsyncOpenAI client — for_app must refuse a stale pair BEFORE building a transport.
    return SimpleNamespace(client=object(), backend=backend, _client_endpoint=endpoint)


def test_matched_pair_returns_transport():
    backend = _llama("http://127.0.0.1:7470")
    transport = model_transport.for_app(_app(backend, backend.base_url().rstrip("/")))
    assert isinstance(transport, OpenAITransport)


def test_backend_swap_without_client_rebuild_refuses_no_http():
    lm = _lmstudio("http://127.0.0.1:1234")
    app = _app(lm, lm.base_url().rstrip("/"))          # client bound to the LM endpoint
    app.backend = _llama("http://127.0.0.1:7470")      # swapped, client NOT rebuilt
    with pytest.raises(AdmissionBlocked):
        model_transport.for_app(app)                   # refuses -> no transport, no request


def test_backend_endpoint_mutation_refuses():
    backend = _lmstudio("http://127.0.0.1:1234")
    app = _app(backend, backend.base_url().rstrip("/"))
    backend._host = "http://127.0.0.1:9999"            # backend moved its endpoint
    with pytest.raises(AdmissionBlocked):
        model_transport.for_app(app)


@pytest.mark.asyncio
async def test_rebuilt_pair_works_again_then_lm_still_jit_blocked():
    app = _app(_llama("http://127.0.0.1:7470"), None)
    app.backend = _lmstudio("http://127.0.0.1:1234")
    app._client_endpoint = app.backend.base_url().rstrip("/")   # rebuilt/rebound
    transport = model_transport.for_app(app)                    # pair matched again
    assert isinstance(transport, OpenAITransport)
    transport.client = _SpyClient()                             # a client that would send
    with pytest.raises(AdmissionBlocked):                       # ...but LM is still JIT-blocked
        await transport.create(model="m", messages=[])
    assert transport.client.calls == []
