"""basalt_engine — the launcher for Basalt (T1355). Every arm is pure or injected: no model is
loaded, no process is spawned, and no port, memory or GPU of this machine is asked."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from litetui import basalt_engine as eng
from litetui import ninfer_engine, strata_engine
from litetui.llm_backend import BackendError

SILENT = ("silent", None, "no answer to /health")
#: The autouse fixture below stubs the probe per test; the real one, read at load.
_REAL_PROBE = eng.probe


class _Settings:
    basalt_host = ""
    basalt_root = ""
    basalt_config = ""
    basalt_max_context = None
    basalt_kv = None
    basalt_vram_reserve_mib = None
    basalt_expert_cache = None
    basalt_conversation_cache_mib = None
    basalt_spec = None
    basalt_vision = None
    basalt_extra_settings = None


@pytest.fixture(autouse=True)
def _no_real_process_ops(monkeypatch, tmp_path):
    """No real kill, no atexit, a bounded wait, and never this machine's LiteSuite config, GPU,
    ports or CUDA install (a dev box may have a real Basalt on 8091)."""
    monkeypatch.setattr(eng, "BASALT_START_TIMEOUT_S", 0.3)
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path / "llm"))
    monkeypatch.setattr(ninfer_engine, "_kill", lambda proc: None)
    monkeypatch.setattr(strata_engine.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(strata_engine.jobkill, "create", lambda: None)
    monkeypatch.setattr(eng, "log_path", lambda: tmp_path / "logs" / "basalt.log")
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    monkeypatch.setattr(strata_engine, "gpu_memory_mib", lambda: None)
    monkeypatch.setattr(eng, "probe", lambda host, timeout=2.0: SILENT)
    monkeypatch.setattr(strata_engine, "port_holder", lambda host: strata_engine.Holder())
    monkeypatch.setattr(eng, "runtime_missing", lambda root: None)


def _install(tmp_path: Path, *packs: str, folder: str = "packs") -> tuple[_Settings, list[Path]]:
    """A folder shaped like a Basalt release with its venv made, and one pack per name."""
    root = tmp_path / "basalt"
    for rel in (".venv/Scripts/python.exe", "bin/basalt.exe", "serve/server.py"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    (root / folder).mkdir(exist_ok=True)
    paths = []
    for name in packs:
        path = root / folder / f"{name}.basalt"
        path.write_bytes(b"BASALTMF")
        paths.append(path)
    settings = _Settings()
    settings.basalt_root = str(root)
    return settings, paths


# ── which install, which model ───────────────────────────────────────────────

def test_the_install_is_the_setting_else_litesuites_else_beside_the_other_engines(tmp_path):
    settings = _Settings()
    llm = tmp_path / "llm"
    assert eng.basalt_root(settings) == llm / "basalt"
    llm.mkdir()
    (llm / "config.json").write_text('{"strataRoot": "D:/Example/Strata", "basaltRoot": " D:/Example/Basalt "}',
                                     encoding="utf-8")
    assert eng.basalt_root(settings) == Path("D:/Example/Basalt")
    settings.basalt_root = "D:/mine"
    assert eng.basalt_root(settings) == Path("D:/mine")


def test_a_model_is_a_pack_file_one_is_chosen_two_are_not_guessed(tmp_path):
    settings, (only,) = _install(tmp_path, "Qwen3.8-FN-IQ3_S")
    (only.parent / "notes.txt").write_text("not a pack", encoding="utf-8")
    (only.parent / "half.basalt.partial").write_bytes(b"x")
    assert eng.list_packs(settings) == [only] and eng.basalt_pack(settings) == only
    assert eng.read_config(only) == {"pack": str(only), "model_name": only.stem}
    assert eng.read_config(only.parent / "notes.txt") is None
    assert eng.config_context(eng.read_config(only)) is None       # the context is LiteTUI's setting
    second = only.parent.parent / "models" / "Other.basalt"        # a release's own download folder
    second.parent.mkdir()
    second.write_bytes(b"BASALTMF")
    assert eng.list_configs(settings) == sorted([only, second]) and eng.chosen_config(settings) is None
    settings.basalt_config = str(second)
    assert eng.basalt_pack(settings) == second


def test_the_host_is_the_setting_else_port_8091(tmp_path):
    settings = _Settings()
    assert eng.default_host(settings) == "http://127.0.0.1:8091"
    settings.basalt_host = "http://10.0.0.5:9000/"
    assert eng.default_host(settings) == "http://10.0.0.5:9000"


# ── the run config LiteTUI writes ────────────────────────────────────────────

def test_with_nothing_set_the_config_is_one_model_on_loopback_with_litetuis_defaults(tmp_path):
    settings, (pack,) = _install(tmp_path, "Qwen3.8-FN-IQ3_S")
    assert eng.run_config(settings, pack, 8091) == {
        "version": 2,
        "models": [{"model": str(pack), "alias": "Qwen3.8-FN-IQ3_S", "max-context": 131072, "kv": "int8",
                    "conversation-cache-mib": 8192}],
        "server": {"host": "127.0.0.1", "port": 8091, "image-remote-only": True},
    }


def test_a_short_context_keeps_basalts_own_kv_and_a_chosen_kv_always_wins(tmp_path):
    settings, (pack,) = _install(tmp_path, "m")
    settings.basalt_max_context = 8192
    assert "kv" not in eng.run_config(settings, pack, 8091)["models"][0]
    settings.basalt_kv = "fp16"
    settings.basalt_max_context = 150000
    assert eng.run_config(settings, pack, 8091)["models"][0]["kv"] == "fp16"


def test_every_load_option_lands_under_basalts_own_key(tmp_path):
    settings, (pack,) = _install(tmp_path, "m")
    settings.basalt_max_context = 150000
    settings.basalt_kv = "k8v4"
    settings.basalt_vram_reserve_mib = 2000
    settings.basalt_expert_cache = "9000"
    settings.basalt_conversation_cache_mib = 0
    settings.basalt_spec = 4
    settings.basalt_vision = "dynamic"
    settings.basalt_extra_settings = '{"prefill": "auto:16384", "resident-budget-gib": 40}'
    assert eng.run_config(settings, pack, 8091)["models"][0] == {
        "model": str(pack), "alias": "m", "max-context": 150000, "kv": "k8v4", "vram-reserve-mib": 2000,
        "expert-cache": 9000, "conversation-cache-mib": 0, "spec": 4, "vision": "dynamic",
        "prefill": "auto:16384", "resident-budget-gib": 40}
    settings.basalt_expert_cache = "auto"
    assert eng.run_config(settings, pack, 8091)["models"][0]["expert-cache"] == "auto"


@pytest.mark.parametrize("field,value,words", [
    ("basalt_kv", "int4", "basalt_kv: choose one of fp16, int8, q4_0, k8v4"),
    ("basalt_vision", "gpu", "basalt_vision: choose one of off, dynamic, cpu"),
    ("basalt_vram_reserve_mib", 100, "refuses less than 256 MiB"),
    ("basalt_expert_cache", "lots", "write auto or a whole number"),
    ("basalt_spec", -1, "basalt_spec: must be zero or more"),
    ("basalt_extra_settings", "{not json", "basalt_extra_settings: not valid JSON"),
    ("basalt_extra_settings", '["route-tail-skip"]', "write a JSON object"),
    ("basalt_extra_settings", '{"model": "other.basalt"}', "model is set by /model, not here"),
    ("basalt_max_context", 300000, "past the model's trained 262144 tokens"),
])
def test_a_load_option_basalt_would_refuse_is_refused_before_anything_starts(tmp_path, field, value, words):
    settings, (pack,) = _install(tmp_path, "m")
    setattr(settings, field, value)
    with pytest.raises(BackendError, match=words) as exc:
        eng.run_config(settings, pack, 8091)
    assert "nothing was started" in str(exc.value)


def test_the_lossy_settings_are_named_only_when_one_is_on():
    settings = _Settings()
    assert eng.lossy_settings(settings) == []
    settings.basalt_extra_settings = '{"prefill": "auto", "route-tail-skip": false, "route-prior": 0}'
    assert eng.lossy_settings(settings) == []
    settings.basalt_extra_settings = '{"route-tail-skip": 7, "relaxed-drafting": true}'
    assert eng.lossy_settings(settings) == ["route-tail-skip", "relaxed-drafting"]


# ── the launch recipe ────────────────────────────────────────────────────────

def test_launch_plan_runs_the_server_itself_on_a_config_written_beside_the_log(tmp_path):
    settings, (pack,) = _install(tmp_path, "Qwen3.8-FN-IQ3_S")
    before = sorted(p.name for p in pack.parent.parent.rglob("*") if p.is_file())
    argv, cwd, cfg, host = eng.launch_plan(settings)
    root = Path(settings.basalt_root)
    ran = eng.log_path().with_name("Qwen3.8-FN-IQ3_S.litetui.json")
    assert argv == [str(root / ".venv" / "Scripts" / "python.exe"), "-m", "serve.server",
                    "--engine-bin", str(root / "bin" / "basalt.exe"), "--config", str(ran)]
    assert cwd == root and host == "http://127.0.0.1:8091" and cfg["model_name"] == "Qwen3.8-FN-IQ3_S"
    assert json.loads(ran.read_text(encoding="utf-8")) == eng.run_config(settings, pack, 8091)
    assert sorted(p.name for p in root.rglob("*") if p.is_file()) == before, "nothing is written into the install"


def test_what_is_missing_is_said_in_words(tmp_path, monkeypatch):
    settings = _Settings()
    settings.basalt_root = str(tmp_path / "nowhere")
    with pytest.raises(BackendError, match="github.com/jesdga95/basalt"):
        eng.launch_plan(settings)
    settings, (first, _second) = _install(tmp_path, "a", "b")
    with pytest.raises(BackendError, match="/model to pick one"):
        eng.launch_plan(settings)
    settings.basalt_config = str(first.with_name("gone.basalt"))
    with pytest.raises(BackendError, match="no Basalt model chosen"):
        eng.launch_plan(settings)
    settings.basalt_config = str(first)
    settings.basalt_host = "http://127.0.0.1"
    with pytest.raises(BackendError, match="names no port"):
        eng.launch_plan(settings)
    settings.basalt_host = ""
    monkeypatch.setattr(eng, "runtime_missing", lambda root: "the CUDA 13 runtime (cudart64_13.dll) is missing; nothing was started.")
    with pytest.raises(BackendError, match="CUDA 13 runtime"):
        eng.launch_plan(settings)


# ── start ────────────────────────────────────────────────────────────────────

class _Proc:
    pid = 4343

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def _never(*a, **k):
    pytest.fail("spawned")


def test_start_refuses_before_spawning_when_a_basalt_server_already_answers(tmp_path, monkeypatch):
    settings, _ = _install(tmp_path, "m")
    noticed = []
    with pytest.raises(BackendError, match="a Basalt server is already answering at http://127.0.0.1:8091 — attach"):
        eng.start(settings, healthy=lambda host: True, spawn=_never, notice=lambda: noticed.append(1))
    assert noticed == [] and "REFUSED" in eng.log_path().read_text(encoding="utf-8")
    # Up, but its model is unloaded or still loading: a second server could not bind the port.
    monkeypatch.setattr(eng, "probe", lambda host, timeout=2.0: ("basalt", {"service": "basalt", "loaded": False}, ""))
    with pytest.raises(BackendError, match="already answering at .* without its model loaded"):
        eng.start(settings, healthy=lambda host: False, spawn=_never)


def test_a_foreign_server_on_the_port_is_refused_naming_the_process(tmp_path, monkeypatch):
    settings, _ = _install(tmp_path, "m")
    monkeypatch.setattr(eng, "probe", lambda host, timeout=2.0: ("foreign", {"service": "strata"}, "/health says service='strata'"))
    monkeypatch.setattr(strata_engine, "port_holder", lambda host: strata_engine.Holder(True, 20044, "python.exe"))
    with pytest.raises(BackendError) as exc:
        eng.start(settings, healthy=lambda host: False, spawn=_never)
    assert str(exc.value) == (
        "port 8091 is held by python.exe (pid 20044), which is not a Basalt server (/health says service='strata') "
        "— stop it or change basalt_host in /settings; nothing was started.")
    monkeypatch.setattr(eng, "probe", lambda host, timeout=2.0: SILENT)
    monkeypatch.setattr(strata_engine, "port_holder", lambda host: strata_engine.Holder(True, 777, "nginx.exe"))
    with pytest.raises(BackendError, match=r"held by nginx\.exe \(pid 777\), which does not answer HTTP like a Basalt server"):
        eng.start(settings, healthy=lambda host: False, spawn=_never)


def test_a_basalt_still_loading_and_a_held_card_are_refused_in_their_own_words(tmp_path, monkeypatch):
    settings, _ = _install(tmp_path, "m")
    monkeypatch.setattr(eng, "engine_process_alive", lambda: True)
    with pytest.raises(BackendError, match="a Basalt process is already running .*still loading"):
        eng.start(settings, healthy=lambda host: False, spawn=_never)
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    monkeypatch.setattr(strata_engine, "gpu_memory_mib", lambda: (9000, 32607))
    with pytest.raises(BackendError, match="something else holds the card"):
        eng.start(settings, healthy=lambda host: False, spawn=_never)


def test_the_servers_own_ready_line_is_not_ready_the_weights_line_is(tmp_path):
    """Basalt prints `ready` at once and loads behind it; a turn sent then would wait out the load."""
    settings, _ = _install(tmp_path, "m")

    def early(cmd, **kw):
        kw["stdout"].write("[server] ready    http://127.0.0.1:8091/v1  openai+anthropic\n"
                           "[server] loading weights into cuda0      [=====>   ]  9.0/26.4 GiB\n")
        kw["stdout"].flush()
        return _Proc()

    with pytest.raises(BackendError, match="Basalt did not become ready"):
        eng.start(settings, healthy=lambda host: False, spawn=early)

    noticed, seen = [], {}

    def loaded(cmd, **kw):
        seen.update(cmd=cmd, cwd=kw["cwd"])
        kw["stdout"].write("[server] ready    http://127.0.0.1:8091/v1  openai+anthropic\n"
                           "[server] weights  loaded, ctx=131072 images=off\n")
        kw["stdout"].flush()
        return _Proc()

    owned = eng.start(settings, healthy=lambda host: False, spawn=loaded, notice=lambda: noticed.append(1))
    assert owned.host == "http://127.0.0.1:8091" and owned.model_id == "m" and noticed == [1]
    assert seen["cwd"] == settings.basalt_root and seen["cmd"][1:3] == ["-m", "serve.server"]


def test_an_engine_that_dies_first_says_its_own_last_words(tmp_path):
    settings, _ = _install(tmp_path, "m")

    def dies(cmd, **kw):
        kw["stdout"].write("basalt: this build needs a Blackwell GPU (compute capability 12.0); found 8.9\n")
        kw["stdout"].flush()
        return _Proc(code=1)

    with pytest.raises(BackendError, match="Basalt exited before it was ready — .*needs a Blackwell GPU"):
        eng.start(settings, healthy=lambda host: False, spawn=dies)


def test_only_a_server_that_names_itself_basalt_is_one(monkeypatch):
    monkeypatch.setattr(eng, "probe", _REAL_PROBE)      # the real probe, against fake /health answers
    answers = {}
    monkeypatch.setattr(strata_engine, "_get", lambda url, timeout: answers["health"])
    answers["health"] = (None, None)
    assert eng.probe("http://127.0.0.1:1")[0] == "silent"
    answers["health"] = (200, {"status": "ok", "loaded": False, "loading": True, "service": "basalt"})
    assert eng.probe("http://127.0.0.1:1")[0] == "basalt"
    answers["health"] = (200, {"status": "ok", "loaded": True, "service": "strata"})
    assert eng.probe("http://127.0.0.1:1")[::2] == ("foreign", "/health says service='strata'")
    answers["health"] = (200, {"status": "ok"})
    assert eng.probe("http://127.0.0.1:1")[::2] == ("foreign", "/health: 200, with no service name")
