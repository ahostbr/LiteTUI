"""Discovery: every servable GGUF on the box, exactly once, source-tagged.

The scan rules each earned their place: mmproj files are projectors (offered
in the Load tab, not the model picker); shards beyond -00001- belong to their
first file; sub-10MB ggufs are debris; the same file in two stores is ONE
model (loading it twice is how VRAM disappears).
"""
from __future__ import annotations

from pathlib import Path

import llm_backend
from settings import Settings


def _settings(*dirs: Path) -> Settings:
    s = Settings()
    s.llama_scan_litesuite = False
    s.llama_scan_lmstudio = False
    s.llama_scan_hf_cache = False
    s.llama_models_dirs = [str(d) for d in dirs]
    return s


def _gguf(path: Path, size: int = 20 * 1024 * 1024) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


def test_scan_skips_and_tags(tmp_path):
    root = tmp_path / "models"
    _gguf(root / "org" / "tiny-chat.Q4_K_M.gguf")
    _gguf(root / "org" / "mmproj-tiny.gguf")                       # projector
    _gguf(root / "org" / "big-00002-of-00003.gguf")                # later shard
    _gguf(root / "org" / "big-00001-of-00003.gguf")                # first shard
    _gguf(root / "org" / "stub.gguf", size=1024)                   # debris

    rows = llm_backend.scan_models(_settings(root))
    keys = [r.key for r in rows]
    assert "tiny-chat.q4_k_m" in keys
    assert "big-00001-of-00003" in keys
    assert not any("mmproj" in k for k in keys)
    assert not any("00002" in k for k in keys)
    assert not any("stub" in k for k in keys)
    assert all(r.source == "custom" for r in rows)


def test_same_file_in_two_roots_is_one_row(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _gguf(a / "dupe.Q4.gguf", size=30 * 1024 * 1024)
    _gguf(b / "sub" / "dupe.Q4.gguf", size=30 * 1024 * 1024)
    rows = llm_backend.scan_models(_settings(a, b))
    assert len(rows) == 1
    # The losing copy is not lost — the Info tab shows where else it lives.
    assert len(rows[0].extra_paths) == 1


def test_stem_collision_gets_distinct_keys(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _gguf(a / "model.gguf", size=20 * 1024 * 1024)
    _gguf(b / "model.gguf", size=25 * 1024 * 1024)   # different size = different model
    rows = llm_backend.scan_models(_settings(a, b))
    assert len(rows) == 2
    assert len({r.key for r in rows}) == 2, "two models, one picker id — loads the wrong one"


def _gguf_with_arch(path: Path, arch: str, size: int = 20 * 1024 * 1024) -> Path:
    """A minimal REAL GGUF header: magic, v3, 0 tensors, 1 KV
    (general.architecture = arch), padded to size."""
    import struct

    key = b"general.architecture"
    val = arch.encode()
    head = (
        b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", 0, 1)
        + struct.pack("<Q", len(key)) + key
        + struct.pack("<I", 8)                      # value type: string
        + struct.pack("<Q", len(val)) + val
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + b"\0" * (size - len(head)))
    return path


def test_non_chat_architectures_are_skipped_by_header_not_name(tmp_path):
    """The E2E proved this the hard way: LiteSuite's models dir holds VOICE
    ggufs, and offering kokoro in /model cost 300s of the router failing to
    serve a TTS codec as an LLM. The header is the truth; the name is not."""
    root = tmp_path / "m"
    _gguf_with_arch(root / "innocent-name.gguf", "kokoro")
    _gguf_with_arch(root / "vocoder.gguf",
                    "this model cannot be used as LLM, use it via --model-vocoder")
    _gguf_with_arch(root / "chatty.gguf", "qwen35")
    _gguf_with_arch(root / "flux1-dev.gguf", "flux")   # the live-walk picker pollution
    rows = llm_backend.scan_models(_settings(root))
    assert [r.key for r in rows] == ["chatty"]


def test_arch_parser_reads_the_header(tmp_path):
    p = _gguf_with_arch(tmp_path / "x.gguf", "gemma3")
    assert llm_backend._gguf_architecture(p) == "gemma3"
    (tmp_path / "junk.gguf").write_bytes(b"NOTG" + b"\0" * 100)
    assert llm_backend._gguf_architecture(tmp_path / "junk.gguf") is None


def test_mmproj_candidates_are_the_skipped_files(tmp_path):
    root = tmp_path / "m"
    _gguf(root / "mmproj-vision.gguf")
    _gguf(root / "chat.gguf")
    cands = llm_backend.mmproj_candidates(_settings(root))
    assert len(cands) == 1 and "mmproj" in cands[0]
