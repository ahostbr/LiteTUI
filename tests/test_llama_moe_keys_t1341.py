"""T1341-A4: Load-tab keys for MoE expert placement and the vision projector.

NInfer has ``--expert-residency host`` and Strata has an expert cache; the llama.cpp counterparts
are static: ``-cmoe, --cpu-moe`` (all MoE weights on the CPU) and ``-ncmoe, --n-cpu-moe N`` (the
first N layers'), both printed by b9360 and b12078 (help lines 113-115 / 123-125). Also
``--mmproj-offload, --no-mmproj-offload`` (engine default on). The keys default to unset, so an
existing model's ini is byte-identical.

``--cpu-moe`` has NO ``--no-`` spelling, so ``cpu_moe = False`` is written as nothing. Synthetic
settings only; the real --help fixtures stand in for the census, and no llama-server runs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from litetui import llm_backend
from litetui.llm_backend import IniUnexpressible, ModelRow, render_preset_ini
from litetui.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def _census(build: str) -> frozenset[str]:
    return llm_backend.parse_supported_flags(
        (FIXTURES / f"llama_server_help_{build}.txt").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    monkeypatch.setattr(llm_backend, "sibling_mmproj", lambda path: (None, ()))
    monkeypatch.setattr(llm_backend, "configured_build", lambda settings: "cuda:test")


def _render(monkeypatch, flags, cfg: dict) -> str:
    monkeypatch.setattr(llm_backend, "installed_flags", lambda *a: flags)
    s = Settings()
    s.llama_load_settings = {"m": cfg} if cfg is not None else {}
    return render_preset_ini([ModelRow("m", "C:/m/m.gguf", "custom")], s)


def _model_section(body: str) -> list[str]:
    lines = body.splitlines()
    start = lines.index("[m]") + 1
    out = []
    for line in lines[start:]:
        if line.startswith("["):
            break
        if line:
            out.append(line)
    return out


@pytest.mark.parametrize("build", ["b9360", "b12078"])
def test_the_three_flags_are_in_both_help_fixtures(build):
    flags = _census(build)
    for key in ("cpu_moe", "n_cpu_moe", "mmproj_offload"):
        assert llm_backend.FLAG_FOR[key] in flags, (build, key)


def test_the_help_fixtures_print_no_negative_form_of_cpu_moe():
    for build in ("b9360", "b12078"):
        text = (FIXTURES / f"llama_server_help_{build}.txt").read_text(encoding="utf-8")
        assert "--no-cpu-moe" not in text
        assert "--no-mmproj-offload" in text


@pytest.mark.parametrize("build", ["b9360", "b12078"])
def test_values_become_ini_lines(monkeypatch, build):
    body = _render(monkeypatch, _census(build),
                   {"n_cpu_moe": 20, "cpu_moe": True, "mmproj_offload": False})
    assert _model_section(body) == [
        "model = C:/m/m.gguf", "cpu-moe = true", "mmproj-offload = false", "n-cpu-moe = 20",
    ]


def test_cpu_moe_false_writes_nothing_because_there_is_no_negation(monkeypatch):
    body = _render(monkeypatch, _census("b12078"), {"cpu_moe": False})
    assert "cpu-moe" not in body
    assert _model_section(body) == ["model = C:/m/m.gguf"]


def test_control_unset_keys_leave_the_ini_unchanged(monkeypatch):
    flags = _census("b12078")
    with_keys_unset = _render(monkeypatch, flags, {"ctx": 8192})
    assert _model_section(with_keys_unset) == ["model = C:/m/m.gguf", "ctx-size = 8192"]
    for flag in ("cpu-moe", "n-cpu-moe", "mmproj-offload"):
        assert flag not in with_keys_unset


def test_a_build_without_the_flag_is_refused_loudly_not_dropped(monkeypatch):
    flags = frozenset(f for f in _census("b12078") if f != "n-cpu-moe")
    with pytest.raises(IniUnexpressible):
        _render(monkeypatch, flags, {"n_cpu_moe": 8})


def test_the_screen_shows_n_a_when_the_build_lacks_the_flag():
    flags = frozenset(f for f in _census("b12078") if f != "mmproj-offload")
    assert not llm_backend.load_key_expressible("mmproj_offload", flags)
    assert llm_backend.load_key_expressible("n_cpu_moe", flags)


def test_the_model_screen_lists_the_three_rows():
    from litetui.plugins.model_switch import _LOAD_FIELDS

    keys = [row[0] for row in _LOAD_FIELDS]
    for key in ("n_cpu_moe", "cpu_moe", "mmproj_offload"):
        assert keys.count(key) == 1
    assert {row[0]: row[2] for row in _LOAD_FIELDS}["n_cpu_moe"] == "int"
    assert {row[0]: row[2] for row in _LOAD_FIELDS}["cpu_moe"] == "tri"
