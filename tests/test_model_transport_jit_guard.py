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
