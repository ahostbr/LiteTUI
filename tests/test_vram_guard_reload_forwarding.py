"""vram_guard forwards a reload flag to admission; a reload is not a fresh load.

Fake admission context — NO backend server, NO model load. Proves:
- vram_guard reaches admission with reload=True on the reload path and False on a
  fresh load, and never runs the guarded body when admission refuses;
- the actual apply_load_settings callsites carry reload=True — LlamaCpp directly,
  LM Studio threaded through load's `_reload` — with the sync workers stubbed so
  nothing touches a real engine.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from litetui.llm_backend import LlamaCppBackend, LMStudioBackend, _VramGate
from litetui.model_resource_session import AdmissionBlocked


class _Recorder:
    """A stand-in for ModelResourceSession.load: records (key, reload)."""

    def __init__(self, refuse: bool = False):
        self.calls = []
        self.refuse = refuse

    def __call__(self, key, *, reload=False):
        self.calls.append((key, reload))
        outer = self

        @asynccontextmanager
        async def cm():
            if outer.refuse:
                raise AdmissionBlocked("refused")
            yield

        return cm()


def _bare(cls, admission):
    # Skip __init__: vram_guard only needs the two installed hook attributes,
    # so no real backend construction (and no connection) happens.
    backend = object.__new__(cls)
    backend.vram_gate = None
    backend.resource_admission = admission
    return backend


@pytest.mark.asyncio
async def test_vram_guard_forwards_reload_true_and_false():
    rec = _Recorder()
    backend = _bare(_VramGate, rec)
    async with backend.vram_guard("m", reload=True):
        pass
    async with backend.vram_guard("m"):
        pass
    assert rec.calls == [("m", True), ("m", False)]


@pytest.mark.asyncio
async def test_refusal_runs_no_guarded_body():
    rec = _Recorder(refuse=True)
    backend = _bare(_VramGate, rec)
    ran = []
    with pytest.raises(AdmissionBlocked):
        async with backend.vram_guard("m", reload=True):
            ran.append(True)
    assert ran == []
    assert rec.calls == [("m", True)]


@pytest.mark.asyncio
async def test_llama_apply_load_settings_admits_as_reload(monkeypatch):
    rec = _Recorder()
    backend = _bare(LlamaCppBackend, rec)
    monkeypatch.setattr(backend, "_apply_sync", lambda *a, **k: None)
    await backend.apply_load_settings("m", {"ctx": 4096})
    assert rec.calls == [("m", True)]


@pytest.mark.asyncio
async def test_llama_fresh_load_admits_as_load(monkeypatch):
    rec = _Recorder()
    backend = _bare(LlamaCppBackend, rec)
    monkeypatch.setattr(backend, "_load_sync", lambda *a, **k: None)
    await backend.load("m")   # no ctx -> _load_sync path, not the reload delegate
    assert rec.calls == [("m", False)]


@pytest.mark.asyncio
async def test_lmstudio_apply_threads_reload_into_load(monkeypatch):
    backend = object.__new__(LMStudioBackend)
    seen = {}

    async def _fake_load(key, *, ctx=None, notice=None, _reload=False):
        seen.update(key=key, ctx=ctx, reload=_reload)

    monkeypatch.setattr(backend, "load", _fake_load)
    await backend.apply_load_settings("m", {"ctx": 4096})
    assert seen == {"key": "m", "ctx": 4096, "reload": True}


@pytest.mark.asyncio
async def test_lmstudio_load_reload_flag_reaches_admission(monkeypatch):
    rec = _Recorder()
    backend = _bare(LMStudioBackend, rec)
    monkeypatch.setattr(backend, "_sdk", lambda: type("L", (), {"load": lambda *a, **k: None})())
    await backend.load("m", _reload=True)
    await backend.load("m")
    assert rec.calls == [("m", True), ("m", False)]
