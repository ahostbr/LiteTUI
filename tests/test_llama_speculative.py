"""Speculation is opt-in, capability-checked, and refused before eviction.

Synthetic GGUF headers and mocked process/HTTP boundaries only: no weights load.
"""
from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest

from litetui import llm_backend as b
from litetui.settings import Settings

MODES = frozenset({"none", "draft-simple", "draft-mtp"})


def _string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<Q", len(raw)) + raw


def gguf(path: Path, *, arch="qwen35", layers=1, layer_type=4, prefix=0,
         layer_arch=None, architecture_last=False) -> Path:
    pairs = [(f"filler.{i}", 8, _string("ignored")) for i in range(prefix)]
    # Real metadata may put tokenizer arrays before the MTP scalar.
    pairs.append(("tokenizer.tokens", 9, struct.pack("<IQ", 8, 2) + _string("a") + _string("b")))
    architecture = ("general.architecture", 8, _string(arch))
    if not architecture_last:
        pairs.append(architecture)
    if layers is not None:
        encoded = _string(layers) if layer_type == 8 else struct.pack(
            {4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q"}[layer_type], layers)
        pairs.append((f"{layer_arch or arch}.nextn_predict_layers", layer_type, encoded))
    if architecture_last:
        pairs.append(architecture)
    data = b"GGUF" + struct.pack("<IQQ", 3, 0, len(pairs))
    data += b"".join(_string(k) + struct.pack("<I", t) + v for k, t, v in pairs)
    path.write_bytes(data)
    return path


@pytest.fixture(autouse=True)
def census(monkeypatch):
    monkeypatch.setattr(b, "configured_flags", lambda s: frozenset(b.FLAG_FOR.values()))
    # raising=False makes pre-implementation failures behavioral, not setup errors.
    monkeypatch.setattr(b, "configured_spec_types", lambda s: MODES, raising=False)


def emit(tmp_path, cfg, *, path=None):
    settings = Settings()
    settings.llama_load_settings = {"target": cfg}
    row = b.ModelRow("target", str(path or tmp_path / "target.gguf"), "custom")
    return b.write_preset_ini([row], settings, tmp_path / "models.ini").read_text(encoding="utf-8")


def test_off_strips_all_stale_draft_inputs(tmp_path):
    text = emit(tmp_path, {"spec_type": "none", "draft_model": "missing.gguf",
                           "draft_max": -4, "draft_min": 12, "draft_p_min": "bad"})
    assert "spec-type = none" in text
    assert "spec-draft" not in text


def test_legacy_external_draft_is_unchanged(tmp_path):
    text = emit(tmp_path, {"draft_model": "C:/old draft.gguf", "draft_max": 8})
    assert "spec-type =" not in text
    assert "spec-draft-model = C:/old draft.gguf" in text
    assert "spec-draft-n-max = 8" in text


def test_mtp_embedded_defaults_and_explicit_tuning(tmp_path):
    path = gguf(tmp_path / "ordinary-name.gguf", prefix=80, architecture_last=True)
    cfg = {"spec_type": "draft-mtp", "draft_model": "stale.gguf"}
    text = emit(tmp_path, cfg, path=path)
    assert "spec-draft-model" not in text
    assert "spec-type = draft-mtp" in text
    for key, value in (("n-max", 3), ("n-min", 0), ("p-min", 0)):
        assert f"spec-draft-{key} = {value}" in text
    text = emit(tmp_path, {**cfg, "draft_max": 5, "draft_min": 2, "draft_p_min": .3}, path=path)
    assert "spec-draft-n-max = 5" in text and "spec-draft-n-min = 2" in text
    assert "spec-draft-p-min = 0.3" in text
    assert cfg == {"spec_type": "draft-mtp", "draft_model": "stale.gguf"}


@pytest.mark.parametrize("kwargs", [
    {"layers": None}, {"layers": 0}, {"layers": -1, "layer_type": 5},
    {"layers": 1., "layer_type": 6}, {"layers": True, "layer_type": 7},
    {"layers": "1", "layer_type": 8}, {"layer_arch": "other"},
])
def test_mtp_requires_architecture_keyed_positive_integer(tmp_path, kwargs):
    path = gguf(tmp_path / "name-says-mtp.gguf", **kwargs)
    with pytest.raises(b.BackendError, match="target.*nextn_predict_layers"):
        emit(tmp_path, {"spec_type": "draft-mtp"}, path=path)


def test_unreadable_model_is_not_mtp_evidence(tmp_path):
    with pytest.raises(b.BackendError, match="target.*nextn_predict_layers"):
        emit(tmp_path, {"spec_type": "draft-mtp"})


@pytest.mark.parametrize("cfg", [
    {"spec_type": "future"}, {"spec_type": "draft-simple"},
    {"spec_type": "draft-simple", "draft_model": "missing.gguf"},
    {"spec_type": "draft-mtp", "draft_max": 0},
    {"spec_type": "draft-mtp", "draft_max": 2.5},
    {"spec_type": "draft-mtp", "draft_max": True},
    {"spec_type": "draft-mtp", "draft_min": -1},
    {"spec_type": "draft-mtp", "draft_min": 4},
    {"spec_type": "draft-mtp", "draft_p_min": float("nan")},
    {"spec_type": "draft-mtp", "draft_p_min": 1.1},
])
def test_invalid_explicit_settings_name_the_model(tmp_path, cfg):
    with pytest.raises(b.BackendError, match="target"):
        emit(tmp_path, cfg, path=gguf(tmp_path / "target.gguf"))


def test_external_draft_requires_real_file(tmp_path):
    draft = gguf(tmp_path / "draft.gguf")
    text = emit(tmp_path, {"spec_type": "draft-simple", "draft_model": str(draft)})
    assert "spec-type = draft-simple" in text and "spec-draft-model =" in text


@pytest.mark.parametrize("flags", [frozenset(), frozenset({"spec-type"})])
def test_mtp_requires_emitted_flags(tmp_path, monkeypatch, flags):
    monkeypatch.setattr(b, "configured_flags", lambda s: flags)
    with pytest.raises(b.BackendError, match="target"):
        emit(tmp_path, {"spec_type": "draft-mtp"}, path=gguf(tmp_path / "target.gguf"))


def test_mtp_requires_advertised_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "configured_spec_types", lambda s: frozenset({"none", "draft-simple"}))
    with pytest.raises(b.BackendError, match="target.*draft-mtp"):
        emit(tmp_path, {"spec_type": "draft-mtp"}, path=gguf(tmp_path / "target.gguf"))


def test_off_with_unknown_census_omits_even_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "configured_flags", lambda s: frozenset())
    text = emit(tmp_path, {"spec_type": "none", "draft_model": "old", "draft_max": 8})
    assert "spec-" not in text


