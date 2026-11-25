"""T0839 — artifact roots (`ninferArtifactRoots`) and model defaults (`<artifact>.launch.json`).

Ryan 2026-11-04: "new model still not in slash model" and "fix it correctly so it shows and
launches from litetui and litesuite". Every arm builds fake files in `tmp_path`; no engine is
started, no process is touched (spawn is injected).
"""
from __future__ import annotations

import json
import struct
import subprocess
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
    monkeypatch.setattr(eng, "_ENGINE_FLAGS", {})      # T1383: the --help cache is per test
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


def test_the_picker_lists_an_extra_root_artifact_with_its_folder_and_defaults_mark(tmp_path):
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
    assert str(extra) in rows[str(art)] and "  · defaults" in rows[str(art)]
    assert "recipe" not in rows[str(art)].lower() and "· defaults" not in rows[str(default / "qwen.ninfer")]


# ── model defaults ───────────────────────────────────────────────────────────

def _artifact(path: Path, directory: dict | None = None) -> Path:
    body = json.dumps(directory or {"components": {"text": {}, "mtp": {}, "vision": {}}}).encode()
    head = bytearray(eng.NINFER_V3_HEADER_BYTES)
    head[:8] = eng.NINFER_V3_MAGIC
    struct.pack_into("<Q", head, 8, len(body))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(head) + body)
    return path


def _defaults(art: Path, exe: Path, **over) -> dict:
    body = {"version": 1, "note": "n", "executable": str(exe), "modelId": "deepseek-v4.1-flash-coder-ninfer",
            "maxContext": 131072,
            "args": ["--ngram-table", "G:\\x\\engram.ninfer", "--expert-residency", "host", "--host-kv-mib", "1024",
                     "--spec", "dspark", "--draft-confidence", "0.7"]}
    body.update(over)
    eng.model_defaults_path(art).write_text(json.dumps(body), encoding="utf-8")
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
    exe = tmp_path / "defaults-bin" / "ninfer-serve.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    art = _artifact(tmp_path / "g" / "DeepSeek.ninfer")
    s = _Settings()
    s.ninfer_artifact = str(art)
    return s, art, exe


def test_a_sidecar_gives_exactly_the_model_defaults_argv_and_nothing_from_settings(tmp_path):
    s, art, exe = _setup(tmp_path)
    body = _defaults(art, exe)
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


def test_the_model_defaults_context_beats_the_global_setting(tmp_path):
    s, art, exe = _setup(tmp_path)
    _defaults(art, exe)
    s.ninfer_max_context = 150000          # Ryan's global value, set for Qwen
    _, cmd = _start(s, tmp_path)
    assert cmd[cmd.index("--max-context") + 1] == "131072"


def test_model_defaults_without_modelid_or_maxcontext_use_the_artifacts_id_and_the_default(tmp_path):
    exe = tmp_path / "bin" / "ninfer-serve.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    art = _artifact(tmp_path / "g" / "x.ninfer", {"model_id": "own-id", "components": {"text": {}}})
    s = _Settings()
    s.ninfer_artifact = str(art)
    body = _defaults(art, exe)
    del body["modelId"], body["maxContext"]
    eng.model_defaults_path(art).write_text(json.dumps(body), encoding="utf-8")
    owned, cmd = _start(s, tmp_path)
    assert cmd[cmd.index("--model-id") + 1] == "own-id" == owned.model_id
    assert cmd[cmd.index("--max-context") + 1] == "32768"   # no maxContext in the file: the setting (here 32768)
    s.ninfer_max_context = 150000
    _, cmd = _start(s, tmp_path)
    assert cmd[cmd.index("--max-context") + 1] == "150000"


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    json.dumps({"version": 2, "executable": "x"}),
    json.dumps({"version": 1, "executable": 5}),     # T1383: absent is allowed now; a non-string is not
    json.dumps({"version": 1, "executable": "x", "args": "--spec dspark"}),
    json.dumps({"version": 1, "executable": "x", "args": [1]}),
    json.dumps({"version": 1, "executable": "x", "maxContext": -4}),
    json.dumps({"version": 1, "executable": "x", "modelId": ""}),
])
def test_a_bad_sidecar_refuses_in_words_and_never_falls_back(tmp_path, content):
    s, art, exe = _setup(tmp_path)
    eng.model_defaults_path(art).write_text(content, encoding="utf-8")
    s.ninfer_executable = str(exe)           # a perfectly good fallback exists; it must NOT be used
    spawned = []
    with pytest.raises(BackendError) as err:
        eng.start(s, healthy=lambda h: False, spawn=lambda *a, **k: spawned.append(a))
    assert "the model defaults file" in str(err.value) and "DeepSeek.ninfer.launch.json" in str(err.value)
    assert "recipe" not in str(err.value).lower()       # T1383-B: Ryan's word is model defaults
    assert spawned == []


