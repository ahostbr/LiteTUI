"""Locality-classified admission installer — the wiring the app.backend setter uses.

Real backend objects (constructed without __init__, no engine), a real
ModelResourceSession over a temp store with the default (None) resolver, and a
fake app. No engine, no model load, no App boot. Proves the installed admission:
- blocks a loopback (LOCAL) uncalibrated load with no guarded body run;
- STILL blocks an attached-but-loopback load (locality, not ownership);
- passes an explicitly-marked remote endpoint through (no coverage claim);
- blocks unknown locality and an unknown backend shape;
- leaves a non-_VramGate (codex) untouched;
- is idempotent and retains an already-installed session;
- forwards the reload flag and does not double-reserve under the nested guard.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from litetui import resource_admission_install as inst
from litetui.resource_admission_install import classify_locality, install_on
from litetui.llm_backend import LlamaCppBackend, LMStudioBackend, _VramGate
from litetui.ninfer_backend import NInferBackend
from litetui.model_resource_session import AdmissionBlocked, ModelResourceSession
from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot


def _bare(cls, **attrs):
    backend = object.__new__(cls)
    backend.vram_gate = None
    backend.resource_admission = None
    for key, value in attrs.items():
        setattr(backend, key, value)
    return backend


def _llama(host="http://127.0.0.1:7470", attached=None):
    return _bare(LlamaCppBackend, _host=host, _attached_host=attached)


def _lmstudio(host="http://127.0.0.1:1234"):
    return _bare(LMStudioBackend, _host=host)


def _ninfer(host=None):
    return _bare(NInferBackend, _host=host)


def _session(tmp_path, demand_for=lambda key: None):
    coord = ResourceCoordinator(
        tmp_path / "r.sqlite3",
        telemetry=lambda: ResourceSnapshot(time.time(), 10**12, {"GPU-A": 10**12}, True),
    )
    return ModelResourceSession(coord, "owner-inst", demand_for=demand_for)


def _app(session):
    return SimpleNamespace(_resource_session=session)


class _RecorderSession:
    def __init__(self):
        self.calls = []

    def load(self, key, *, reload=False):
        self.calls.append((key, reload))

        @asynccontextmanager
        async def cm():
            yield

        return cm()


# ── classification (pure) ───────────────────────────────────────────────────

def test_loopback_endpoints_are_local():
    assert classify_locality(_llama("http://127.0.0.1:7470")) == "local"
    assert classify_locality(_lmstudio("http://localhost:1234")) == "local"
    assert classify_locality(_llama("http://[::1]:7470")) == "local"


def test_ninfer_pre_spawn_no_host_is_local():
    assert classify_locality(_ninfer(host=None)) == "local"


def test_attached_but_loopback_is_still_local():
    assert classify_locality(_llama(attached="http://127.0.0.1:7470")) == "local"


def test_non_loopback_without_marker_is_unknown():
    assert classify_locality(_llama("http://10.0.0.5:7470")) == "unknown"
    assert classify_locality(_lmstudio("http://192.168.1.9:1234")) == "unknown"


def test_explicit_remote_marker_passes():
    assert classify_locality(_llama("http://10.0.0.5:7470"), remote_marker=lambda b: True) == "remote"


def test_unknown_backend_shape_is_unknown():
    class Weird(_VramGate):
        pass
    assert classify_locality(_bare(Weird)) == "unknown"


# ── installed admission behaviour (real backend + real session) ─────────────

@pytest.mark.asyncio
async def test_local_uncalibrated_blocks_with_no_body(tmp_path):
    backend = _llama()
    assert install_on(_app(_session(tmp_path)), backend) is True
    ran = []
    with pytest.raises(AdmissionBlocked):
        async with backend.vram_guard("m"):
            ran.append(True)
    assert ran == []


@pytest.mark.asyncio
async def test_attached_local_still_blocks(tmp_path):
    backend = _llama(attached="http://127.0.0.1:7470")
    install_on(_app(_session(tmp_path)), backend)
    ran = []
    with pytest.raises(AdmissionBlocked):
        async with backend.vram_guard("m"):
            ran.append(True)
    assert ran == []


@pytest.mark.asyncio
async def test_explicit_remote_passes_through(tmp_path):
    backend = _llama("http://10.0.0.5:7470")
    install_on(_app(_session(tmp_path)), backend, remote_marker=lambda b: True)
    ran = []
    async with backend.vram_guard("m"):
        ran.append(True)
    assert ran == [True]


@pytest.mark.asyncio
async def test_unknown_locality_blocks(tmp_path):
    backend = _llama("http://10.0.0.5:7470")
    install_on(_app(_session(tmp_path)), backend)
    ran = []
    with pytest.raises(AdmissionBlocked):
        async with backend.vram_guard("m"):
            ran.append(True)
    assert ran == []


def test_non_vramgate_backend_is_untouched(tmp_path):
    class Codex:
        resource_admission = None
    backend = Codex()
    assert install_on(_app(_session(tmp_path)), backend) is False
    assert backend.resource_admission is None


def test_install_is_idempotent_and_retains_session(tmp_path):
    backend = _llama()
    app = _app(_session(tmp_path))
    assert install_on(app, backend) is True
    first = backend.resource_admission
    assert install_on(app, backend) is False
    assert backend.resource_admission is first


@pytest.mark.asyncio
async def test_reload_flag_forwarded(tmp_path):
    recorder = _RecorderSession()
    backend = _llama()
    install_on(SimpleNamespace(_resource_session=recorder), backend)
    async with backend.vram_guard("m", reload=True):
        pass
    async with backend.vram_guard("m"):
        pass
    assert recorder.calls == [("m", True), ("m", False)]


@pytest.mark.asyncio
async def test_nested_guard_does_not_double_reserve(tmp_path):
    recorder = _RecorderSession()
    backend = _llama()
    install_on(SimpleNamespace(_resource_session=recorder), backend)
    async with backend.vram_guard("m"):
        async with backend.vram_guard("m"):   # reentrant, same task
            pass
    assert recorder.calls == [("m", False)]   # admission entered ONCE
