"""ninfer_engine — the launcher LiteTUI may own (Ryan a-35456da0 "LiteTUI may start it").

Every arm is pure or injected: no card, no process, no real config file. The
env is redirected so nothing reads Ryan's machine (the trap test_ninfer_backend
already recorded once).
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from litetui import ninfer_engine as eng
from litetui.llm_backend import BackendError


class _Settings:
    backend = "ninfer"
    ninfer_host = ""
    ninfer_executable = ""
    ninfer_artifact = ""
    ninfer_max_context = 32768


def _artifact(tmp_path: Path, directory: dict | None, magic: bytes = eng.NINFER_V3_MAGIC) -> Path:
    body = json.dumps(directory).encode() if directory is not None else b""
    head = bytearray(eng.NINFER_V3_HEADER_BYTES)
    head[:8] = magic
    struct.pack_into("<Q", head, 8, len(body))
    p = tmp_path / "m.ninfer"
    p.write_bytes(bytes(head) + body)
    return p


# ── argv: LiteSuite's, flag for flag ────────────────────────────────────────

def test_argv_is_litesuites_with_the_ruled_defaults():
    args = eng.build_ninfer_args(Path("a.ninfer"), 49260, "qwen3.8-27b", components=("text", "mtp"))
    assert args[:7] == ["a.ninfer", "--host", "127.0.0.1", "--port", "49260", "--model-id", "qwen3.8-27b"]
    assert args[args.index("--max-context") + 1] == "32768"
    assert args[args.index("--spec") + 1] == "mtp" and "--lm-head-draft" in args
    assert args[args.index("--draft-tokens") + 1] == "3"
    assert args[args.index("--kv-dtype") + 1] == "fp8" and args[-1] == "--preserve-thinking"


def test_spec_is_gated_on_the_artifact_not_the_flag_parser():
    """The engine parses --spec dflash2 whether or not the file carries it; the
    failure lands at model load after the user waited (ninfer-args.ts)."""
    args = eng.build_ninfer_args(Path("a.ninfer"), 1, "m", components=("text",))
    assert "--spec" not in args and "--lm-head-draft" not in args


def test_draft_tokens_are_clamped_into_the_backends_range():
    args = eng.build_ninfer_args(Path("a"), 1, "m", components=("mtp",), draft_tokens=9)
    assert args[args.index("--draft-tokens") + 1] == "3"


def test_a_chosen_context_wins_over_the_default():
    args = eng.build_ninfer_args(Path("a"), 1, "m", max_context=65536)
    assert args[args.index("--max-context") + 1] == "65536"


# ── the artifact header: read the FILE, not a sidecar ───────────────────────

def test_reads_the_v3_directory_from_the_container_header(tmp_path):
    p = _artifact(tmp_path, {"model_id": "qwen3.8-27b", "components": {"text": {}, "mtp": {}}})
    d = eng.read_artifact_directory(p)
    assert eng.artifact_model_id(d) == "qwen3.8-27b"
    assert eng.artifact_components(d) == ("text", "mtp")


def test_a_wrong_magic_answers_none_and_the_model_id_falls_back(tmp_path):
    p = _artifact(tmp_path, {"model_id": "x"}, magic=b"NOTNINF\x00")
    assert eng.read_artifact_directory(p) is None
    assert eng.artifact_model_id(None) == "qwen3.8-27b"


# ── the shared registry: LiteSuite's extraEndpoints ─────────────────────────

def test_register_and_unregister_round_trip_in_litesuites_config(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"version": 1, "extraEndpoints": [{"baseUrl": "http://x:1", "kind": "other"}]}))
    assert eng.registered_host() is None
    assert eng.register_host("http://127.0.0.1:5") is True
    assert eng.registered_host() == "http://127.0.0.1:5"
    body = json.loads(cfg.read_text())
    assert {"baseUrl": "http://x:1", "kind": "other"} in body["extraEndpoints"]   # other kinds untouched
    eng.unregister_host("http://127.0.0.1:5/")
    assert eng.registered_host() is None
    assert json.loads(cfg.read_text())["version"] == 1


# ── refusals: never a second model ──────────────────────────────────────────

def test_refuses_when_an_engine_is_registered_and_answering(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    eng.register_host("http://127.0.0.1:7")
    reason = eng.refuse_reason(_Settings(), healthy=lambda h: True)
    assert reason and "already registered and answering" in reason


def test_refuses_when_an_engine_is_registered_silent_and_loading(tmp_path, monkeypatch):
    """Silent + a live ninfer-serve process = LOADING (the port binds after the weights).
    A second start here is the VRAM bug, so it refuses."""
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(eng, "engine_process_alive", lambda: True)
    eng.register_host("http://127.0.0.1:7")
    reason = eng.refuse_reason(_Settings(), healthy=lambda h: False)
    assert reason and "still loading" in reason
    assert eng.registered_host() == "http://127.0.0.1:7"          # left alone


def test_a_stale_registration_with_no_process_is_cleared_not_obeyed(tmp_path, monkeypatch):
    """Measured 2026-09-17: a holder killed with TerminateProcess skipped atexit and left
    the dead port registered; refusing on it would have blocked every start forever."""
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: None)
    eng.register_host("http://127.0.0.1:7")
    assert eng.refuse_reason(_Settings(), healthy=lambda h: False) is None
    assert eng.registered_host() is None                            # stale entry removed


def test_refuses_when_the_explicit_host_answers(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    s = _Settings(); s.ninfer_host = "http://127.0.0.1:9/"
    reason = eng.refuse_reason(s, healthy=lambda h: h == "http://127.0.0.1:9")
    assert reason and "ninfer_host" in reason


def test_refuses_on_a_full_card(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: 4000)
    reason = eng.refuse_reason(_Settings(), healthy=lambda h: False)
    assert reason and "free on the GPU" in reason


def test_an_unaskable_card_does_not_refuse(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: None)
    assert eng.refuse_reason(_Settings(), healthy=lambda h: False) is None


# ── start: spawn, wait for the engine's own words, register ─────────────────

class _Proc:
    pid = 777
    def __init__(self):
        self.ended = None
    def poll(self):
        return self.ended


def _ready_engine(tmp_path, monkeypatch, *, log_text: str):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: 30000)
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    monkeypatch.setattr(eng, "free_port", lambda host="127.0.0.1": 49260)
    exe = tmp_path / "ninfer" / "ninfer-serve.exe"; exe.parent.mkdir(); exe.write_bytes(b"MZ")
    art = _artifact(tmp_path, {"model_id": "qwen3.8-27b", "components": {"text": {}, "mtp": {}}})
    s = _Settings(); s.ninfer_executable = str(exe); s.ninfer_artifact = str(art)
    spawned = {}
    def spawn(cmd, **kw):
        spawned["cmd"] = cmd
        kw["stdout"].write(log_text); kw["stdout"].flush()
        return _Proc()
    return s, spawn, spawned


def test_start_spawns_litesuites_argv_and_registers_the_port(tmp_path, monkeypatch):
    s, spawn, spawned = _ready_engine(tmp_path, monkeypatch, log_text="engine ready\nlistening on http://127.0.0.1:49260\n")
    monkeypatch.setattr(eng.atexit, "register", lambda *a, **k: None)
    owned = eng.start(s, healthy=lambda h: False, spawn=spawn)
    assert owned.host == "http://127.0.0.1:49260" and owned.model_id == "qwen3.8-27b"
    assert spawned["cmd"][0].endswith("ninfer-serve.exe") and "--spec" in spawned["cmd"]
    assert eng.registered_host() == "http://127.0.0.1:49260"
    eng.stop(owned)
    assert eng.registered_host() is None


def test_a_failure_line_ends_the_wait_now(tmp_path, monkeypatch):
    s, spawn, _ = _ready_engine(tmp_path, monkeypatch, log_text="server listen failed\n")
    monkeypatch.setattr(eng, "_kill", lambda proc: None)
    with pytest.raises(BackendError) as excinfo:
        eng.start(s, healthy=lambda h: False, spawn=spawn)
    assert "server listen failed" in str(excinfo.value)
    assert eng.registered_host() is None


def test_start_refuses_before_touching_the_exe(tmp_path, monkeypatch):
    """A refusal never spawns — the mutation that matters for VRAM."""
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(eng, "engine_process_alive", lambda: True)
    eng.register_host("http://127.0.0.1:7")
    calls = []
    with pytest.raises(BackendError):
        eng.start(_Settings(), healthy=lambda h: True, spawn=lambda *a, **k: calls.append(1))
    assert calls == []