def test_model_defaults_whose_executable_is_missing_refuse_naming_both_files(tmp_path):
    s, art, exe = _setup(tmp_path)
    _defaults(art, tmp_path / "gone" / "ninfer-serve.exe")
    s.ninfer_executable = str(exe)
    spawned = []
    with pytest.raises(BackendError) as err:
        eng.start(s, healthy=lambda h: False, spawn=lambda *a, **k: spawned.append(a))
    assert "gone" in str(err.value) and "launch.json" in str(err.value)
    assert str(err.value).startswith("the model defaults file ") and "recipe" not in str(err.value).lower()
    assert spawned == []


def test_a_model_defaults_start_never_adds_the_fast_prefill_kernel(tmp_path):
    """T1383: model defaults are one model's measured flag set; the kernel was not measured there.
    The setting is on and the engine lists the flag, and still their argv is untouched."""
    s, art, exe = _setup(tmp_path)
    body = _defaults(art, exe)
    s.ninfer_fast_prefill_kernel = True
    asked, spawned = [], {}

    def help_run(cmd, **kw):
        asked.append(cmd)
        raise AssertionError("a model defaults start must not ask the engine for optional flags")

    def spawn(cmd, **kw):
        spawned["cmd"] = cmd
        kw["stdout"].write("listening on http://127.0.0.1:49260\n")
        kw["stdout"].flush()
        return _Proc()

    eng.start(s, healthy=lambda h: False, spawn=spawn, help_run=help_run)
    assert "--fast-prefill-kernel" not in spawned["cmd"] and asked == []
    assert spawned["cmd"][-len(body["args"]):] == body["args"]


# ── T1383: model defaults need not name an engine build (Ryan 2026-11-21: "stop trying to pin engines") ──

def _start_with(settings, help_run):
    spawned = {}

    def spawn(cmd, **kw):
        spawned["cmd"] = cmd
        kw["stdout"].write("listening on http://127.0.0.1:49260\n")
        kw["stdout"].flush()
        return _Proc()

    eng.start(settings, healthy=lambda h: False, spawn=spawn, help_run=help_run)
    return spawned["cmd"]


def _help(*flags):
    return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="".join(f"  {f}   x\n" for f in flags), stderr="")


def _no_help(cmd, **kw):
    raise subprocess.TimeoutExpired(cmd, 15)


def _unpinned(tmp_path, *, exe_value=None):
    """Model defaults with no `executable` (or a blank one) and a configured engine of the user's own."""
    s, art, _ = _setup(tmp_path)
    configured = tmp_path / "configured" / "ninfer-serve.exe"
    configured.parent.mkdir()
    configured.write_bytes(b"MZ")
    s.ninfer_executable = str(configured)
    body = _defaults(art, configured)
    if exe_value is None:
        del body["executable"]
    else:
        body["executable"] = exe_value
    eng.model_defaults_path(art).write_text(json.dumps(body), encoding="utf-8")
    return s, art, configured, body


@pytest.mark.parametrize("exe_value", [None, "", "  "], ids=["absent", "empty", "blank"])
def test_model_defaults_without_an_executable_run_their_flags_on_the_configured_engine(tmp_path, exe_value):
    s, art, configured, body = _unpinned(tmp_path, exe_value=exe_value)
    cmd = _start_with(s, _help("--kv-dtype"))
    assert cmd == [str(configured), str(art), "--host", "127.0.0.1", "--port", "49260",
                   "--model-id", "deepseek-v4.1-flash-coder-ninfer", "--max-context", "131072", *body["args"]]
    assert eng.effective_executable(s) == configured


def test_model_defaults_without_an_executable_and_no_configured_engine_refuse_in_the_usual_words(tmp_path):
    s, _, configured, _ = _unpinned(tmp_path)
    configured.unlink()
    spawned = []
    with pytest.raises(BackendError) as err:
        eng.start(s, healthy=lambda h: False, spawn=lambda *a, **k: spawned.append(a), help_run=_help())
    assert "ninfer-serve not installed at" in str(err.value) and spawned == []


