"""LMStudioBackend — the SDK control plane, with the SDK faked.

What must hold: load passes contextLength through config; unload reaches the
handle; a missing SDK degrades to a NAMED in-band error (never a boot
failure); model_info keeps the ceiling-vs-window contract the app fought
for; every error names the host it is about.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from litetui import llm_backend
from litetui import runtime_log
from litetui.llm_backend import BackendError, LMStudioBackend
from litetui.settings import Settings


class FakeHandle:
    def __init__(self, log):
        self._log = log

    def unload(self):
        self._log.append(("unload",))


class FakeSDK(types.ModuleType):
    def __init__(self):
        super().__init__("lmstudio")
        self.log = []

    def configure_default_client(self, host):
        self.log.append(("configure", host))

    def set_sync_api_timeout(self, s):
        self.log.append(("timeout", s))

    def llm(self, key, config=None):
        self.log.append(("llm", key, config))
        return FakeHandle(self.log)


@pytest.fixture()
def sdk(monkeypatch):
    fake = FakeSDK()
    monkeypatch.setitem(sys.modules, "lmstudio", fake)
    return fake


def _backend() -> LMStudioBackend:
    s = Settings()
    s.lm_host = "http://localhost:1234"
    s.lms_load_timeout_s = 777
    return LMStudioBackend(s)


def test_load_passes_context_length(sdk):
    b = _backend()
    asyncio.run(b.load("qwen", ctx=8192))
    assert ("llm", "qwen", {"contextLength": 8192}) in sdk.log
    assert ("configure", "localhost:1234") in sdk.log
    assert ("timeout", 777) in sdk.log


def test_load_without_ctx_sends_no_config(sdk):
    b = _backend()
    asyncio.run(b.load("qwen"))
    assert ("llm", "qwen", None) in sdk.log


def test_unload_reaches_handle(sdk):
    b = _backend()
    asyncio.run(b.unload("qwen"))
    assert ("unload",) in sdk.log


def test_missing_sdk_is_a_named_error_not_a_crash(monkeypatch):
    monkeypatch.setitem(sys.modules, "lmstudio", None)   # import → ImportError
    b = _backend()
    with pytest.raises(BackendError) as exc:
        asyncio.run(b.load("qwen", ctx=1))
    assert "pip install lmstudio" in str(exc.value)


def test_apply_refuses_what_the_sdk_cannot_drive(sdk):
    b = _backend()
    with pytest.raises(BackendError) as exc:
        asyncio.run(b.apply_load_settings("qwen", {"ctx": 4096, "ngl": 99}))
    assert "ngl" in str(exc.value)
    # And the scriptable subset alone goes through.
    asyncio.run(b.apply_load_settings("qwen", {"ctx": 4096}))
    assert ("llm", "qwen", {"contextLength": 4096}) in sdk.log


def test_model_info_ceiling_vs_window(monkeypatch):
    rows = [
        {"id": "hot", "loaded_context_length": 8192, "max_context_length": 262144, "type": "llm"},
        {"id": "cold", "max_context_length": 262144, "type": "llm"},
    ]
    monkeypatch.setattr(
        llm_backend, "_http_json", lambda url, body=None, timeout=10: {"data": rows}
    )
    b = _backend()
    assert asyncio.run(b.model_info("hot")) == (8192, "llm", True)
    assert asyncio.run(b.model_info("cold")) == (262144, "llm", False)
    assert asyncio.run(b.model_info("ghost")) is None


def test_errors_name_the_host(monkeypatch):
    """The host must still be identifiable — T137 moved it out of the
    user-facing line (no URLs, rule b) and into the error sink."""
    def boom(url, body=None, timeout=10):
        raise OSError("connection refused")

    seen = []
    monkeypatch.setattr(llm_backend, "_http_json", boom)
    monkeypatch.setattr(runtime_log, "record_error", lambda event, **kw: seen.append(kw))
    b = _backend()
    with pytest.raises(BackendError) as exc:
        asyncio.run(b.list_models())
    # Plain words in chat — no URL.
    assert "http://localhost:1234" not in str(exc.value)
    # The host rides in the sink's detail, where raw detail belongs.
    assert any("http://localhost:1234" in d.get("detail", "") for d in seen)
