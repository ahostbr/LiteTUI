"""xolotl_engine — the launcher for Xolotl (T0633). Every arm is pure or injected:
no model is loaded, no process is spawned, no memory or GPU is read from this machine."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from litetui import ninfer_engine, strata_engine, xolotl_engine as eng
from litetui.llm_backend import BackendError

GB = 10 ** 9
GIB = 1 << 30


class _Settings:
    xolotl_host = ""
    xolotl_root = ""
    xolotl_config = ""
    xolotl_max_context = None


@pytest.fixture(autouse=True)
def _no_real_process_ops(monkeypatch, tmp_path):
    """Same discipline as test_strata_engine: no real kill, no atexit, a bounded wait, and
    never the user's LiteSuite config, memory or GPU."""
    monkeypatch.setattr(eng, "XOLOTL_START_TIMEOUT_S", 0.3)
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path / "llm"))
    monkeypatch.setattr(ninfer_engine, "_kill", lambda proc: None)
    monkeypatch.setattr(strata_engine.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(strata_engine.jobkill, "create", lambda: None)
    monkeypatch.setattr(eng, "log_path", lambda: tmp_path / "logs" / "xolotl.log")
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    monkeypatch.setattr(strata_engine, "memory_free_bytes", lambda: (200 * GB, 400 * GB))
    monkeypatch.setattr(strata_engine, "gpu_memory_mib", lambda: None)


def _ds41(root: Path, *, ctx: int = 262144, arena: str | None = "48", pool: str | None = "4096", port: int = 8150) -> dict:
    args = ["-m", "C:/models/V41-XQ-1017-v3.gguf", "--engram", "C:/models/engram", "--ctx", str(ctx)]
    if arena is not None:
        args += ["--arena-gib", arena]
    if pool is not None:
        args += ["--conversation-cache-mib", pool]
    return {"exe": str(root / "build-win" / "ds41_generate.exe"), "args": args, "port": port,
            "model_name": "deepseek-v4.1-flash-xolotl", "tokenizer": "C:/models"}


def _install(tmp_path: Path, *names: str, venv: str = ".venv-serve", **cfg_kw) -> tuple[_Settings, list[Path]]:
    """A folder shaped like a built Xolotl checkout, with one DeepSeek-V4.1 config per name."""
    root = tmp_path / "xolotl"
    python = root / venv / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    (root / "serve").mkdir()
    (root / "serve" / "server.py").write_text("", encoding="utf-8")
    shim = root / "tools" / "v41" / "serve" / "serve_ds41.py"
    shim.parent.mkdir(parents=True)
    shim.write_text("", encoding="utf-8")
    paths = []
    for name in names:
        path = root / f"strata-{name}.json"
        path.write_text(json.dumps(_ds41(root, **cfg_kw)), encoding="utf-8")
        paths.append(path)
    settings = _Settings()
    settings.xolotl_root = str(root)
    return settings, paths


# ── which checkout, which model ──────────────────────────────────────────────

def test_the_checkout_is_the_setting_else_litesuites_else_the_default(tmp_path):
    settings = _Settings()
    llm = tmp_path / "llm"
    assert eng.xolotl_root(settings) == llm / "xolotl"
    llm.mkdir()
    (llm / "config.json").write_text('{"strataRoot": "F:/Strata", "xolotlRoot": " F:/REPO_CLONES/Xolotl "}',
                                     encoding="utf-8")
    assert eng.xolotl_root(settings) == Path("F:/REPO_CLONES/Xolotl")
    settings.xolotl_root = "D:/mine"
    assert eng.xolotl_root(settings) == Path("D:/mine")


def test_one_prepared_model_is_chosen_two_are_not_guessed_and_a_shared_settings_file_is_no_model(tmp_path):
    settings, (only,) = _install(tmp_path, "ds41-1017-v3")
    (only.parent / "strata-ds41-1017-v3.shared-settings.json").write_text('{"temperature": 1}', encoding="utf-8")
    assert eng.list_xolotl_configs(settings) == [only] and eng.xolotl_config(settings) == only
    (only.parent / "strata-ds41-1017-v2.json").write_text(only.read_text(encoding="utf-8"), encoding="utf-8")
    assert eng.xolotl_config(settings) is None
    settings.xolotl_config = str(only)
    assert eng.chosen_config(settings) == only


def test_the_host_is_the_setting_else_the_configs_port_else_8150(tmp_path):
    settings, _ = _install(tmp_path, "ds41", port=8151)
    assert eng.default_host(settings) == "http://127.0.0.1:8151"
    settings.xolotl_host = "http://10.0.0.5:9000/"
    assert eng.default_host(settings) == "http://10.0.0.5:9000"
    assert eng.default_host(_Settings()) == "http://127.0.0.1:8150"


# ── the engine's own flags ───────────────────────────────────────────────────

def test_a_deepseek_config_runs_its_shim_and_a_qwen_config_runs_the_plain_server(tmp_path):
    root = tmp_path / "x"
    assert eng.exe_stem({"exe": "F:\\REPO_CLONES\\Xolotl\\build-win\\ds41_generate.exe"}) == "ds41_generate"
    assert eng.server_script(root, {"exe": "a/ds41_generate.exe"}) == root / "tools" / "v41" / "serve" / "serve_ds41.py"
    assert eng.server_script(root, {"exe": "a/glm_generate"}) == root / "tools" / "glm" / "serve" / "serve_glm.py"
    assert eng.server_script(root, {"exe": "engine/strata.exe"}) == root / "serve" / "server.py"


def test_context_is_ctx_for_deepseek_and_max_context_for_qwen_and_the_original_is_left_alone():
    ds = {"exe": "ds41_generate.exe", "args": ["--ctx", "262144", "--kv-fp4"]}
    out = eng.with_context(ds, 131072)
    assert eng.config_context(out) == 131072 and eng.config_context(ds) == 262144
    assert eng.with_context(ds, 262144) is ds and eng.with_context(ds, None) is ds
    assert eng.with_context({"exe": "ds41_generate", "args": []}, 65536)["args"] == ["--ctx", "65536"]
    qwen = {"exe": "strata.exe", "args": ["--max-context", "32768"]}
    assert eng.with_context(qwen, 65536)["args"] == ["--max-context", "65536"]


# ── the memory gate ──────────────────────────────────────────────────────────

def test_the_pin_is_arena_plus_pool_plus_the_engines_own_share(tmp_path):
    cfg = _ds41(tmp_path, arena="48", pool="4096")
    assert eng.pinned_bytes(cfg) == int((48 + eng.XOLOTL_OVERHEAD_GIB) * GIB) + 4096 * (1 << 20)
    defaults = _ds41(tmp_path, arena=None, pool=None)       # the engine's own 60 GiB / 8192 MiB
    assert eng.pinned_bytes(defaults) == int((60 + eng.XOLOTL_OVERHEAD_GIB) * GIB) + 8192 * (1 << 20)
    assert eng.pinned_bytes(_ds41(tmp_path, arena="lots")) == 0     # cannot size -> no refusal on it


def test_memory_gate_passes_a_fit_and_names_each_shortfall(tmp_path):
    cfg = _ds41(tmp_path)                                   # 48 + 12.5 GiB + 4 GiB = ~69.3 GB
    gpu = (31500, 32607)
    assert eng.memory_refusal(cfg, ram_free=75 * GB, commit_free=110 * GB, gpu=gpu) is None
    ram = eng.memory_refusal(cfg, ram_free=40 * GB, commit_free=110 * GB, gpu=gpu)
    assert "Xolotl pins about" in ram and "--arena-gib" in ram
    assert "commit" in eng.memory_refusal(cfg, ram_free=75 * GB, commit_free=80 * GB, gpu=gpu)
    assert "holds the card" in eng.memory_refusal(cfg, ram_free=75 * GB, commit_free=110 * GB, gpu=(9000, 32607))
    assert eng.memory_refusal(cfg, ram_free=None, commit_free=None, gpu=None) is None


def test_a_qwen_config_in_a_xolotl_checkout_keeps_stratas_native_rule(monkeypatch):
    monkeypatch.setattr(strata_engine, "native_bytes", lambda cfg: 47 * GB)
    qwen = {"exe": "engine/strata.exe", "args": ["--native", "not-a-real-file.gguf"]}
    assert "Strata pins about" in eng.memory_refusal(qwen, ram_free=40 * GB, commit_free=78 * GB, gpu=None)


# ── the launch recipe ────────────────────────────────────────────────────────

def test_launch_plan_is_the_docs_serve_line(tmp_path):
    settings, (cfg_path,) = _install(tmp_path, "ds41-1017-v3", port=8150)
    argv, cwd, cfg, host = eng.launch_plan(settings)
    root = Path(settings.xolotl_root)
    assert argv == [str(root / ".venv-serve" / "Scripts" / "python.exe"), "-I", "-u", "-X", "utf8",
                    str(root / "tools" / "v41" / "serve" / "serve_ds41.py"),
                    "--engine", "strata", "--config", str(cfg_path), "--port", "8150"]
    assert cwd == root and host == "http://127.0.0.1:8150" and cfg["model_name"] == "deepseek-v4.1-flash-xolotl"


def test_setup_pys_venv_is_used_when_there_is_no_serve_venv(tmp_path):
    settings, _ = _install(tmp_path, "ds41", venv=".venv")
    assert eng.launch_plan(settings)[0][0] == str(Path(settings.xolotl_root) / ".venv" / "Scripts" / "python.exe")


def test_a_chosen_context_runs_from_a_copy_never_the_checkouts_own_file(tmp_path):
    settings, (cfg_path,) = _install(tmp_path, "ds41")
    before = cfg_path.read_text(encoding="utf-8")
    settings.xolotl_max_context = 131072
    argv, _cwd, cfg, _host = eng.launch_plan(settings)
    ran = Path(argv[argv.index("--config") + 1])
    assert ran != cfg_path and ran.parent == eng.log_path().parent
    assert eng.config_context(json.loads(ran.read_text(encoding="utf-8"))) == 131072 == eng.config_context(cfg)
    assert cfg_path.read_text(encoding="utf-8") == before


def test_a_missing_checkout_or_shim_says_what_is_missing(tmp_path):
    settings = _Settings()
    settings.xolotl_root = str(tmp_path / "nowhere")
    with pytest.raises(BackendError, match="github.com/Indras-Mirror/Xolotl"):
        eng.launch_plan(settings)
    settings, (cfg_path, _other) = _install(tmp_path, "a", "b")
    with pytest.raises(BackendError, match="/model"):
        eng.launch_plan(settings)
    settings.xolotl_config = str(cfg_path)
    (Path(settings.xolotl_root) / "tools" / "v41" / "serve" / "serve_ds41.py").unlink()
    with pytest.raises(BackendError, match="serve_ds41.py is missing"):
        eng.launch_plan(settings)


# ── start ────────────────────────────────────────────────────────────────────

class _Proc:
    pid = 4343

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def test_start_refuses_before_spawning_when_a_server_already_answers(tmp_path):
    settings, _ = _install(tmp_path, "ds41")
    spawned, noticed = [], []
    with pytest.raises(BackendError, match="Xolotl server is already answering"):
        eng.start(settings, healthy=lambda host: True, spawn=lambda *a, **k: spawned.append(a),
                  notice=lambda: noticed.append(1))
    assert spawned == [] and noticed == []
    assert "REFUSED" in eng.log_path().read_text(encoding="utf-8")


def test_start_refuses_on_memory_before_spawning(tmp_path, monkeypatch):
    settings, _ = _install(tmp_path, "ds41")
    monkeypatch.setattr(strata_engine, "memory_free_bytes", lambda: (30 * GB, 400 * GB))
    with pytest.raises(BackendError, match="Xolotl pins about"):
        eng.start(settings, healthy=lambda host: False, spawn=lambda *a, **k: pytest.fail("spawned"))


def test_start_returns_an_owned_engine_once_the_server_says_ready(tmp_path):
    settings, _ = _install(tmp_path, "ds41", port=8152)
    noticed, seen = [], {}

    def spawn(cmd, **kw):
        seen.update(cmd=cmd, cwd=kw["cwd"])
        kw["stdout"].write("loading ...\nready: http://127.0.0.1:8152/v1  (OpenAI: /v1/chat/completions)\n")
        kw["stdout"].flush()
        return _Proc()

    owned = eng.start(settings, healthy=lambda host: False, spawn=spawn, notice=lambda: noticed.append(1))
    assert owned.host == "http://127.0.0.1:8152" and owned.model_id == "deepseek-v4.1-flash-xolotl"
    assert owned.registered is False and noticed == [1] and seen["cwd"] == settings.xolotl_root
    assert seen["cmd"][1:5] == ["-I", "-u", "-X", "utf8"]
    owned.log_file.close()


def test_a_server_that_exits_while_loading_is_a_failed_start_named_xolotl(tmp_path):
    settings, _ = _install(tmp_path, "ds41")

    def spawn(cmd, **kw):
        kw["stdout"].write("engram: cannot open model-00047-of-00048.safetensors\n")
        kw["stdout"].flush()
        return _Proc(code=1)

    with pytest.raises(eng.EngineStartFailed, match="Xolotl exited before it was ready .*model-00047") as exc:
        eng.start(settings, healthy=lambda host: False, spawn=spawn)
    assert exc.value.retained_owned is None