def test_a_cache_flag_the_unpinned_model_defaults_engine_lacks_is_left_out_with_one_log_line(tmp_path):
    s, _, _, body = _unpinned(tmp_path)
    s.ninfer_auto_long_anchors = True
    s.ninfer_derive_session_keys = True
    cmd = _start_with(s, _help("--kv-dtype", "--derive-session-keys"))       # an engine without anchors
    assert "--auto-long-anchors" not in cmd and cmd[-1] == "--derive-session-keys"
    assert cmd[-len(body["args"]) - 1:-1] == body["args"]                   # the model defaults' own flags as written
    log = (tmp_path / "data" / ".ninfer" / "litetui-ninfer-serve.log").read_text(encoding="utf-8")
    assert log.count("--auto-long-anchors left out") == 1 and "does not list them" in log


def test_an_unreadable_help_drops_cache_flags_only_for_model_defaults_that_name_no_engine(tmp_path):
    s, _, _, _ = _unpinned(tmp_path)
    s.ninfer_auto_long_anchors = True
    assert "--auto-long-anchors" not in _start_with(s, _no_help)
    assert "could not be read" in (tmp_path / "data" / ".ninfer" / "litetui-ninfer-serve.log").read_text(encoding="utf-8")


def test_model_defaults_that_name_their_engine_keep_todays_cache_flags_when_the_help_is_unreadable(tmp_path):
    s, art, exe = _setup(tmp_path)
    _defaults(art, exe)
    s.ninfer_auto_long_anchors = True
    cmd = _start_with(s, _no_help)
    assert cmd[0] == str(exe) and cmd[-1] == "--auto-long-anchors"


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


def test_effective_executable_follows_the_model_defaults_and_never_raises(tmp_path):
    s, art, exe = _setup(tmp_path)
    assert eng.effective_executable(s) == eng.ninfer_executable(s)
    _defaults(art, exe)
    assert eng.effective_executable(s) == exe
    eng.model_defaults_path(art).write_text("{bad", encoding="utf-8")
    assert eng.effective_executable(s) == eng.ninfer_executable(s)


# ── T1310: upstream DSpine (a drafter artifact of its own) beside the old dflash-embedded DSpine ──

def _two_builds(tmp_path):
    old = tmp_path / "ninfer-all-dspine-76c35c85" / "ninfer-serve.exe"
    new = tmp_path / "ninfer-all-c6a560d1" / "ninfer-serve.exe"
    for exe in (old, new):
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
    return old, new


def test_the_old_dflash_model_defaults_start_exactly_as_before(tmp_path):
    """Ryan's 290 tok/s setup: the fork build, the drafter inside the artifact's dflash component."""
    old, _ = _two_builds(tmp_path)
    art = _artifact(tmp_path / "g" / "swift15_nvfp4_dspine_p.ninfer", {"components": {"text": {}, "dflash": {}}})
    args = ["--spec", "dflash", "--draft-tokens", "15", "--lm-head-draft", "--kv-dtype", "fp8",
            "--max-concurrency", "2"]
    eng.model_defaults_path(art).write_text(json.dumps(
        {"version": 1, "executable": str(old), "modelId": "swift15-nvfp4-dspine", "args": args}), encoding="utf-8")
    s = _Settings()
    s.ninfer_artifact = str(art)
    s.ninfer_max_context = 65536
    _, cmd = _start(s, tmp_path)
    assert cmd == [str(old), str(art), "--host", "127.0.0.1", "--port", "49260",
                   "--model-id", "swift15-nvfp4-dspine", "--max-context", "65536", *args]
    assert "--draft-model" not in cmd


def test_dspine_model_defaults_pass_the_drafter_and_the_users_context(tmp_path):
    _, new = _two_builds(tmp_path)
    drafter = _artifact(tmp_path / "g" / "dspine" / "Qwen3.8-27B-DSpine-b16-q8.ninfer", {"components": {"dspine": {}}})
    art = _artifact(tmp_path / "g" / "swift15_nvfp4_dspine_up.ninfer", {"components": {"text": {}, "dflash": {}}})
    args = ["--spec", "dspine", "--draft-model", str(drafter), "--draft-tokens", "15", "--kv-dtype", "fp8",
            "--max-concurrency", "2"]
    eng.model_defaults_path(art).write_text(json.dumps(
        {"version": 1, "executable": str(new), "modelId": "swift15-nvfp4-dspine-up", "args": args}), encoding="utf-8")
    s = _Settings()
    s.ninfer_artifact = str(art)
    s.ninfer_max_context = 65536            # no maxContext in the file: his setting decides
    _, cmd = _start(s, tmp_path)
    assert cmd[0] == str(new)
    assert cmd[cmd.index("--spec") + 1] == "dspine" and cmd.count("--spec") == 1
    assert cmd[cmd.index("--draft-model") + 1] == str(drafter)
    assert cmd[cmd.index("--max-context") + 1] == "65536"


