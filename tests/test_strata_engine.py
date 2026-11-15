"""strata_engine — the launcher for Strata (T0374). Every arm is pure or injected:
no model is loaded, no process is spawned, no memory or GPU is read from this machine."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from litetui import ninfer_engine, strata_engine as eng
from litetui.llm_backend import BackendError

GB = 10 ** 9

#: conftest stubs the port verdict per test (no test may ask a real port who holds it); these are
#: the real ones, read at module load before any fixture runs.
_REAL_FOREIGN_CLAUSE, _REAL_PROBE, _REAL_PORT_HOLDER = eng.foreign_clause, eng.probe, eng.port_holder


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
    (llm / "config.json").write_text('{"strataRoot": " D:/Example/Strata "}', encoding="utf-8")
    assert eng.strata_root(settings) == Path("D:/Example/Strata")
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


# ── who is on the port (T1316) ───────────────────────────────────────────────

def _world(monkeypatch, *, answer, holder):
    """The port as the test describes it: `answer` is what `probe` returns, `holder` what listens."""
    monkeypatch.setattr(eng, "foreign_clause", _REAL_FOREIGN_CLAUSE)
    monkeypatch.setattr(eng, "probe", lambda host, timeout=2.0: answer)
    monkeypatch.setattr(eng, "port_holder", lambda host: holder)


WHISPER = eng.Holder(True, 20044, "whisper-server.exe")


def test_a_foreign_health_answer_is_refused_naming_the_process_and_the_way_out(tmp_path, monkeypatch):
    settings, _ = _install(tmp_path, "iq3_s")
    _world(monkeypatch, answer=("foreign", {"status": "ok"}, "/health: 200, /v1/models: 404"), holder=WHISPER)
    spawned = []
    with pytest.raises(BackendError) as exc:
        eng.start(settings, healthy=lambda host: False, spawn=lambda *a, **k: spawned.append(a))
    assert str(exc.value) == (
        'port 8080 is held by whisper-server.exe (pid 20044), which is not a Strata server '
        '(/health: 200, /v1/models: 404) — stop it or change the "port" in strata-iq3_s.json; '
        'nothing was started.')
    assert spawned == [] and "REFUSED" in eng.log_path().read_text(encoding="utf-8")


def test_a_real_strata_is_still_attached_to_with_the_same_words(tmp_path, monkeypatch):
    settings, _ = _install(tmp_path, "iq3_s")
    _world(monkeypatch, answer=("strata", {"service": "strata", "loaded": True}, ""), holder=WHISPER)
    with pytest.raises(BackendError) as exc:
        eng.start(settings, healthy=lambda host: True, spawn=lambda *a, **k: pytest.fail("spawned"))
    assert str(exc.value) == "a Strata server is already answering at http://127.0.0.1:8080 — attach to it; nothing was started."


def test_a_port_held_by_something_that_answers_no_http_is_refused_naming_it(tmp_path, monkeypatch):
    settings, _ = _install(tmp_path, "iq3_s")
    _world(monkeypatch, answer=("silent", None, "no answer to /health"), holder=eng.Holder(True, 777, "nginx.exe"))
    with pytest.raises(BackendError, match=r"port 8080 is held by nginx\.exe \(pid 777\), which does not answer HTTP like a Strata server"):
        eng.start(settings, healthy=lambda host: False, spawn=lambda *a, **k: pytest.fail("spawned"))


def test_an_owner_that_cannot_be_read_is_said_not_guessed(tmp_path, monkeypatch):
    settings, _ = _install(tmp_path, "iq3_s")
    _world(monkeypatch, answer=("foreign", {}, "/health: 200, /v1/models: 404"),
           holder=eng.Holder(True, 4321, None, "AccessDenied"))
    with pytest.raises(BackendError, match=r"a process \(pid 4321; its name could not be read: AccessDenied\)"):
        eng.start(settings, healthy=lambda host: False, spawn=lambda *a, **k: pytest.fail("spawned"))
    _world(monkeypatch, answer=("foreign", {}, "/health: 200, /v1/models: 404"), holder=eng.Holder())
    with pytest.raises(BackendError, match=r"another server \(its owner could not be found\)"):
        eng.start(settings, healthy=lambda host: False, spawn=lambda *a, **k: pytest.fail("spawned"))


def test_a_strata_still_loading_gets_the_still_loading_words_not_a_foreign_refusal(tmp_path, monkeypatch):
    """Its process holds the port and /health does not answer yet: that is not 'held by python.exe'."""
    settings, _ = _install(tmp_path, "iq3_s")
    _world(monkeypatch, answer=("silent", None, "no answer to /health"), holder=eng.Holder(True, 555, "python.exe"))
    monkeypatch.setattr(eng, "engine_process_alive", lambda: True)
    with pytest.raises(BackendError, match="engine process is already running .*still loading") as exc:
        eng.start(settings, healthy=lambda host: False, spawn=lambda *a, **k: pytest.fail("spawned"))
    assert "held by" not in str(exc.value)


def test_a_free_port_and_a_real_server_with_its_model_unloaded_are_not_foreign(tmp_path, monkeypatch):
    settings, (path,) = _install(tmp_path, "iq3_s")
    cfg = eng.read_config(path)
    _world(monkeypatch, answer=("silent", None, "no answer to /health"), holder=eng.Holder())
    assert eng.refuse_reason(settings, cfg, "http://127.0.0.1:8080", healthy=lambda h: False) is None
    _world(monkeypatch, answer=("strata", {"service": "strata", "loaded": False}, ""), holder=WHISPER)
    assert eng.foreign_clause("http://127.0.0.1:8080", "Strata") is None


def test_an_explicit_host_setting_is_the_remedy_when_it_chose_the_port(tmp_path, monkeypatch):
    settings, _ = _install(tmp_path, "iq3_s")
    settings.strata_host = "http://127.0.0.1:8080"
    _world(monkeypatch, answer=("foreign", {}, "/health: 200, /v1/models: 404"), holder=WHISPER)
    with pytest.raises(BackendError, match=r"stop it or change strata_host in /settings"):
        eng.start(settings, healthy=lambda host: False, spawn=lambda *a, **k: pytest.fail("spawned"))


def test_probe_reads_service_then_falls_back_to_the_models_list(monkeypatch):
    def serve(**routes):
        monkeypatch.setattr(eng, "_get", lambda url, timeout: routes.get(url.rsplit("8080", 1)[-1], (None, None)))

    h = "http://127.0.0.1:8080"
    serve(**{"/health": (200, {"status": "ok", "service": "strata", "loaded": True})})
    assert _REAL_PROBE(h)[0] == "strata"
    serve(**{"/health": (200, {"status": "ok", "service": "llama.cpp"}), "/v1/models": (200, {"data": []})})
    kind, _, said = _REAL_PROBE(h)
    assert kind == "foreign" and "llama.cpp" in said          # it named itself: no fallback
    serve(**{"/health": (200, {"status": "ok"}), "/v1/models": (200, {"object": "list", "data": []})})
    assert _REAL_PROBE(h)[0] == "strata"                       # an older build: the models list tells
    serve(**{"/health": (200, {"status": "ok"}), "/v1/models": (404, None)})
    assert _REAL_PROBE(h) == ("foreign", {"status": "ok"}, "/health: 200, /v1/models: 404")
    serve(**{"/health": (None, None)})
    assert _REAL_PROBE(h)[0] == "silent"


def test_port_holder_names_the_listener_and_says_when_it_cannot_ask(monkeypatch):
    import collections
    import psutil
    C = collections.namedtuple("C", "status laddr pid")
    A = collections.namedtuple("A", "ip port")
    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": [
        C("LISTEN", A("192.168.1.5", 8080), 1),          # another interface: does not block loopback
        C("ESTABLISHED", A("127.0.0.1", 8080), 2),
        C("LISTEN", A("127.0.0.1", 9999), 3),
        C("LISTEN", A("127.0.0.1", 8080), 20044)])

    class P:
        def __init__(self, pid):
            self.pid = pid

        def name(self):
            return "whisper-server.exe"

    monkeypatch.setattr(psutil, "Process", P)
    got = _REAL_PORT_HOLDER("http://127.0.0.1:8080")
    assert (got.found, got.pid, got.exe) == (True, 20044, "whisper-server.exe")
    assert _REAL_PORT_HOLDER("http://127.0.0.1:7777").found is False
    assert _REAL_PORT_HOLDER("http://10.0.0.9:8080").found is False      # not this machine: not asked

    def denied(kind="tcp"):
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "net_connections", denied)
    got = _REAL_PORT_HOLDER("http://127.0.0.1:8080")
    assert got.found is False and got.error == "AccessDenied"


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
