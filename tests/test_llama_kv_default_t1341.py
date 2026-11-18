"""T1341-A3: a model with no KV setting of its own gets q8_0 K + V with flash attention on.

NInfer stores its KV cache as fp8 and Strata as int8; llama.cpp's default is f16 (b9360 and b12078
help: ``-ctk/-ctv ... (default: f16)``). A quantised V cache needs flash attention, so the three
keys travel together. A model with ANY of K cache, V cache or Flash Attention set keeps exactly
what it has (even a value equal to the engine default is a choice). The setting
``llama_default_kv_quant`` turns it off. Synthetic settings; the real --help fixtures stand in for
the census; no llama-server runs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from litetui import llm_backend
from litetui.llm_backend import ModelRow, render_preset_ini
from litetui.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def _census(build: str) -> frozenset[str]:
    return llm_backend.parse_supported_flags(
        (FIXTURES / f"llama_server_help_{build}.txt").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    monkeypatch.setattr(llm_backend, "sibling_mmproj", lambda path: (None, ()))
    monkeypatch.setattr(llm_backend, "configured_build", lambda settings: "cuda:test")


def _model(body: str, key: str = "m") -> dict[str, str]:
    out: dict[str, str] = {}
    inside = False
    for line in body.splitlines():
        if line == f"[{key}]":
            inside = True
        elif line.startswith("["):
            inside = False
        elif inside and " = " in line:
            k, v = line.split(" = ", 1)
            out[k] = v
    return out


def _render(monkeypatch, flags, cfg, *, on: bool = True) -> dict[str, str]:
    monkeypatch.setattr(llm_backend, "installed_flags", lambda *a: flags)
    s = Settings()
    s.llama_default_kv_quant = on
    s.llama_load_settings = {"m": cfg}
    return _model(render_preset_ini([ModelRow("m", "C:/m/m.gguf", "custom")], s))


def test_the_setting_is_on_by_default():
    assert Settings().llama_default_kv_quant is True


@pytest.mark.parametrize("build", ["b9360", "b12078"])
def test_a_model_with_no_kv_setting_gets_q8_0_and_flash_attention(monkeypatch, build):
    sec = _render(monkeypatch, _census(build), {"ctx": 8192})
    assert sec["cache-type-k"] == "q8_0"
    assert sec["cache-type-v"] == "q8_0"
    assert sec["flash-attn"] == "on"
    assert sec["ctx-size"] == "8192"


def test_a_model_with_no_settings_at_all_gets_it_too(monkeypatch):
    sec = _render(monkeypatch, _census("b12078"), {})
    assert (sec["cache-type-k"], sec["cache-type-v"], sec["flash-attn"]) == ("q8_0", "q8_0", "on")


@pytest.mark.parametrize("cfg, expect", [
    ({"cache_k": "f16"}, {"cache-type-k": "f16"}),
    ({"cache_v": "q4_0"}, {"cache-type-v": "q4_0"}),
    ({"flash_attn": "off"}, {"flash-attn": "off"}),
    ({"flash_attn": "auto"}, {"flash-attn": "auto"}),
    ({"cache_k": "q5_1", "cache_v": "q5_1", "flash_attn": "on"},
     {"cache-type-k": "q5_1", "cache-type-v": "q5_1", "flash-attn": "on"}),
])
def test_a_model_with_any_of_the_three_set_keeps_exactly_what_it_has(monkeypatch, cfg, expect):
    sec = _render(monkeypatch, _census("b12078"), cfg)
    kv = {k: v for k, v in sec.items() if k in ("cache-type-k", "cache-type-v", "flash-attn")}
    assert kv == expect


def test_the_setting_off_writes_nothing_for_an_unset_model(monkeypatch):
    sec = _render(monkeypatch, _census("b12078"), {"ctx": 8192}, on=False)
    assert not any(k in sec for k in ("cache-type-k", "cache-type-v", "flash-attn"))


def test_control_a_settings_object_without_the_field_means_not_set(monkeypatch):
    monkeypatch.setattr(llm_backend, "installed_flags", lambda *a: _census("b12078"))

    class Bare:
        llama_load_settings = {"m": {"ctx": 4096}}
        llama_cache_ram_mib = llama_slot_prompt_similarity = llama_cache_idle_slots = None

    sec = _model(render_preset_ini([ModelRow("m", "C:/m/m.gguf", "custom")], Bare()))
    assert "cache-type-k" not in sec


def test_an_empty_census_proves_nothing_so_nothing_is_added(monkeypatch):
    sec = _render(monkeypatch, frozenset(), {"ctx": 8192})
    assert "cache-type-k" not in sec and "flash-attn" not in sec


def test_a_build_missing_any_of_the_three_flags_gets_nothing(monkeypatch):
    for missing in ("cache-type-k", "cache-type-v", "flash-attn"):
        flags = frozenset(f for f in _census("b12078") if f != missing)
        sec = _render(monkeypatch, flags, {"ctx": 8192})
        assert not any(k in sec for k in ("cache-type-k", "cache-type-v", "flash-attn")), missing


def test_the_setting_is_wired_to_the_settings_screen_and_scope():
    from litetui import settings_scope, settings_ui_model

    assert settings_ui_model.FIELD_DESCRIPTIONS["llama_default_kv_quant"]
    assert "llama_default_kv_quant" in settings_scope._RESTART