@pytest.mark.parametrize("args, says", [
    (["--spec", "dspine", "--draft-model", "MISSING"], "--draft-model"),
    (["--spec", "dspine", "--draft-tokens", "15"], "--spec dspine"),
    (["--spec", "dspine", "--draft-model"], "--draft-model"),
])
def test_dspine_model_defaults_without_their_drafter_refuse_before_any_spawn(tmp_path, args, says):
    _, new = _two_builds(tmp_path)
    art = _artifact(tmp_path / "g" / "m.ninfer", {"components": {"text": {}}})
    args = [str(tmp_path / "gone.ninfer") if a == "MISSING" else a for a in args]
    eng.model_defaults_path(art).write_text(json.dumps({"version": 1, "executable": str(new), "args": args}),
                                    encoding="utf-8")
    s = _Settings()
    s.ninfer_artifact = str(art)
    spawned = []
    with pytest.raises(BackendError) as err:
        eng.start(s, healthy=lambda h: False, spawn=lambda *a, **k: spawned.append(a))
    assert says in str(err.value) and "m.ninfer.launch.json" in str(err.value)
    assert spawned == []


def test_a_drafter_artifact_is_not_a_model_choice(tmp_path):
    default = _default_dir(tmp_path)
    _artifact(default / "model.ninfer", {"components": {"text": {}, "dflash": {}}})
    _artifact(default / "drafter.ninfer", {"components": {"dspine": {}}})
    (default / "unreadable.ninfer").write_bytes(b"x")        # no header: stays listed, as before
    assert [p.name for p in eng.list_ninfer_artifacts()] == ["model.ninfer", "unreadable.ninfer"]


# ── T1383-B: Ryan 2026-11-21, "there not recipes there just the default settings of a model" ──

def _user_strings(module_path: Path) -> list[tuple[int, str]]:
    """Every string literal in the module a user can be shown, with its line: plain strings and the
    literal parts of f-strings. Left out: docstrings (developer text) and the one stored value
    "recipe" (a saved ninfer_speed_preset key, kept so old settings keep working). Comments are not
    in the syntax tree at all."""
    import ast

    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)}
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings and node.value != "recipe"]


@pytest.mark.parametrize("module", ["ninfer_engine.py", "plugins/model_switch.py", "settings_screen.py"])
def test_no_string_a_user_reads_says_recipe(module):
    found = [(line, s) for line, s in _user_strings(Path(eng.__file__).parent / module) if "recipe" in s.lower()]
    assert found == []


def test_the_sweep_can_see_a_recipe_string(tmp_path):
    """The positive control: the same reader finds the word in a message, an f-string and a label,
    and skips only the docstring and the stored value."""
    src = tmp_path / "m.py"
    src.write_text('"""a recipe docstring"""\n'
                   'x = "launch recipe gone"\n'
                   'y = f"launch recipe {x} is unusable"\n'
                   'z = {"recipe": ("recipe", "Recipe as written")}\n', encoding="utf-8")
    assert [line for line, s in _user_strings(src) if "recipe" in s.lower()] == [2, 3, 4]


def test_a_saved_recipe_speed_preset_still_means_flags_as_written(tmp_path):
    """The stored value stays "recipe": a settings.json saved before T1383-B loads, is a choice
    the Settings dropdown offers (Textual's Select refuses a value outside its options), and leaves
    the model defaults' flags exactly as written."""
    from litetui import settings as settings_mod

    (tmp_path / settings_mod.SETTINGS_FILENAME).write_text(json.dumps({"ninfer_speed_preset": "recipe"}), encoding="utf-8")
    saved = settings_mod.load(tmp_path).ninfer_speed_preset
    assert saved == "recipe"
    dropdown = [(p.label, p.key) for p in eng.SPEED_PRESETS.values()]     # settings_screen's options, as built there
    assert saved in [key for _, key in dropdown]
    assert eng.SPEED_PRESETS[saved].label.startswith("Model defaults as written")
    args = ["--expert-residency", "disk", "--expert-host-mib", "46080", "--prefill-chunk", "4096"]
    out, note = eng.apply_speed_preset(args, saved, free_ram_gib=200)
    assert out == args and "as written" in note


# ── LOOPKV-466: the loop-KV default also rides a model defaults start (every Inkling artifact has one) ──