def test_off_with_known_missing_mode_flag_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "configured_flags", lambda s: frozenset({"ctx-size"}))
    with pytest.raises(b.BackendError, match="target"):
        emit(tmp_path, {"spec_type": "none"})


def test_whitespace_is_unset(tmp_path):
    text = emit(tmp_path, {"spec_type": "  ", "draft_model": " \t", "draft_max": " "})
    assert "spec-" not in text
    text = emit(tmp_path, {"spec_type": "draft-mtp", "draft_max": " "},
                path=gguf(tmp_path / "target.gguf"))
    assert "spec-draft-n-max = 3" in text


def test_whole_ini_failure_does_not_replace_previous_file(tmp_path):
    dest = tmp_path / "models.ini"
    dest.write_text("previous ini", encoding="utf-8")
    with pytest.raises(b.BackendError):
        emit(tmp_path, {"spec_type": "draft-mtp"})
    assert dest.read_text(encoding="utf-8") == "previous ini"


@pytest.mark.parametrize("entry", ["apply", "context", "load", "regen"])
def test_other_models_invalid_profile_cannot_evict_resident(tmp_path, monkeypatch, entry):
    settings = Settings()
    settings.llama_load_settings = {"bad-other": {"spec_type": "draft-mtp"}}
    rows = [b.ModelRow("resident", str(gguf(tmp_path / "resident.gguf")), "custom"),
            b.ModelRow("bad-other", str(gguf(tmp_path / "bad.gguf", layers=0)), "custom")]
    backend = b.LlamaCppBackend(settings)
    monkeypatch.setattr(b, "scan_models", lambda s: rows)
    monkeypatch.setattr(backend, "_server_models", lambda: {"resident": {"status": {"value": "loaded"}}})
    effects = []
    previous = {k: dict(v) for k, v in settings.llama_load_settings.items()}
    dest = b.paths.LLAMA_DIR / "litetui-models.ini"
    dest.parent.mkdir(parents=True)
    dest.write_text("resident preset", encoding="utf-8")
    monkeypatch.setattr(backend, "_unload_sync", lambda key: effects.append("unload"))
    monkeypatch.setattr(backend, "shutdown", lambda: effects.append("shutdown"))
    monkeypatch.setattr(backend, "_ensure_running_sync", lambda **kw: effects.append("start"))
    monkeypatch.setattr(b, "_http_json", lambda *a, **kw: effects.append("http") or {})
    with pytest.raises(b.BackendError, match="bad-other"):
        if entry == "apply":
            asyncio.run(backend.apply_load_settings("resident", {"ctx": 4096}))
        elif entry == "context":
            asyncio.run(backend.load("resident", ctx=4096))
        elif entry == "regen":
            backend._regen_ini()
        else:
            asyncio.run(backend.load("resident"))
    assert effects == []
    assert settings.llama_load_settings == previous
    assert dest.read_text(encoding="utf-8") == "resident preset"


@pytest.mark.parametrize("kind,payload", [(8, _string("1")), (4, struct.pack("<I", 1))])
def test_duplicate_metadata_is_ineligible(tmp_path, kind, payload):
    path = gguf(tmp_path / "duplicate.gguf")
    data = bytearray(path.read_bytes())
    count = struct.unpack_from("<Q", data, 16)[0]
    struct.pack_into("<Q", data, 16, count + 1)
    data += _string("qwen35.nextn_predict_layers") + struct.pack("<I", kind) + payload
    path.write_bytes(data)
    with pytest.raises(b.BackendError, match="target.*nextn_predict_layers"):
        emit(tmp_path, {"spec_type": "draft-mtp"}, path=path)


