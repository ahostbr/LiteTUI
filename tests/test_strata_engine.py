"""strata_engine — the launcher for Strata (T0374). Every arm is pure or injected:
no model is loaded, no process is spawned, no memory or GPU is read from this machine."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from litetui import ninfer_engine, strata_engine as eng
from litetui.llm_backend import BackendError

GB = 10 ** 9


class _Settings:
    strata_host = ""
    strata_root = ""
    strata_config = ""
    strata_max_context = None


@pytest.fixture(autouse=True)
def _no_real_process_ops(monkeypatch, tmp_path):
    """Same discipline as test_ninfer_engine: fake pids are never handed to a real kill,
    atexit is faked so no cleanup callback outlives the test, and the wait is bounded."""
    monkeypatch.setattr(eng, "STRATA_START_TIMEOUT_S", 0.3)
    # `strata_root` falls back to LiteSuite's config.json; never the user's real one.
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path / "llm"))
    monkeypatch.setattr(ninfer_engine, "_kill", lambda proc: None)
    monkeypatch.setattr(eng.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(eng.jobkill, "create", lambda: None)
    monkeypatch.setattr(eng, "log_path", lambda: tmp_path / "logs" / "strata.log")
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    monkeypatch.setattr(eng, "memory_free_bytes", lambda: (200 * GB, 400 * GB))
    monkeypatch.setattr(eng, "gpu_memory_mib", lambda: None)
    monkeypatch.setattr(eng, "request_unload", lambda host, timeout=30.0: True)
    from litetui import llm_backend
    monkeypatch.setattr(llm_backend, "wait_for_exit", lambda proc, **kw: True)


def _install(tmp_path: Path, *names: str, ctx: int = 32768, port: int = 8080) -> tuple[_Settings, list[Path]]:
    """A folder shaped like one Strata's setup prepared, with one config per name."""
    root = tmp_path / "strata"
    python = eng.server_python(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    (root / "serve").mkdir()
    (root / "serve" / "server.py").write_text("", encoding="utf-8")
    native = tmp_path / "model-00001-of-00002.gguf"
    native.write_bytes(b"\0" * 1000)
    paths = []
    for name in names:
        path = root / f"strata-{name}.json"
        path.write_text(json.dumps({
            "exe": str(root / "engine" / "strata.exe"), "port": port, "model_name": f"flash-{name}",
            "args": ["--native", str(native), "--max-context", str(ctx), "--kv", "int8"]}), encoding="utf-8")
        paths.append(path)
    settings = _Settings()
    settings.strata_root = str(root)
    return settings, paths


# ── which model ──────────────────────────────────────────────────────────────

def test_the_install_folder_is_the_setting_else_litesuites_else_the_default(tmp_path):
    settings = _Settings()
    llm = tmp_path / "llm"
    assert eng.strata_root(settings) == llm / "strata"
    llm.mkdir()
    (llm / "config.json").write_text('{"strataRoot": " F:/Strata "}', encoding="utf-8")
    assert eng.strata_root(settings) == Path("F:/Strata")
    settings.strata_root = "D:/mine"
    assert eng.strata_root(settings) == Path("D:/mine")
    (llm / "config.json").write_text("not json", encoding="utf-8")
    settings.strata_root = ""
    assert eng.strata_root(settings) == llm / "strata"


def test_one_prepared_model_is_chosen_and_two_are_not_guessed(tmp_path):
    settings, (only,) = _install(tmp_path, "iq3_xxs")
    assert eng.strata_config(settings) == only
    (only.parent / "strata-iq2_xs.json").write_text(only.read_text(encoding="utf-8"), encoding="utf-8")
    assert eng.strata_config(settings) is None
    settings.strata_config = str(only)
    assert eng.strata_config(settings) == only


def test_a_json_that_is_not_a_strata_config_is_not_listed(tmp_path):
    settings, (real,) = _install(tmp_path, "iq3_xxs")
    (real.parent / "strata-notes.json").write_text('{"hello": 1}', encoding="utf-8")
    (real.parent / "strata-broken.json").write_text("{", encoding="utf-8")
    assert eng.list_strata_configs(settings) == [real]


def test_the_host_is_the_setting_else_the_configs_port(tmp_path):
    settings, _ = _install(tmp_path, "iq3_xxs", port=8123)
    assert eng.default_host(settings) == "http://127.0.0.1:8123"
    settings.strata_host = "http://10.0.0.5:9000/"
    assert eng.default_host(settings) == "http://10.0.0.5:9000"
    assert eng.default_host(_Settings()) == "http://127.0.0.1:8080"      # nothing installed


# ── the context override ─────────────────────────────────────────────────────

def test_a_chosen_context_replaces_the_configs_and_leaves_the_original_alone():
    cfg = {"exe": "x", "args": ["--max-context", "32768", "--kv", "int8"]}
    out = eng.with_context(cfg, 150000)
    assert eng.config_context(out) == 150000 and eng.config_context(cfg) == 32768
    assert eng.with_context(cfg, 32768) is cfg and eng.with_context(cfg, None) is cfg


def test_a_long_context_gets_the_8_bit_kv_setup_would_have_written():
    out = eng.with_context({"exe": "x", "args": ["--max-context", "8192"]}, 65536)
    assert out["args"][-2:] == ["--kv", "int8"]
    assert "--kv" not in eng.with_context({"exe": "x", "args": ["--max-context", "8192"]}, 4096)["args"]


def test_a_context_past_the_trained_window_is_refused_not_started():
    with pytest.raises(BackendError, match="262144"):
        eng.with_context({"exe": "x", "args": ["--max-context", "32768"]}, 400000)


# ── the memory gate ──────────────────────────────────────────────────────────

def _cfg(monkeypatch, size: int, *extra: str) -> dict:
    """A config whose --native shard REPORTS `size` bytes. NO FILE IS CREATED.

    🔴 The first version made the file with `truncate(size)`. NTFS does not make that sparse:
    it wrote 47 GB of zeros to C: (twice) on 2026-10-06 and took the drive from 76 GB free to
    34 GB. A test never creates anything near model size; it states the number."""
    monkeypatch.setattr(eng, "native_bytes", lambda cfg: size)
    return {"exe": "x", "args": ["--native", "not-a-real-file.gguf", *extra]}


def test_memory_gate_passes_the_measured_fit_and_names_each_shortfall(monkeypatch):
    cfg = _cfg(monkeypatch, 47 * GB)                    # the IQ3_XXS first shard: pins ~42.8 GB
    gpu = (31500, 32607)
    assert eng.memory_refusal(cfg, ram_free=45 * GB, commit_free=78 * GB, gpu=gpu) is None
    assert "available" in eng.memory_refusal(cfg, ram_free=40 * GB, commit_free=78 * GB, gpu=gpu)
    assert "commit" in eng.memory_refusal(cfg, ram_free=45 * GB, commit_free=60 * GB, gpu=gpu)
    assert "holds the card" in eng.memory_refusal(cfg, ram_free=45 * GB, commit_free=78 * GB, gpu=(9000, 32607))


def test_memory_gate_does_not_refuse_on_what_it_cannot_measure(monkeypatch):
    cfg = _cfg(monkeypatch, 47 * GB)
    assert eng.memory_refusal(cfg, ram_free=None, commit_free=None, gpu=None) is None
    # Strata's low-RAM modes read experts from disk: there is no pin to size.
    mapped = _cfg(monkeypatch, 47 * GB, "--mmap-experts")
    assert eng.memory_refusal(mapped, ram_free=8 * GB, commit_free=8 * GB, gpu=None) is None


# ── the launch recipe ────────────────────────────────────────────────────────

def test_launch_plan_is_stratas_own_start_line(tmp_path):
    settings, (cfg_path,) = _install(tmp_path, "iq3_xxs", port=8090)
    argv, cwd, cfg, host = eng.launch_plan(settings)
    root = Path(settings.strata_root)
    assert argv == [str(eng.server_python(root)), str(root / "serve" / "server.py"),
                    "--engine", "strata", "--config", str(cfg_path), "--port", "8090"]
    assert cwd == root and host == "http://127.0.0.1:8090" and cfg["model_name"] == "flash-iq3_xxs"


def test_a_chosen_context_runs_from_a_copy_never_the_installs_own_file(tmp_path):
    settings, (cfg_path,) = _install(tmp_path, "iq3_xxs")
    before = cfg_path.read_text(encoding="utf-8")
    settings.strata_max_context = 131072
    argv, _cwd, cfg, _host = eng.launch_plan(settings)
    ran = Path(argv[argv.index("--config") + 1])
    assert ran != cfg_path and ran.parent == eng.log_path().parent
    assert eng.config_context(json.loads(ran.read_text(encoding="utf-8"))) == 131072 == eng.config_context(cfg)
    assert cfg_path.read_text(encoding="utf-8") == before


def test_a_missing_install_says_where_to_get_it(tmp_path):
    settings = _Settings()
    settings.strata_root = str(tmp_path / "nowhere")
    with pytest.raises(BackendError, match="github.com/Niko1221/Strata"):
        eng.launch_plan(settings)
    settings, paths = _install(tmp_path, "a", "b")       # installed, two models, none chosen
    with pytest.raises(BackendError, match="/model"):
        eng.launch_plan(settings)


# ── start ────────────────────────────────────────────────────────────────────

class _Proc:
    pid = 4242

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def test_start_refuses_before_spawning_when_a_server_already_answers(tmp_path):
    settings, _ = _install(tmp_path, "iq3_xxs")
    spawned, noticed = [], []
    with pytest.raises(BackendError, match="already answering"):
        eng.start(settings, healthy=lambda host: True, spawn=lambda *a, **k: spawned.append(a),
                  notice=lambda: noticed.append(1))
    assert spawned == [] and noticed == []               # T865: no promise before a refusal
    assert "REFUSED" in eng.log_path().read_text(encoding="utf-8")


def test_start_refuses_on_memory_before_spawning(tmp_path, monkeypatch):
    settings, _ = _install(tmp_path, "iq3_xxs")
    monkeypatch.setattr(eng, "memory_free_bytes", lambda: (100, 100))
    with pytest.raises(BackendError, match="pins about"):
        eng.start(settings, healthy=lambda host: False, spawn=lambda *a, **k: pytest.fail("spawned"))


def test_start_returns_an_owned_engine_once_the_server_says_ready(tmp_path):
    settings, _ = _install(tmp_path, "iq3_xxs", port=8091)
    noticed, seen = [], {}

    def spawn(cmd, **kw):
        seen.update(cmd=cmd, cwd=kw["cwd"], env=kw["env"])
        kw["stdout"].write("loading the model ...\nready: http://127.0.0.1:8091/v1  (OpenAI)\n")
        kw["stdout"].flush()
        return _Proc()

    owned = eng.start(settings, healthy=lambda host: False, spawn=spawn, notice=lambda: noticed.append(1))
    assert owned.host == "http://127.0.0.1:8091" and owned.model_id == "flash-iq3_xxs"
    assert owned.registered is False and owned.args == tuple(seen["cmd"])
    assert noticed == [1] and seen["cwd"] == settings.strata_root and seen["env"]["PYTHONUNBUFFERED"] == "1"
    owned.log_file.close()


def test_a_server_that_exits_while_loading_is_a_failed_start_with_its_last_words(tmp_path):
    settings, _ = _install(tmp_path, "iq3_xxs")

    def spawn(cmd, **kw):
        kw["stdout"].write("the engine stopped unexpectedly: out of memory\n")
        kw["stdout"].flush()
        return _Proc(code=1)

    with pytest.raises(eng.EngineStartFailed, match="out of memory") as exc:
        eng.start(settings, healthy=lambda host: False, spawn=spawn)
    assert exc.value.retained_owned is None              # its exit was confirmed: nothing left to own


def test_stopping_asks_the_server_to_unload_before_the_kill(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(eng, "request_unload", lambda host, timeout=30.0: calls.append(("unload", host)))
    monkeypatch.setattr(ninfer_engine, "terminate_owned", lambda owned: calls.append(("terminate", owned.host)) or "proof")
    owned = eng.OwnedEngine(proc=_Proc(), host="http://127.0.0.1:8080", log_path=tmp_path / "l",
                            log_file=None, model_id="m", registered=False)
    assert eng.terminate_owned(owned) == "proof"
    assert calls == [("unload", owned.host), ("terminate", owned.host)]


def test_an_unregistered_engine_never_touches_litesuites_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path / "llm"))
    owned = eng.OwnedEngine(proc=_Proc(), host="http://127.0.0.1:8080", log_path=tmp_path / "l",
                            log_file=None, model_id="m", registered=False)
    assert ninfer_engine.unregister_owned(owned) is True
    assert not (tmp_path / "llm").exists()
