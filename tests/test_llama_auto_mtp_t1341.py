"""T1341-A2: embedded MTP is on by default for a GGUF that carries MTP heads.

NInfer turns MTP on by itself for a model with a drafter and Strata runs ``--spec 4 --mtp`` from its
config; llama.cpp's ``--spec-type`` defaults to ``none``, so a ``*-mtp`` GGUF ran without its heads
here unless the user set ``spec_type`` by hand. Now an UNSET ``spec_type`` on a GGUF with a positive
``<arch>.nextn_predict_layers`` resolves to ``draft-mtp`` (draft_max 3, draft_min 0, draft_p_min 0,
the same defaults the explicit mode applies). Everything else is left exactly as it was.

Synthetic GGUF headers (the same builder as test_llama_speculative.py) and a patched census: no
weights load and no llama-server runs.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from litetui import llm_backend as b
from litetui.settings import Settings

MODES = frozenset({"none", "draft-simple", "draft-mtp"})


def _string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<Q", len(raw)) + raw


def gguf(path: Path, *, layers: int | None = 1, arch: str = "qwen35") -> Path:
    pairs = [("general.architecture", 8, _string(arch))]
    if layers is not None:
        pairs.append((f"{arch}.nextn_predict_layers", 4, struct.pack("<I", layers)))
    data = b"GGUF" + struct.pack("<IQQ", 3, 0, len(pairs))
    data += b"".join(_string(k) + struct.pack("<I", t) + v for k, t, v in pairs)
    path.write_bytes(data)
    return path


@pytest.fixture(autouse=True)
def census(monkeypatch):
    monkeypatch.setattr(b, "sibling_mmproj", lambda path: (None, ()))
    monkeypatch.setattr(b, "configured_flags", lambda s: frozenset(b.FLAG_FOR.values()))
    monkeypatch.setattr(b, "configured_spec_types", lambda s: MODES)


def emit(tmp_path, cfg, model: Path) -> str:
    settings = Settings()
    settings.llama_load_settings = {"target": cfg}
    row = b.ModelRow("target", str(model), "custom")
    return b.write_preset_ini([row], settings, tmp_path / "models.ini").read_text(encoding="utf-8")


def _section(text: str) -> dict[str, str]:
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


def test_an_mtp_gguf_with_nothing_set_gets_embedded_mtp(tmp_path):
    model = gguf(tmp_path / "with-mtp.gguf", layers=1)
    sec = _section(emit(tmp_path, {"ctx": 8192}, model))
    assert sec["spec-type"] == "draft-mtp"
    assert sec["spec-draft-n-max"] == "3"
    assert sec["spec-draft-n-min"] == "0"
    assert sec["spec-draft-p-min"] == "0"
    assert sec["ctx-size"] == "8192"
    assert "spec-draft-model" not in sec


def test_a_model_with_no_settings_at_all_gets_it_too(tmp_path):
    model = gguf(tmp_path / "with-mtp.gguf")
    sec = _section(emit(tmp_path, {}, model))
    assert sec["spec-type"] == "draft-mtp"


def test_a_gguf_without_the_header_key_gets_nothing(tmp_path):
    model = gguf(tmp_path / "plain.gguf", layers=None)
    sec = _section(emit(tmp_path, {"ctx": 8192}, model))
    assert not any(k.startswith("spec-") for k in sec)


def test_zero_layers_is_not_mtp(tmp_path):
    model = gguf(tmp_path / "zero.gguf", layers=0)
    assert "spec-type" not in _section(emit(tmp_path, {}, model))


def test_an_unreadable_path_gets_nothing_and_does_not_raise(tmp_path):
    sec = _section(emit(tmp_path, {}, tmp_path / "missing.gguf"))
    assert "spec-type" not in sec


def test_explicit_off_is_honoured_on_an_mtp_gguf(tmp_path):
    model = gguf(tmp_path / "with-mtp.gguf")
    sec = _section(emit(tmp_path, {"spec_type": "none"}, model))
    assert sec["spec-type"] == "none"  # written as the user's explicit Off, with no draft flags
    assert not any(k.startswith("spec-draft") for k in sec)


def test_explicit_mtp_keeps_the_users_own_numbers(tmp_path):
    model = gguf(tmp_path / "with-mtp.gguf")
    sec = _section(emit(tmp_path, {"spec_type": "draft-mtp", "draft_max": 5}, model))
    assert sec["spec-type"] == "draft-mtp" and sec["spec-draft-n-max"] == "5"


def test_a_legacy_draft_profile_is_left_alone(tmp_path):
    model = gguf(tmp_path / "with-mtp.gguf")
    draft = tmp_path / "draft.gguf"
    draft.write_bytes(b"x")
    sec = _section(emit(tmp_path, {"draft_model": str(draft)}, model))
    assert "spec-type" not in sec and sec["spec-draft-model"].endswith("draft.gguf")


def test_a_user_set_draft_number_alone_also_means_the_user_chose(tmp_path):
    model = gguf(tmp_path / "with-mtp.gguf")
    sec = _section(emit(tmp_path, {"draft_max": 8}, model))
    assert "spec-type" not in sec


def test_a_build_that_does_not_list_draft_mtp_gets_nothing_and_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "configured_spec_types", lambda s: frozenset({"none", "draft-simple"}))
    model = gguf(tmp_path / "with-mtp.gguf")
    assert "spec-type" not in _section(emit(tmp_path, {}, model))


def test_an_empty_census_proves_nothing_so_nothing_is_added(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "configured_flags", lambda s: frozenset())
    model = gguf(tmp_path / "with-mtp.gguf")
    assert "spec-type" not in _section(emit(tmp_path, {}, model))


def test_a_census_missing_a_draft_flag_gets_nothing(tmp_path, monkeypatch):
    flags = frozenset(b.FLAG_FOR.values()) - {"spec-draft-p-min"}
    monkeypatch.setattr(b, "configured_flags", lambda s: flags)
    model = gguf(tmp_path / "with-mtp.gguf")
    assert "spec-type" not in _section(emit(tmp_path, {}, model))
