"""T1339 — a failed llama-server / listen-server start shows the user a plain sentence, never the
server's own log lines.

The raw lines are llama.cpp's own format, not ISO dates: `0.00.098.919 I system_info: n_threads = 16
... | CUDA : ARCHS = 500,610,... |`, an elapsed-time stamp and a one-letter level. They are recorded
below from a real llama-server log. Both start paths already keep them out of the chat and put them
in the raw error sink (runtime-errors.log, "for the debug surface only"); this holds that line, and
that the sink still gets the raw tail for whoever debugs it.

🔴 NO SERVER IS STARTED. `popen` is a fake process that writes the recorded lines and has exited.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import listen_tool, llm_backend, paths

# Recorded verbatim from a real llama-server log (router start, then a bad argument, then a bad GGUF).
REAL_LINES = """\
0.00.098.875 I log_info: verbosity = 3 (adjust with the `-lv N` CLI arg)
0.00.098.919 I system_info: n_threads = 16 (n_threads_batch = 16) / 32 | CUDA : ARCHS = 500,610,700,750,800,860,890,900 | USE_GRAPHS = 1 | PEER_MAX_BATCH_SIZE = 128 | CPU : SSE3 = 1 | SSSE3 = 1 | AVX
0.00.098.923 I srv  llama_server: n_parallel is set to auto, using n_parallel = 4 and kv_unified = true
0.00.240.739 I srv   load_models: Loaded 14 custom model presets
[59410] 0.00.789.294 E gguf_init_from_reader: tensor 'output.weight' has invalid ggml type 142. should be in [0, 42)
"""

LEAKS = (re.compile(r"system_info"), re.compile(r"CUDA : ARCHS"), re.compile(r"\b\d+\.\d\d\.\d{3}\.\d{3}\b"),
         re.compile(r"\b[IWE] srv\b"), re.compile(r"n_threads"), re.compile(r"gguf_init_from_reader"))


def _assert_clean(text: str) -> None:
    for rx in LEAKS:
        assert not rx.search(text), (rx.pattern, text)


class _Dead:
    pid = 31337

    def poll(self):
        return 1

    returncode = 1


def _dying_popen(cmd, **kw):
    kw["stdout"].write(REAL_LINES)
    kw["stdout"].flush()
    return _Dead()


@pytest.fixture
def raw_sink(monkeypatch):
    seen = []
    monkeypatch.setattr(listen_tool.runtime_log, "record_error", lambda event, **kw: seen.append((event, kw.get("detail", ""))) or True)
    monkeypatch.setattr(llm_backend.runtime_log, "record_error", lambda event, **kw: seen.append((event, kw.get("detail", ""))) or True)
    return seen


def test_listen_server_that_dies_at_once_says_a_plain_line_and_keeps_the_tail_in_the_sink(tmp_path, monkeypatch, raw_sink):
    gguf, mmproj = tmp_path / "a.gguf", tmp_path / "a.mmproj.gguf"
    gguf.write_bytes(b"x")
    mmproj.write_bytes(b"x")
    monkeypatch.setattr(listen_tool, "GGUF", gguf)
    monkeypatch.setattr(listen_tool, "MMPROJ", mmproj)
    monkeypatch.setattr(listen_tool, "WORKDIR", tmp_path / "work")
    monkeypatch.setattr(listen_tool, "SERVER_LOG", tmp_path / "work" / "llama_server.log")
    monkeypatch.setattr(listen_tool, "_llama_server", lambda: "llama-server.exe")
    monkeypatch.setattr(listen_tool.ttyguard, "popen", _dying_popen)

    proc, said = listen_tool._start_server()

    assert proc is None and said.startswith("[listen] the audio server stopped right after starting")
    _assert_clean(said)
    event, detail = raw_sink[-1]
    assert event == "listen_server_early_exit" and "system_info" in detail      # the debug sink keeps it all


def test_llama_router_that_never_becomes_healthy_names_the_log_not_its_lines(tmp_path, monkeypatch, raw_sink):
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(paths, "LLAMA_DIR", tmp_path / ".llama")
    monkeypatch.setattr(llm_backend, "llama_executable", lambda settings=None: exe)
    monkeypatch.setattr(llm_backend, "scan_models", lambda settings: [object()])
    monkeypatch.setattr(llm_backend, "write_preset_ini", lambda rows, settings, dest=None: tmp_path / "models.ini")
    monkeypatch.setattr(llm_backend, "_healthy", lambda host, timeout=2.0: False)
    monkeypatch.setattr(llm_backend.ttyguard, "popen", _dying_popen)
    monkeypatch.setattr(llm_backend.LlamaCppBackend, "_kill_tree", staticmethod(lambda proc: None))
    from litetui import prefix_persist
    monkeypatch.setattr(prefix_persist, "llama_launch_args", lambda settings, flags: [])

    backend = llm_backend.LlamaCppBackend.__new__(llm_backend.LlamaCppBackend)
    backend._settings = SimpleNamespace(llama_models_max=1, llama_attach_hosts=[])
    backend._host = "http://127.0.0.1:7470"
    backend.slot_persistence = SimpleNamespace(note_start=lambda: None)

    with pytest.raises(llm_backend.BackendError) as exc:
        backend._spawn()

    text = str(exc.value)
    assert "did not become ready in time" in text and "litetui-llama-server.log" in text
    _assert_clean(text)
    event, detail = raw_sink[-1]
    assert event == "llama.spawn_failed" and "system_info" in detail
