"""T1341-A5: ``ngram-mod`` is an opt-in speculative type (prompt-lookup drafting).

NInfer drafts copy proposals from the context (``--ngram-draft-tokens``, 15 with ``--spec``) and Strata
has ``--suffix-draft 3`` on by default; llama.cpp's counterpart is ``--spec-type ngram-mod`` (both
help fixtures list it). It stays OFF unless the user picks it on the Model screen. It needs no draft
model and has its own ``--spec-ngram-mod-*`` knobs, which are not in the Load vocabulary, so the
engine's defaults apply and the draft-model keys are dropped. Synthetic headers and the real --help
fixtures stand in for the census; no llama-server runs.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from litetui import llm_backend as b
from litetui.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def _help(build: str) -> str:
    return (FIXTURES / f"llama_server_help_{build}.txt").read_text(encoding="utf-8")


def _string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<Q", len(raw)) + raw


def gguf(path: Path, *, layers: int | None = None, arch: str = "qwen35") -> Path:
    pairs = [("general.architecture", 8, _string(arch))]
    if layers is not None:
        pairs.append((f"{arch}.nextn_predict_layers", 4, struct.pack("<I", layers)))
    data = b"GGUF" + struct.pack("<IQQ", 3, 0, len(pairs))
    data += b"".join(_string(k) + struct.pack("<I", t) + v for k, t, v in pairs)
    path.write_bytes(data)
    return path


@pytest.fixture
def census(monkeypatch):
    def use(build: str, *, drop_mode: str | None = None):
        text = _help(build)
        flags = b.parse_supported_flags(text)
        modes = b.parse_spec_types(text) - ({drop_mode} if drop_mode else set())
        monkeypatch.setattr(b, "configured_flags", lambda s: flags)
        monkeypatch.setattr(b, "configured_spec_types", lambda s: modes)
        monkeypatch.setattr(b, "installed_flags", lambda *a: flags)
    monkeypatch.setattr(b, "sibling_mmproj", lambda path: (None, ()))
    monkeypatch.setattr(b, "configured_build", lambda s: "cuda:test")
    return use


def _section(tmp_path, cfg, model: Path) -> dict[str, str]:
    settings = Settings()
    settings.llama_default_kv_quant = False
    settings.llama_load_settings = {"target": cfg}
    row = b.ModelRow("target", str(model), "custom")
    text = b.write_preset_ini([row], settings, tmp_path / "models.ini").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        if line == "[target]":
            inside = True
        elif line.startswith("["):
            inside = False
        elif inside and " = " in line:
            k, v = line.split(" = ", 1)
            out[k] = v
    return out


@pytest.mark.parametrize("build", ["b9360", "b12078"])
def test_both_help_fixtures_advertise_ngram_mod(build):
    assert "ngram-mod" in b.parse_spec_types(_help(build))


@pytest.mark.parametrize("build", ["b9360", "b12078"])
def test_choosing_it_writes_one_spec_type_line_and_no_draft_flags(tmp_path, census, build):
    census(build)
    sec = _section(tmp_path, {"spec_type": "ngram-mod", "draft_max": 8, "draft_min": 2,
                              "draft_p_min": 0.5}, gguf(tmp_path / "m.gguf"))
    assert sec["spec-type"] == "ngram-mod"
    assert not any(k.startswith("spec-draft") for k in sec)


def test_it_is_off_unless_chosen(tmp_path, census):
    census("b12078")
    sec = _section(tmp_path, {"ctx": 8192}, gguf(tmp_path / "m.gguf"))
    assert "spec-type" not in sec


def test_choosing_it_on_an_mtp_gguf_is_not_overridden_by_the_automatic_mtp(tmp_path, census):
    census("b12078")
    sec = _section(tmp_path, {"spec_type": "ngram-mod"}, gguf(tmp_path / "m.gguf", layers=1))
    assert sec["spec-type"] == "ngram-mod"
    assert "spec-draft-n-max" not in sec


def test_a_build_that_does_not_list_it_refuses_loudly(tmp_path, census):
    census("b12078", drop_mode="ngram-mod")
    with pytest.raises(b.BackendError, match="ngram-mod"):
        _section(tmp_path, {"spec_type": "ngram-mod"}, gguf(tmp_path / "m.gguf"))


def test_an_unknown_type_is_still_refused(tmp_path, census):
    census("b12078")
    with pytest.raises(b.BackendError, match="unknown spec_type"):
        _section(tmp_path, {"spec_type": "ngram-bogus"}, gguf(tmp_path / "m.gguf"))


def test_the_other_ngram_types_stay_unselectable(tmp_path, census):
    census("b12078")
    for other in ("ngram-simple", "ngram-map-k", "ngram-cache"):
        with pytest.raises(b.BackendError, match="unknown spec_type"):
            _section(tmp_path, {"spec_type": other}, gguf(tmp_path / "m.gguf"))
