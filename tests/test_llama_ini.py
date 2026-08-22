"""The generated preset ini — the router's whole model world.

Schema facts verified live against cuda:b9360 (2026-08-21 spike): section
name = model id; keys are long-option names without --; booleans are written
`key = true/false` and the ROUTER emits --flag / --no-flag argv itself;
value flags pass through. Deterministic output so this file can diff it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import llm_backend
from llm_backend import IniUnexpressible, ModelRow, write_preset_ini
from settings import Settings

ALL_FLAGS = frozenset(llm_backend.FLAG_FOR.values())


def _row(key: str, path: str) -> ModelRow:
    return ModelRow(key=key, path=path, source="custom")


def _settings(cfg: dict | None = None) -> Settings:
    s = Settings()
    if cfg:
        s.llama_load_settings = cfg
    return s


@pytest.fixture(autouse=True)
def _full_census(monkeypatch):
    """Census pinned to 'everything supported' unless a test says otherwise —
    these tests are about the GENERATOR, not the installed binary."""
    monkeypatch.setattr(llm_backend, "installed_flags", lambda: ALL_FLAGS)


def test_golden_ini(tmp_path):
    rows = [
        _row("b-model", "C:/models/b.gguf"),
        _row("a-model", "C:/models/a.gguf"),
    ]
    s = _settings({
        "a-model": {
            "ctx": 4096, "ngl": 99, "cache_k": "q8_0",
            "mmap": False, "mlock": True, "flash_attn": "on",
        },
    })
    out = write_preset_ini(rows, s, dest=tmp_path / "t.ini")
    body = out.read_text(encoding="utf-8")
    expected = (
        "[a-model]\n"
        "model = C:/models/a.gguf\n"
        "cache-type-k = q8_0\n"
        "ctx-size = 4096\n"
        "flash-attn = on\n"
        "mlock = true\n"
        "mmap = false\n"
        "n-gpu-layers = 99\n"
        "\n"
        "[b-model]\n"
        "model = C:/models/b.gguf\n"
    )
    assert expected in body
    # Sections are sorted — a-model precedes b-model regardless of input order.
    assert body.index("[a-model]") < body.index("[b-model]")


def test_deterministic(tmp_path):
    rows = [_row("m", "C:/m.gguf")]
    s = _settings({"m": {"ctx": 2048, "ngl": 10}})
    one = write_preset_ini(rows, s, dest=tmp_path / "1.ini").read_text(encoding="utf-8")
    two = write_preset_ini(rows, s, dest=tmp_path / "2.ini").read_text(encoding="utf-8")
    assert one == two


def test_unknown_cfg_key_is_refused(tmp_path):
    s = _settings({"m": {"warp_drive": 9}})
    with pytest.raises(IniUnexpressible):
        write_preset_ini([_row("m", "C:/m.gguf")], s, dest=tmp_path / "t.ini")


def test_flag_missing_from_build_is_refused(tmp_path, monkeypatch):
    """A cfg key whose flag the INSTALLED build lacks must refuse by name —
    writing it would surface as a mystery worker crash at load time."""
    monkeypatch.setattr(
        llm_backend, "installed_flags", lambda: ALL_FLAGS - {"kv-unified"}
    )
    s = _settings({"m": {"kv_unified": True}})
    with pytest.raises(IniUnexpressible) as exc:
        write_preset_ini([_row("m", "C:/m.gguf")], s, dest=tmp_path / "t.ini")
    assert exc.value.key == "kv_unified"


def test_none_values_are_omitted(tmp_path):
    s = _settings({"m": {"ctx": None, "ngl": 5}})
    body = write_preset_ini(
        [_row("m", "C:/m.gguf")], s, dest=tmp_path / "t.ini"
    ).read_text(encoding="utf-8")
    assert "ctx-size" not in body and "n-gpu-layers = 5" in body
