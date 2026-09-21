"""Locality-classified admission installer — the wiring the app.backend setter uses.

Real backend objects (no __init__, no engine), real per-backend ModelResourceSession
over a temp store, and both a fake owner and the REAL LiteTUI.backend setter. No
engine, no model load, no App boot. Proves classification (incl. error-safety),
per-backend session retention, idempotency, the fail-closed block, and that the
actual property setter wires install.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from litetui.resource_admission_install import classify_locality, install_on, make_admission
from litetui.llm_backend import LlamaCppBackend, LMStudioBackend, _VramGate
from litetui.ninfer_backend import NInferBackend
from litetui.model_resource_session import AdmissionBlocked
from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot
from litetui.resource_calibration import CalibrationStore


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


def _bundle(tmp_path):
    coord = ResourceCoordinator(
        tmp_path / "r.sqlite3",
        telemetry=lambda: ResourceSnapshot(time.time(), 10**12, {"GPU-A": 10**12}, True),
    )
    return (coord, "owner-inst", CalibrationStore(tmp_path / "cal.json"))   # cal absent -> uncalibrated


def _app(tmp_path):
    return SimpleNamespace(_admission_bundle=_bundle(tmp_path))


class _RecorderSession:
    def __init__(self):
        self.calls = []

    def load(self, key, *, reload=False):
        self.calls.append((key, reload))

        @asynccontextmanager
        async def cm():
            yield

        return cm()


# ── classification, incl. error-safety ──────────────────────────────────────

def test_loopback_and_ninfer_prespawn_and_attached_are_local():
    assert classify_locality(_llama("http://127.0.0.1:7470")) == "local"
    assert classify_locality(_lmstudio("http://localhost:1234")) == "local"
    assert classify_locality(_llama("http://[::1]:7470")) == "local"
    assert classify_locality(_ninfer(host=None)) == "local"                 # pre-spawn
    assert classify_locality(_llama(attached="http://127.0.0.1:7470")) == "local"


def test_non_loopback_is_unknown_and_explicit_marker_is_remote():
    assert classify_locality(_llama("http://10.0.0.5:7470")) == "unknown"
    assert classify_locality(_llama("http://10.0.0.5:7470"), remote_marker=lambda b: True) == "remote"


def test_unknown_backend_shape_is_unknown():
    class Weird(_VramGate):
        pass
    assert classify_locality(_bare(Weird)) == "unknown"


def test_classification_errors_fail_closed_to_unknown():
    assert classify_locality(_llama("http://[gg::bad")) == "unknown"        # malformed IPv6
    assert classify_locality(_llama(host=12345)) == "unknown"               # non-string host
    boom = lambda b: (_ for _ in ()).throw(RuntimeError())                  # noqa: E731
    assert classify_locality(_llama("http://10.0.0.5:7470"), remote_marker=boom) == "unknown"


# ── per-backend sessions + idempotency ──────────────────────────────────────

def test_two_backends_get_separate_backend_owned_sessions(tmp_path):
    app = _app(tmp_path)
    a, b = _llama(), _lmstudio()
    assert install_on(app, a) is True and install_on(app, b) is True
    assert a._admission_session is not b._admission_session
    assert [entry[0] for entry in app._admission_sessions] == [a, b]        # registry for cleanup


def test_same_backend_reassign_is_idempotent_and_retains(tmp_path):
    app = _app(tmp_path)
    backend = _llama()
    assert install_on(app, backend) is True
    session = backend._admission_session
    hook = backend.resource_admission
    assert install_on(app, backend) is False
    assert backend._admission_session is session and backend.resource_admission is hook


def test_non_vramgate_backend_is_untouched(tmp_path):
    class Codex:
        resource_admission = None
    backend = Codex()
    assert install_on(_app(tmp_path), backend) is False
    assert backend.resource_admission is None


def test_preinstalled_hook_is_retained(tmp_path):
    backend = _llama()
    sentinel = object()
    backend.resource_admission = sentinel
    assert install_on(_app(tmp_path), backend) is False
    assert backend.resource_admission is sentinel


# ── installed behaviour (real backend + real session) ───────────────────────

@pytest.mark.asyncio
async def test_local_uncalibrated_blocks_with_no_body(tmp_path):
    backend = _llama()
    install_on(_app(tmp_path), backend)
    ran = []
    with pytest.raises(AdmissionBlocked):
        async with backend.vram_guard("m"):
            ran.append(True)
    assert ran == []


@pytest.mark.asyncio
async def test_attached_local_still_blocks(tmp_path):
    backend = _llama(attached="http://127.0.0.1:7470")
    install_on(_app(tmp_path), backend)
    ran = []
    with pytest.raises(AdmissionBlocked):
        async with backend.vram_guard("m"):
            ran.append(True)
    assert ran == []


@pytest.mark.asyncio
async def test_explicit_remote_passes_and_unknown_blocks(tmp_path):
    remote = _llama("http://10.0.0.5:7470")
    install_on(_app(tmp_path), remote, remote_marker=lambda b: True)
    ran = []
    async with remote.vram_guard("m"):
        ran.append(True)
    assert ran == [True]

    unknown = _llama("http://10.0.0.5:7470")
    install_on(_app(tmp_path), unknown)
    ran2 = []
    with pytest.raises(AdmissionBlocked):
        async with unknown.vram_guard("m"):
            ran2.append(True)
    assert ran2 == []


# ── the reload flag and the nested guard (make_admission directly) ──────────

@pytest.mark.asyncio
async def test_reload_flag_forwarded():
    recorder = _RecorderSession()
    backend = _llama()
    backend.resource_admission = make_admission(backend, recorder)
    async with backend.vram_guard("m", reload=True):
        pass
    async with backend.vram_guard("m"):
        pass
    assert recorder.calls == [("m", True), ("m", False)]


@pytest.mark.asyncio
async def test_nested_guard_does_not_double_reserve():
    recorder = _RecorderSession()
    backend = _llama()
    backend.resource_admission = make_admission(backend, recorder)
    async with backend.vram_guard("m"):
        async with backend.vram_guard("m"):
            pass
    assert recorder.calls == [("m", False)]


# ── the REAL LiteTUI.backend setter wires install (no App boot) ─────────────

def test_real_backend_setter_installs_admission(tmp_path):
    from litetui.app import LiteTUI

    class _Owner:
        def __init__(self, bundle):
            self._admission_bundle = bundle
            self.remembered = []

        def _remember_for_this_convo(self, field, value):
            self.remembered.append((field, value))

    owner = _Owner(_bundle(tmp_path))
    backend = _llama()
    LiteTUI.backend.fset(owner, backend)            # invoke the actual property setter
    assert owner._backend is backend
    assert backend.resource_admission is not None
    assert backend._admission_session is not None
