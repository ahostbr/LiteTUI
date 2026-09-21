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


@pytest.fixture(autouse=True)
def _no_real_process_ops(monkeypatch):
    """Every pid here is FAKE. Never let a real jobkill/taskkill target one — a nonexistent
    pid makes taskkill enumerate the whole table (seconds) and a REUSED pid is a live,
    unrelated process. Also bound the readiness wait so a never-ready fake spawn cannot loop
    for NINFER_START_TIMEOUT_S (180s). Per-test monkeypatches still override these."""
    monkeypatch.setattr(eng, "NINFER_START_TIMEOUT_S", 0.3)
    monkeypatch.setattr(eng, "_kill", lambda proc: None)
    # atexit MUST be faked: start() registers atexit.register(stop, owned); an un-faked
    # callback survives the monkeypatch restore and runs real cleanup at interpreter exit.
    monkeypatch.setattr(eng.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(eng.jobkill, "create", lambda: None)
    monkeypatch.setattr(eng.jobkill, "assign", lambda job, pid: False)
    monkeypatch.setattr(eng.jobkill, "close", lambda job: True)
    monkeypatch.setattr(eng.jobkill, "terminate", lambda job, exit_code=1: True)
    monkeypatch.setattr(eng.jobkill, "active_process_count", lambda job: 0)
    monkeypatch.setattr(eng, "_JOB_DRAIN_TIMEOUT", 0.05)
    monkeypatch.setattr(eng, "_JOB_DRAIN_INTERVAL", 0.005)
    # terminate_owned proves exit via llm_backend.wait_for_exit on the retained handle;
    # a fake proc has no real exit, so treat it as terminated (round-trip stop tests).
    from litetui import llm_backend as _lb
    monkeypatch.setattr(_lb, "wait_for_exit", lambda proc, **kw: True)


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


def test_vision_is_enabled_only_for_an_artifact_that_carries_it():
    """Ryan 15:0x 2026-09-17: view_image on the 35B -> HTTP 400 vision disabled on every
    request after it, because the engine was started without --vision."""
    with_v = eng.build_ninfer_args(Path("a.ninfer"), 1, "m", components=("text", "vision", "mtp"))
    without = eng.build_ninfer_args(Path("a.ninfer"), 1, "m", components=("text", "mtp"))
    assert "--vision" in with_v and "--vision" not in without
    assert "--vision" not in eng.build_ninfer_args(Path("a"), 1, "m", components=("vision",), vision=False)


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


def test_concurrency_is_passed_only_when_chosen_and_clamped_to_the_engines_range():
    """T892 — serving.md:760: `--max-concurrency N`, valid range 1..8, default 1.
    Absent = the engine's own default; a chosen value is clamped like the draft window."""
    assert "--max-concurrency" not in eng.build_ninfer_args(Path("a"), 1, "m")
    four = eng.build_ninfer_args(Path("a"), 1, "m", max_concurrency=4)
    assert four[four.index("--max-concurrency") + 1] == "4"
    high = eng.build_ninfer_args(Path("a"), 1, "m", max_concurrency=12)
    assert high[high.index("--max-concurrency") + 1] == "8"
    assert "--max-concurrency" not in eng.build_ninfer_args(Path("a"), 1, "m", max_concurrency=0)
    assert "--max-concurrency" not in eng.build_ninfer_args(Path("a"), 1, "m", max_concurrency=True)


def test_concurrency_is_read_back_from_an_argv_with_the_engines_default_when_absent():
    assert eng.concurrency_in(["x.ninfer", "--port", "1", "--max-concurrency", "4"]) == 4
    assert eng.concurrency_in(["x.ninfer", "--port", "1"]) == 1
    assert eng.concurrency_in(["x.ninfer", "--max-concurrency"]) == 1
    assert eng.concurrency_in(None) == 1


def test_running_argv_picks_the_process_on_the_port_from_the_table(monkeypatch):
    """An attached engine carries SOMEBODY ELSE'S flags: the process table is the
    only honest source (no route reports capacity). Port-matched, never first-row."""
    class _Out:
        stdout = ("C:\\n\\ninfer-serve.exe a.ninfer --host 127.0.0.1 --port 60915 --max-concurrency 3\n"
                  "C:\\n\\ninfer-serve.exe b.ninfer --host 127.0.0.1 --port 56504\n")
    monkeypatch.setattr(eng.ttyguard, "run", lambda cmd, timeout=30: _Out())
    assert eng.concurrency_in(eng.running_argv(60915)) == 3
    assert eng.concurrency_in(eng.running_argv(56504)) == 1
    assert eng.running_argv(1) is None

    def _boom(cmd, timeout=30):
        raise OSError("no powershell")
    monkeypatch.setattr(eng.ttyguard, "run", _boom)
    assert eng.running_argv(60915) is None


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
    entry = next(e for e in json.loads((tmp_path / "config.json").read_text())["extraEndpoints"] if e["kind"] == "ninfer")
    assert entry["owner"] == "litetui" and entry["pid"] == 777   # the hub's owner label (T819)
    eng.stop(owned)
    assert eng.registered_host() is None


def test_start_passes_the_settings_concurrency_and_keeps_the_argv_on_the_handle(tmp_path, monkeypatch):
    """T892: the setting reaches the spawn, and the handle keeps the argv so the
    readout (/engine status, seat_snapshot) reports what RUNS, not what was wished."""
    s, spawn, spawned = _ready_engine(tmp_path, monkeypatch, log_text="listening on http://127.0.0.1:49260\n")
    monkeypatch.setattr(eng.atexit, "register", lambda *a, **k: None)
    s.ninfer_max_concurrency = 3
    owned = eng.start(s, healthy=lambda h: False, spawn=spawn)
    assert spawned["cmd"][spawned["cmd"].index("--max-concurrency") + 1] == "3"
    assert eng.concurrency_in(owned.args) == 3
    eng.stop(owned)


def test_an_earlier_runs_ready_line_does_not_count(tmp_path, monkeypatch):
    """14:1x 2026-09-17: the log is appended across spawns; the 12:xx run's "listening on"
    made start() return at SPAWN while 20.6 GiB were still loading, and the caller's
    first request killed the engine. Only bytes after THIS spawn's header count."""
    s, spawn, _ = _ready_engine(tmp_path, monkeypatch, log_text="loading weights | 20.6 GiB\n")
    lp = eng.log_path(); lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("--- litetui spawn earlier ---\nlistening on http://127.0.0.1:1\n", encoding="utf-8")
    monkeypatch.setattr(eng, "NINFER_START_TIMEOUT_S", 1.0)
    monkeypatch.setattr(eng, "_kill", lambda proc: None)
    with pytest.raises(BackendError) as excinfo:
        eng.start(s, healthy=lambda h: False, spawn=spawn)
    assert "did not become ready" in str(excinfo.value)
    assert eng.registered_host() is None


def test_a_header_without_a_model_id_names_the_engine_after_the_file(tmp_path, monkeypatch):
    """The 35B-A3B artifact's header has no model_id; the pinned fallback advertised the MoE
    as qwen3.8-27b in LiteSuite's picker (Ryan's screenshot, 14:2x 2026-09-17)."""
    s, spawn, spawned = _ready_engine(tmp_path, monkeypatch, log_text="listening on http://127.0.0.1:49260\n")
    monkeypatch.setattr(eng, "read_artifact_directory", lambda p: {"components": {"text": {}}})
    monkeypatch.setattr(eng.atexit, "register", lambda *a, **k: None)
    owned = eng.start(s, healthy=lambda h: False, spawn=spawn)
    stem = Path(s.ninfer_artifact).stem
    assert owned.model_id == stem and stem != "qwen3.8-27b"
    assert spawned["cmd"][spawned["cmd"].index("--model-id") + 1] == stem
    eng.stop(owned)


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


# ── T877: a refusal leaves a record ─────────────────────────────────────────
#
# Ryan, 2026-09-18 00:2x: `/model` then `/engine start`, and nothing happened.
# There was nothing to read afterwards — no log line, no process, no registry
# entry, no VRAM movement — because every refusal raises ABOVE the `open(lp)`
# in `start()`. Three agents guessed at four causes for twenty minutes.
#
#     A FAILURE THAT WRITES NOTHING IS INDISTINGUISHABLE FROM A COMMAND THAT
#     NEVER RAN — and those two need completely different fixes.

def _log_text(tmp_path: Path) -> str:
    p = eng.log_path()
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_a_full_card_refusal_is_written_to_the_log(tmp_path, monkeypatch):
    """RED BEFORE T877: the refusal raised and the log did not exist at all."""
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(eng, "data_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(eng, "log_path", lambda: tmp_path / ".ninfer" / "engine.log")
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: 6441)
    with pytest.raises(BackendError):
        eng.start(_Settings(), healthy=lambda h: False, spawn=_never_spawns)
    text = (tmp_path / ".ninfer" / "engine.log").read_text(encoding="utf-8")
    assert "engine start requested" in text
    assert "REFUSED" in text and "6441" in text


def test_a_missing_exe_refusal_names_the_path_it_looked_at(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(eng, "log_path", lambda: tmp_path / ".ninfer" / "engine.log")
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: None)
    monkeypatch.setattr(eng, "ninfer_executable", lambda s: tmp_path / "nope.exe")
    with pytest.raises(BackendError):
        eng.start(_Settings(), healthy=lambda h: False, spawn=_never_spawns)
    text = (tmp_path / ".ninfer" / "engine.log").read_text(encoding="utf-8")
    assert "REFUSED" in text and "nope.exe" in text


def test_the_request_line_is_written_even_when_nothing_refuses_it(tmp_path, monkeypatch):
    """CONTROL: the request marker is not a by-product of refusing.

    Without this, 'engine start requested' could be written only on the refusal
    paths and the arms above would still pass — an observable that exists only
    when the thing under test fails is not a record of the thing.
    """
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(eng, "log_path", lambda: tmp_path / ".ninfer" / "engine.log")
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: None)
    exe = tmp_path / "ninfer-serve.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(eng, "ninfer_executable", lambda s: exe)
    monkeypatch.setattr(eng, "ninfer_artifact", lambda s: _artifact(tmp_path, {"components": ["text"]}))
    with pytest.raises(BackendError):   # the fake spawn never becomes ready
        eng.start(_Settings(), healthy=lambda h: False, spawn=_never_spawns)
    text = (tmp_path / ".ninfer" / "engine.log").read_text(encoding="utf-8")
    assert "engine start requested" in text
    assert "REFUSED" not in text


