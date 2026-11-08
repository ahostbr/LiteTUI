"""T0839 — artifact roots (`ninferArtifactRoots`) and launch recipes (`<artifact>.launch.json`).

Ryan 2026-11-04: "new model still not in slash model" and "fix it correctly so it shows and
launches from litetui and litesuite". Every arm builds fake files in `tmp_path`; no engine is
started, no process is touched (spawn is injected).
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from litetui import ninfer_engine as eng
from litetui.llm_backend import BackendError
from litetui.plugins import model_switch


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Config, data and artifact folders all live under tmp_path; nothing real is read."""
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path / "llm"))
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(eng, "NINFER_START_TIMEOUT_S", 0.3)
    monkeypatch.setattr(eng, "_kill", lambda proc: None)
    monkeypatch.setattr(eng.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(eng.jobkill, "create", lambda: None)
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: 30000)
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    monkeypatch.setattr(eng, "free_port", lambda host="127.0.0.1": 49260)
    (tmp_path / "llm").mkdir()


def _write_config(tmp_path: Path, body: dict) -> None:
    (tmp_path / "llm" / "config.json").write_text(json.dumps(body), encoding="utf-8")


def _default_dir(tmp_path: Path) -> Path:
    d = tmp_path / "llm" / "ninfer-models"
    d.mkdir(exist_ok=True)
    return d


# ── roots ────────────────────────────────────────────────────────────────────

def test_extra_roots_are_scanned_deduped_and_a_missing_one_is_ignored(tmp_path):
    default = _default_dir(tmp_path)
    (default / "qwen.ninfer").write_bytes(b"x")
    extra = tmp_path / "g" / "ninfer"
    extra.mkdir(parents=True)
    (extra / "deepseek.ninfer").write_bytes(b"x")
    (extra / "notes.txt").write_bytes(b"x")
    _write_config(tmp_path, {"ninferArtifactRoots": [
        str(extra),
        str(extra / "..") + "\\ninfer",          # the same folder spelled differently
        str(default),                            # the default again
        str(tmp_path / "does-not-exist"),
        "", 7,                                   # junk entries
    ]})
    found = eng.list_ninfer_artifacts()
    assert [p.name for p in found] == ["deepseek.ninfer", "qwen.ninfer"]
    assert eng.in_default_artifacts_dir(default / "qwen.ninfer")
    assert not eng.in_default_artifacts_dir(extra / "deepseek.ninfer")


def test_no_config_or_bad_config_means_only_the_default_folder(tmp_path):
    (_default_dir(tmp_path) / "a.ninfer").write_bytes(b"x")
    assert [p.name for p in eng.list_ninfer_artifacts()] == ["a.ninfer"]
    (tmp_path / "llm" / "config.json").write_text("{not json", encoding="utf-8")
    assert [p.name for p in eng.list_ninfer_artifacts()] == ["a.ninfer"]
    _write_config(tmp_path, {"ninferArtifactRoots": "G:\\notalist"})
    assert [p.name for p in eng.list_ninfer_artifacts()] == ["a.ninfer"]


def test_the_picker_lists_an_extra_root_artifact_with_its_folder_and_recipe_flag(tmp_path):
    default = _default_dir(tmp_path)
    (default / "qwen.ninfer").write_bytes(b"x")
    extra = tmp_path / "g"
    extra.mkdir()
    art = extra / "deepseek.ninfer"
    art.write_bytes(b"x")
    (extra / "deepseek.ninfer.launch.json").write_text("{}", encoding="utf-8")
    _write_config(tmp_path, {"ninferArtifactRoots": [str(extra)]})

    class App:
        settings = type("S", (), {"ninfer_artifact": ""})()

    rows = dict(model_switch._ninfer_artifact_rows(App()))
    assert str(extra) not in rows[str(default / "qwen.ninfer")]
    assert str(extra) in rows[str(art)] and "recipe" in rows[str(art)]


# ── recipe ───────────────────────────────────────────────────────────────────

def _artifact(path: Path, directory: dict | None = None) -> Path:
    body = json.dumps(directory or {"components": {"text": {}, "mtp": {}, "vision": {}}}).encode()
    head = bytearray(eng.NINFER_V3_HEADER_BYTES)
    head[:8] = eng.NINFER_V3_MAGIC
    struct.pack_into("<Q", head, 8, len(body))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(head) + body)
    return path


def _recipe(art: Path, exe: Path, **over) -> dict:
    body = {"version": 1, "note": "n", "executable": str(exe), "modelId": "deepseek-v4.1-flash-coder-ninfer",
            "maxContext": 131072,
            "args": ["--ngram-table", "G:\\x\\engram.ninfer", "--expert-residency", "host", "--host-kv-mib", "1024",
                     "--spec", "dspark", "--draft-confidence", "0.7"]}
    body.update(over)
    eng.recipe_path(art).write_text(json.dumps(body), encoding="utf-8")
    return body


class _Proc:
    pid = 777

    def poll(self):
        return None


class _Settings:
    ninfer_host = ""
    ninfer_executable = ""
    ninfer_artifact = ""
    ninfer_max_context = 32768
    ninfer_max_concurrency = 4
    ninfer_kv_dtype = "int8"
    ninfer_kv_capacity = "auto"
    ninfer_host_kv_mib = 4096


def _start(settings, tmp_path):
    spawned = {}

    def spawn(cmd, **kw):
        spawned["cmd"] = cmd
        kw["stdout"].write("listening on http://127.0.0.1:49260\n")
        kw["stdout"].flush()
        return _Proc()

    owned = eng.start(settings, healthy=lambda h: False, spawn=spawn)
    return owned, spawned["cmd"]