@pytest.mark.parametrize("data", [
    b"GGUF" + struct.pack("<IQQ", 3, 0, 100_001),
    b"GGUF" + struct.pack("<IQQQ", 3, 0, 1, 2**63),
    b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + _string("array") + struct.pack("<IIQ", 9, 4, 2**63),
    b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + _string("array") + struct.pack("<IIQ", 9, 8, 1_000_001),
])
def test_bounded_metadata_refuses_malformed_headers(tmp_path, data):
    path = tmp_path / "bad.gguf"
    path.write_bytes(data)
    assert b.gguf_metadata(path) == (None, None)


def test_discovery_carries_mtp_evidence(tmp_path, monkeypatch):
    path = gguf(tmp_path / "arbitrary.gguf", prefix=80)
    monkeypatch.setattr(b, "_MIN_BYTES", 0)
    settings = Settings(llama_scan_litesuite=False, llama_scan_lmstudio=False,
                        llama_scan_hf_cache=False, llama_models_dirs=[str(tmp_path)])
    assert b.scan_models(settings) == [b.ModelRow("arbitrary", str(path), "custom", nextn_predict_layers=1)]


def test_valid_apply_hands_validated_ini_to_restart_before_load(tmp_path, monkeypatch):
    settings = Settings()
    path = gguf(tmp_path / "resident.gguf")
    rows = [b.ModelRow("resident", str(path), "custom")]
    backend = b.LlamaCppBackend(settings)
    backend._owned = object()  # process effects below are substituted, never native
    monkeypatch.setattr(b, "scan_models", lambda s: rows)
    monkeypatch.setattr(backend, "_server_models", lambda: {"resident": {"status": {"value": "loaded"}}})
    effects = []
    monkeypatch.setattr(backend, "_record_load_settings", lambda key, cfg: effects.append("persist"))
    monkeypatch.setattr(backend, "_unload_sync", lambda key: effects.append("unload"))
    monkeypatch.setattr(backend, "shutdown", lambda: effects.append("shutdown"))

    def start(*, preset):
        text = preset.read_text(encoding="utf-8")
        assert "spec-type = draft-mtp" in text
        assert "spec-draft-n-max = 3" in text
        assert "spec-draft-model" not in text
        effects.append("start")

    monkeypatch.setattr(backend, "_ensure_running_sync", start)
    monkeypatch.setattr(b, "_http_json", lambda *a, **kw: effects.append("load") or {})
    asyncio.run(backend.apply_load_settings("resident", {"spec_type": "draft-mtp"},
                                           notice=lambda: effects.append("notice")))
    assert effects == ["persist", "notice", "unload", "shutdown", "start", "load"]


def test_backend_rereads_eligibility_after_earlier_validation(tmp_path, monkeypatch):
    settings = Settings()
    path = gguf(tmp_path / "resident.gguf")
    cfg = {"spec_type": "draft-mtp"}
    b.resolve_speculation("resident", str(path), cfg, settings)
    rows = [b.ModelRow("resident", str(path), "custom", nextn_predict_layers=1)]
    gguf(path, layers=0)
    backend = b.LlamaCppBackend(settings)
    monkeypatch.setattr(b, "scan_models", lambda s: rows)
    monkeypatch.setattr(backend, "_unload_sync", lambda key: pytest.fail("unloaded"))
    monkeypatch.setattr(backend, "_server_models", lambda: pytest.fail("read server before validation"))
    with pytest.raises(b.BackendError, match="resident.*nextn_predict_layers"):
        asyncio.run(backend.apply_load_settings("resident", cfg))



def test_speculation_does_not_change_unrelated_settings(tmp_path):
    cfg = {"mmproj": None, "chat_template_file": " C:/kept path ", "spec_type": "none"}
    effective = b.resolve_speculation("target", None, cfg, Settings())
    assert effective == cfg



def test_restart_does_not_grant_permission_to_replacement_foreign_server(tmp_path, monkeypatch):
    settings = Settings()
    rows = [b.ModelRow("resident", str(gguf(tmp_path / "resident.gguf")), "custom")]
    backend = b.LlamaCppBackend(settings)
    monkeypatch.setattr(b, "scan_models", lambda s: rows)
    monkeypatch.setattr(backend, "_server_models", dict)
    monkeypatch.setattr(backend, "_record_load_settings", lambda *a: None)

    def replacement(*, preset):
        backend._attached_host = "http://foreign:1"
        backend._shape = b._SHAPE_SINGLE

    monkeypatch.setattr(backend, "_ensure_running_sync", replacement)
    monkeypatch.setattr(b, "_http_json", lambda *a, **kw: pytest.fail("load sent to foreign server"))
    with pytest.raises(b.BackendError, match="serving one model"):
        asyncio.run(backend.apply_load_settings("resident", {"spec_type": "none"}))