class _DeadProc:
    pid = 4242
    def poll(self):  # never exits, never becomes ready
        return None
    def terminate(self):
        pass
    def wait(self, timeout=None):
        pass


def _never_spawns(*a, **kw):
    return _DeadProc()


# ── ownership survives the handle (Ryan, 2026-09-18) ────────────────────────
#
# "it spawned that ninfer server then refused to close it with /engine stop
# saying it didnt spawn it ... killing litetui didnt close the server"
#
# Measured that morning: ninfer-serve.exe pid 269276, 10.7 GB resident, parent
# gone, and the registry entry LiteTUI wrote still saying owner=litetui with
# that pid. `register_host` had recorded both fields since T819/T820 and every
# reader discarded them, so the app could not read its own receipt.


def test_the_registry_entry_keeps_the_owner_and_pid_it_was_written_with(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    eng.register_host("http://127.0.0.1:64977", pid=269276)

    entry = eng.registered_entry()
    assert entry is not None
    assert entry["baseUrl"] == "http://127.0.0.1:64977"
    assert entry["owner"] == "litetui", "LiteTUI must be able to read its own receipt"
    assert entry["pid"] == 269276, "the pid is the only handle that survives a restart"


def test_registered_host_still_answers_with_just_the_url(tmp_path, monkeypatch):
    """The URL-only reader has other callers (the launcher refuses on it)."""
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    eng.register_host("http://127.0.0.1:64977", pid=1)
    assert eng.registered_host() == "http://127.0.0.1:64977"


def test_stop_registered_fails_closed_and_keeps_the_record(tmp_path, monkeypatch):
    """Option A (2026-09-21): without a live handle or a verified process identity, a
    pid-only `taskkill` could terminate a REUSED pid — so stop_registered no longer
    kills and no longer drops the record. It fails closed and preserves the entry for a
    future identity-verified stop (superseding the old kill-and-clear behaviour)."""
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    eng.register_host("http://127.0.0.1:64977", pid=269276)

    killed: list = []
    monkeypatch.setattr(eng.ttyguard, "run", lambda cmd, **kw: killed.append(cmd))

    assert eng.stop_registered(eng.registered_entry()) is False
    assert killed == [], "a pid-only kill is refused"
    assert eng.registered_entry() is not None, "the record is preserved, not erased"


def test_stop_registered_refuses_an_entry_with_no_pid(tmp_path, monkeypatch):
    """An entry written by something else — or by a version before the pid was
    recorded — is not ours to kill."""
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    eng.register_host("http://127.0.0.1:64977")          # no pid

    killed: list = []
    monkeypatch.setattr(eng.ttyguard, "run", lambda cmd, **kw: killed.append(cmd))

    assert eng.stop_registered(eng.registered_entry()) is False
    assert killed == [], "nothing may be killed without a recorded pid"
    assert eng.registered_entry() is not None, "and the entry stays for its real owner"


def test_stop_registered_preserves_a_possibly_dead_ghost_pending_identity(tmp_path, monkeypatch):
    """Under option A a pid-only reader cannot tell 'already dead' from 'reused', so it
    must not act on either — the record is PRESERVED (no kill attempted at all). Safely
    clearing a ghost entry needs the verified-identity slice; until then a stale record
    is stopped/cleared where it was started. (Supersedes the old clear-even-if-dead.)"""
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    eng.register_host("http://127.0.0.1:64977", pid=269276)

    killed: list = []
    monkeypatch.setattr(eng.ttyguard, "run", lambda cmd, **kw: killed.append(cmd))

    assert eng.stop_registered(eng.registered_entry()) is False
    assert killed == [], "no taskkill is issued on an unverifiable pid"
    assert eng.registered_entry() is not None


# ── terminate_owned + EngineStartFailed (ownership retention) ────────────────

import io as _io


def _owned(job=None, dead=True):
    proc = type("P", (), {"pid": 4242, "poll": staticmethod(lambda: (0 if dead else None))})()
    return eng.OwnedEngine(proc=proc, host="http://127.0.0.1:9/v1", log_path=Path("x.log"),
                           log_file=_io.StringIO(), model_id="m", job=job, args=())


def test_terminate_owned_job_confirms_and_unregisters(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    eng.register_host("http://127.0.0.1:9/v1", pid=4242)
    r = eng.terminate_owned(_owned(job=7))           # autouse: terminate/close True, count 0, wait True
    assert r.tree == "confirmed" and r.main_exited and not r.retained
    assert eng.registered_host() is None             # unregistered ONLY on the confirmed proof


def test_terminate_owned_retains_and_preserves_record_when_not_drained(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    eng.register_host("http://127.0.0.1:9/v1", pid=4242)
    monkeypatch.setattr(eng.jobkill, "active_process_count", lambda job: 5)   # never drains
    r = eng.terminate_owned(_owned(job=7))
    assert r.tree == "unknown" and r.retained
    assert eng.registered_host() is not None         # record PRESERVED on an uncertain outcome


def test_terminate_owned_no_job_is_main_only(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    eng.register_host("http://127.0.0.1:9/v1", pid=4242)
    r = eng.terminate_owned(_owned(job=None))        # dead proc -> wait proves exit
    assert r.main_exited and r.tree == "unknown" and not r.retained
    assert eng.registered_host() is None


def test_start_failure_surfaces_the_retained_handle_not_a_registry_row(tmp_path, monkeypatch):
    from litetui import llm_backend
    monkeypatch.setattr(llm_backend, "wait_for_exit", lambda proc, **kw: False)  # cleanup uncertain
    s, spawn, _ = _ready_engine(tmp_path, monkeypatch, log_text="server failed during startup\n")
    with pytest.raises(eng.EngineStartFailed) as ei:
        eng.start(s, healthy=lambda h: False, spawn=spawn)
    assert ei.value.retained_owned is not None       # the HANDLE is the retention...
    assert ei.value.retained_owned.host == "http://127.0.0.1:49260"
    assert eng.registered_host() is None             # ...NOT a registry row (no foreign-overwriting register)


def test_start_failure_retains_nothing_when_cleanup_confirmed(tmp_path, monkeypatch):
    # autouse wait_for_exit->True + count 0 -> cleanup confirmed -> nothing to own.
    s, spawn, _ = _ready_engine(tmp_path, monkeypatch, log_text="server failed during startup\n")
    with pytest.raises(eng.EngineStartFailed) as ei:
        eng.start(s, healthy=lambda h: False, spawn=spawn)
    assert ei.value.retained_owned is None
    assert isinstance(ei.value, BackendError)        # still an ordinary BackendError to old callers


def test_terminate_owned_close_raise_retains_handle(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    def _boom(job):
        raise OSError("CloseHandle failed")
    monkeypatch.setattr(eng.jobkill, "close", _boom)      # a raw raise would lose the handle
    r = eng.terminate_owned(_owned(job=7))
    assert r.tree == "confirmed" and r.retained and r.error == "OSError"


def test_terminate_owned_unregister_failure_retains(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "unregister_owned", lambda owned: False)   # bookkeeping failure
    r = eng.terminate_owned(_owned(job=7))
    assert r.retained and r.error == "unregister_failed"   # registry failure != lost handle


def test_unregister_owned_spares_a_replacement_at_the_same_host(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    host = "http://127.0.0.1:9/v1"
    cfg = eng.litesuite_llm_dir() / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"version": 1, "extraEndpoints": [
        {"baseUrl": host, "kind": "ninfer", "owner": "litetui", "pid": 4242},   # ours
        {"baseUrl": host, "kind": "ninfer", "owner": "litetui", "pid": 999},     # a replacement, same host
    ]}))
    assert eng.unregister_owned(_owned(job=None)) is True   # _owned.proc.pid == 4242
    left = [e for e in json.loads(cfg.read_text())["extraEndpoints"] if e.get("kind") == "ninfer"]
    assert [e["pid"] for e in left] == [999]               # only OURS removed; the replacement survives


def test_fail_start_surfaces_retained_on_a_cleanup_exception(tmp_path, monkeypatch):
    def _boom(owned):
        raise RuntimeError("cleanup blew up")
    monkeypatch.setattr(eng, "terminate_owned", _boom)     # any unexpected cleanup failure
    s, spawn, _ = _ready_engine(tmp_path, monkeypatch, log_text="server failed during startup\n")
    with pytest.raises(eng.EngineStartFailed) as ei:
        eng.start(s, healthy=lambda h: False, spawn=spawn)
    assert ei.value.retained_owned is not None             # the handle is surfaced, not lost