def _setup(tmp_path):
    exe = tmp_path / "recipe-bin" / "ninfer-serve.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    art = _artifact(tmp_path / "g" / "DeepSeek.ninfer")
    s = _Settings()
    s.ninfer_artifact = str(art)
    return s, art, exe


def test_a_sidecar_gives_exactly_the_recipe_argv_and_nothing_from_settings(tmp_path):
    s, art, exe = _setup(tmp_path)
    body = _recipe(art, exe)
    owned, cmd = _start(s, tmp_path)
    assert cmd == [
        str(exe), str(art), "--host", "127.0.0.1", "--port", "49260",
        "--model-id", "deepseek-v4.1-flash-coder-ninfer", "--max-context", "131072",
        *body["args"],
    ]
    for banned in ("--kv-dtype", "--preserve-thinking", "--lm-head-draft", "--draft-tokens",
                   "--vision", "--max-concurrency", "--kv-capacity"):
        assert banned not in cmd
    assert cmd.count("--host-kv-mib") == 1 and cmd[cmd.index("--host-kv-mib") + 1] == "1024"
    assert owned.model_id == "deepseek-v4.1-flash-coder-ninfer"
    assert tuple(cmd[1:]) == owned.args
    log = (tmp_path / "data" / ".ninfer" / "litetui-ninfer-serve.log").read_text(encoding="utf-8")
    assert "--spec dspark" in log and "DeepSeek.ninfer.launch.json" in log


def test_the_recipes_context_beats_the_global_setting(tmp_path):
    s, art, exe = _setup(tmp_path)
    _recipe(art, exe)
    s.ninfer_max_context = 150000          # Ryan's global value, set for Qwen
    _, cmd = _start(s, tmp_path)
    assert cmd[cmd.index("--max-context") + 1] == "131072"


def test_recipe_without_modelid_or_maxcontext_uses_the_artifacts_id_and_the_default(tmp_path):
    exe = tmp_path / "bin" / "ninfer-serve.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    art = _artifact(tmp_path / "g" / "x.ninfer", {"model_id": "own-id", "components": {"text": {}}})
    s = _Settings()
    s.ninfer_artifact = str(art)
    body = _recipe(art, exe)
    del body["modelId"], body["maxContext"]
    eng.recipe_path(art).write_text(json.dumps(body), encoding="utf-8")
    owned, cmd = _start(s, tmp_path)
    assert cmd[cmd.index("--model-id") + 1] == "own-id" == owned.model_id
    assert cmd[cmd.index("--max-context") + 1] == "32768"   # no recipe maxContext: the setting (here 32768)
    s.ninfer_max_context = 150000
    _, cmd = _start(s, tmp_path)
    assert cmd[cmd.index("--max-context") + 1] == "150000"


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    json.dumps({"version": 2, "executable": "x"}),
    json.dumps({"version": 1}),
    json.dumps({"version": 1, "executable": "x", "args": "--spec dspark"}),
    json.dumps({"version": 1, "executable": "x", "args": [1]}),
    json.dumps({"version": 1, "executable": "x", "maxContext": -4}),
    json.dumps({"version": 1, "executable": "x", "modelId": ""}),
])
def test_a_bad_sidecar_refuses_in_words_and_never_falls_back(tmp_path, content):
    s, art, exe = _setup(tmp_path)
    eng.recipe_path(art).write_text(content, encoding="utf-8")
    s.ninfer_executable = str(exe)           # a perfectly good fallback exists; it must NOT be used
    spawned = []
    with pytest.raises(BackendError) as err:
        eng.start(s, healthy=lambda h: False, spawn=lambda *a, **k: spawned.append(a))
    assert "launch recipe" in str(err.value) and "DeepSeek.ninfer.launch.json" in str(err.value)
    assert spawned == []


def test_a_recipe_whose_executable_is_missing_refuses_naming_both_files(tmp_path):
    s, art, exe = _setup(tmp_path)
    _recipe(art, tmp_path / "gone" / "ninfer-serve.exe")
    s.ninfer_executable = str(exe)
    spawned = []
    with pytest.raises(BackendError) as err:
        eng.start(s, healthy=lambda h: False, spawn=lambda *a, **k: spawned.append(a))
    assert "gone" in str(err.value) and "launch.json" in str(err.value)
    assert spawned == []


def test_no_sidecar_keeps_todays_argv(tmp_path):
    exe = tmp_path / "bin" / "ninfer-serve.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    art = _artifact(tmp_path / "g" / "plain.ninfer", {"model_id": "m", "components": {"text": {}, "mtp": {}}})
    s = _Settings()
    s.ninfer_executable = str(exe)
    s.ninfer_artifact = str(art)
    _, cmd = _start(s, tmp_path)
    assert cmd == [str(exe), *eng.build_ninfer_args(
        art, 49260, "m", max_context=32768, max_concurrency=4, components=("text", "mtp"),
        kv_dtype="int8", kv_capacity="auto", host_kv_mib=4096)]
    assert "--kv-dtype" in cmd and "--spec" in cmd


def test_effective_executable_follows_the_recipe_and_never_raises(tmp_path):
    s, art, exe = _setup(tmp_path)
    assert eng.effective_executable(s) == eng.ninfer_executable(s)
    _recipe(art, exe)
    assert eng.effective_executable(s) == exe
    eng.recipe_path(art).write_text("{bad", encoding="utf-8")
    assert eng.effective_executable(s) == eng.ninfer_executable(s)
