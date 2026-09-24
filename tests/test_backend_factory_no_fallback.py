"""Explicit backend selection must never silently change provider."""
import pytest
from litetui import gpu_gate, llm_backend
from litetui.settings import Settings


def test_unsupported_ninfer_refuses_without_mutating_requested_settings(monkeypatch):
    settings = Settings(backend="ninfer")
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    with pytest.raises(llm_backend.BackendError, match="NInfer.*RTX 5090"):
        llm_backend.make_backend(settings)
    assert settings.backend == "ninfer"


def test_supported_ninfer_preserves_requested_backend(monkeypatch):
    from litetui import ninfer_backend
    settings = Settings(backend="ninfer")
    sentinel = object()
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: True)
    monkeypatch.setattr(ninfer_backend, "NInferBackend", lambda supplied: sentinel)
    monkeypatch.setattr(llm_backend, "_DEFAULT_VRAM_GATE", None)
    monkeypatch.setattr(llm_backend, "_DEFAULT_LOAD_HOOK", None)
    assert llm_backend.make_backend(settings) is sentinel
    assert settings.backend == "ninfer"