INKLING_DIR = {"components": {"text": {"config": {"model_type": "inkling2"}}, "vision": {}}}


def _inkling_defaults(tmp_path, args):
    exe = tmp_path / "configured" / "ninfer-serve.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    art = _artifact(tmp_path / "g" / "Inkling.ninfer", INKLING_DIR)
    s = _Settings()
    s.ninfer_artifact = str(art)
    s.ninfer_executable = str(exe)
    eng.model_defaults_path(art).write_text(json.dumps({
        "version": 1, "modelId": "inkling-2-mini", "maxContext": 150000, "args": args}), encoding="utf-8")
    return s


def test_an_inkling_model_defaults_start_gets_loop_kv_last_full_after_its_own_flags(tmp_path):
    s = _inkling_defaults(tmp_path, ["--vision", "--preserve-thinking"])
    cmd = _start_with(s, _help("--vision", "--loop-kv"))
    assert cmd[-4:] == ["--vision", "--preserve-thinking", "--loop-kv", "last-full"]
    assert cmd.count("--loop-kv") == 1


def test_a_model_defaults_file_that_sets_loop_kv_is_not_overridden(tmp_path):
    s = _inkling_defaults(tmp_path, ["--vision", "--loop-kv", "last-all"])
    cmd = _start_with(s, _help("--vision", "--loop-kv"))
    assert cmd.count("--loop-kv") == 1 and cmd[cmd.index("--loop-kv") + 1] == "last-all"


def test_an_inkling_model_defaults_start_on_an_engine_without_the_flag_is_unchanged(tmp_path):
    s = _inkling_defaults(tmp_path, ["--vision"])
    assert "--loop-kv" not in _start_with(s, _help("--vision"))
    assert "--loop-kv" not in _start_with(s, _no_help)


def test_a_non_inkling_model_defaults_start_is_unchanged_by_the_loop_kv_default(tmp_path):
    s, art, exe = _setup(tmp_path)                              # DeepSeek: no model_type in its header
    body = _defaults(art, exe)
    cmd = _start_with(s, _help("--loop-kv", "--spec"))
    assert "--loop-kv" not in cmd and cmd[-len(body["args"]):] == body["args"]


# ── /model offers one Inkling: the Basalt pack (Ryan 2026-11-24) ─────────────────────────────────────────

def _basalt_inkling_pack(tmp_path):
    root = tmp_path / "llm" / "basalt"
    (root / "packs").mkdir(parents=True)
    body = json.dumps({"arch": "inkling2"}).encode()
    head = struct.pack("<8sIIQ40s", b"BASALTMF", 2, 1, (64 << 10) + len(body), b"")
    head += struct.pack("<64sQQ32s16s", b"meta", 64 << 10, len(body), b"", b"")
    (root / "packs" / "Inkling-2-Mini-NVFP4-MX8.basalt").write_bytes(head.ljust(64 << 10, b"\0") + body)


def _picker_app(tmp_path):
    default = _default_dir(tmp_path)
    _artifact(default / "Inkling-NInfer.ninfer", INKLING_DIR)
    _artifact(default / "Qwen.ninfer", {"components": {"text": {"config": {"model_type": "qwen3_5_text"}}}})
    (default / "Inkling-NInfer.ninfer.launch.json").write_text("{}", encoding="utf-8")

    class App:
        settings = type("S", (), {"ninfer_artifact": ""})()

    return App()


def test_ninfers_inkling_artifacts_are_not_offered_while_basalt_holds_an_inkling_pack(tmp_path):
    app = _picker_app(tmp_path)
    assert {Path(k).name for k, _ in model_switch._ninfer_artifact_rows(app)} == {"Inkling-NInfer.ninfer", "Qwen.ninfer"}
    _basalt_inkling_pack(tmp_path)
    assert {Path(k).name for k, _ in model_switch._ninfer_artifact_rows(app)} == {"Qwen.ninfer"}


def test_a_chosen_inkling_artifact_still_lists_nothing_extra_and_nothing_is_deleted(tmp_path):
    app = _picker_app(tmp_path)
    _basalt_inkling_pack(tmp_path)
    app.settings = type("S", (), {"ninfer_artifact": str(_default_dir(tmp_path) / "Inkling-NInfer.ninfer")})()
    model_switch._ninfer_artifact_rows(app)
    assert (_default_dir(tmp_path) / "Inkling-NInfer.ninfer").is_file()
    assert (_default_dir(tmp_path) / "Inkling-NInfer.ninfer.launch.json").is_file()

