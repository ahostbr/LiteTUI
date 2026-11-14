"""T1311 follow-up — /engine stop and /engine restart stop an engine an EARLIER LiteTUI
session started, once its identity is verified.

Ryan, 2026-11-14, with a screenshot of 0.30.0 after /agent-new:

    "its refusting to stop ninfer after slash agent-new"

The screen said, twice, that a LiteTUI-registered engine was recorded (pid 42772, port 49705)
but "this session holds no handle for it and cannot safely verify that pid". /agent-new
continues the window as a NEW LiteTUI process; the engine stays with the old one (a thin
parent that holds its job), so the new session has only the registry record.

The record now carries what makes the pid safe to adopt: the process's creation time and exe
path, read at spawn, plus the port in its baseUrl. A later session checks all of them against
the live process; all match -> tree-kill, proof of exit, record removed. Any differs -> nothing
is killed, the record is kept and the refusal names the check.

🔴 NO MODEL IS EVER LOADED HERE. The "engine" is a real `python` child that listens on a port,
answers /health, has a child of its own and never exits by itself. Config and data roots live
under tmp_path.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import jobkill
from litetui import ninfer_engine as eng
from litetui.ninfer_backend import NInferBackend
from litetui.plugins import model_switch

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="taskkill and job objects are Windows")

#: The base interpreter, not a venv's python.exe: on Windows that is a launcher that runs the
#: real interpreter as a CHILD, so the pid we hold would not be the one listening on the port.
PYTHON = getattr(sys, "_base_executable", None) or sys.executable

STUB = r'''
import http.server, signal, subprocess, sys, threading, time
for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"])
open(sys.argv[2], "w").write(str(child.pid))
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass
srv = http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H)
print(f"listening on http://127.0.0.1:{sys.argv[1]}", flush=True)
srv.serve_forever()
'''

_K32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None


def _pin(pid: int):
    """An OpenProcess handle: the pid cannot be recycled while it is open."""
    h = _K32.OpenProcess(0x00100000 | 0x1000, False, pid)   # SYNCHRONIZE | QUERY_LIMITED
    assert h, f"could not open pid {pid}"
    return h


def _gone(h, timeout_s: float) -> bool:
    return _K32.WaitForSingleObject(h, int(timeout_s * 1000)) == 0


class _Settings:
    ninfer_host = ""
    ninfer_executable = ""
    ninfer_artifact = ""
    ninfer_max_context = 32768
    ninfer_max_concurrency = 1
    ninfer_kv_dtype = "fp8"
    ninfer_kv_capacity = "auto"
    ninfer_host_kv_mib = 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path / "llm"))
    (tmp_path / "llm").mkdir()
    monkeypatch.setattr(eng.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: 30000)
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    port = _free_port()
    monkeypatch.setattr(eng, "free_port", lambda host="127.0.0.1": port)
    exe = tmp_path / "bin" / "ninfer-serve.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    art = tmp_path / "models" / "Edge0.ninfer"
    art.parent.mkdir()
    art.write_bytes(b"not a real artifact")
    s = _Settings()
    s.ninfer_executable, s.ninfer_artifact = str(exe), str(art)
    made = SimpleNamespace(tmp=tmp_path, settings=s, port=port, procs=[], owned=[],
                           pidfile=tmp_path / "child.pid", config=tmp_path / "llm" / "config.json")
    script = tmp_path / "stub_engine.py"
    script.write_text(STUB, encoding="utf-8")

    def spawn(cmd, **kw):
        proc = subprocess.Popen([PYTHON, str(script), str(port), str(made.pidfile)],
                                stdin=subprocess.DEVNULL, stdout=kw["stdout"], stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        made.procs.append(proc)
        return proc

    made.spawn = spawn
    yield made
    for owned in made.owned:                 # session A's job handle and log, as its exit would close them
        if owned.job is not None:
            jobkill.close(owned.job)
        eng._close_log(owned)
    for proc in made.procs:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)


def _session_a(box):
    """Session A: LiteTUI's own start, on the stub. It keeps the engine's job open (as the thin
    parent /agent-new leaves behind does) and hands nothing to the next session."""
    owned = eng.start(box.settings, healthy=lambda h: False, spawn=box.spawn)
    box.owned.append(owned)
    pid = owned.proc.pid
    deadline = time.monotonic() + 10
    while not box.pidfile.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    child = int(box.pidfile.read_text())
    return pid, child


def _entry(box) -> dict:
    return next(e for e in json.loads(box.config.read_text(encoding="utf-8"))["extraEndpoints"]
                if e.get("kind") == "ninfer")


def _rewrite(box, **changes):
    body = json.loads(box.config.read_text(encoding="utf-8"))
    for e in body["extraEndpoints"]:
        if e.get("kind") == "ninfer":
            e.update(changes)
    box.config.write_text(json.dumps(body), encoding="utf-8")


def test_the_spawn_records_the_creation_time_and_exe_of_the_engine(box):
    pid, _child = _session_a(box)
    import psutil
    entry = _entry(box)
    proc = psutil.Process(pid)
    assert entry["pid"] == pid and entry["owner"] == "litetui"
    assert abs(entry["created"] - proc.create_time()) < 0.01
    assert Path(entry["exe"]).resolve() == Path(proc.exe()).resolve()
    assert entry["baseUrl"] == f"http://127.0.0.1:{box.port}"


def test_an_engine_session_a_started_is_stopped_by_session_b(box):
    pid, child = _session_a(box)
    h_main, h_child = _pin(pid), _pin(child)
    try:
        b = NInferBackend(box.settings)          # session B: no handle, only the record
        assert b._owned is None
        msg = b.stop_engine()
        assert "stopped the engine LiteTUI started" in msg and "confirmed gone" in msg, msg
        assert b.last_stop_freed is True
        assert _gone(h_main, 10), "the engine is still running"
        assert _gone(h_child, 10), "its child is still running"
        assert eng.registered_entry() is None, "the record of a stopped engine stays"
    finally:
        _K32.CloseHandle(h_main)
        _K32.CloseHandle(h_child)


def test_a_pid_reused_by_a_different_exe_is_refused(box):
    pid, _child = _session_a(box)
    _rewrite(box, exe=str(box.tmp / "bin" / "ninfer-serve.exe"))   # what the record names, not what runs
    h = _pin(pid)
    try:
        msg = NInferBackend(box.settings).stop_engine()
        assert "cannot safely verify" in msg and "record is preserved" in msg, msg
        assert "not the recorded" in msg and "ninfer-serve.exe" in msg, "the refusal must name the exe"
        assert not _gone(h, 1), "a process that is not the recorded exe was killed"
        assert eng.registered_entry() is not None
    finally:
        _K32.CloseHandle(h)


def test_a_pid_with_a_different_creation_time_is_refused(box):
    pid, _child = _session_a(box)
    _rewrite(box, created=_entry(box)["created"] - 3600)          # recorded an hour before this process
    h = _pin(pid)
    try:
        msg = NInferBackend(box.settings).stop_engine()
        assert "creation time differs" in msg and "reused" in msg, msg
        assert not _gone(h, 1)
        assert eng.registered_entry() is not None
    finally:
        _K32.CloseHandle(h)


def test_a_pid_not_listening_on_the_recorded_port_is_refused(box):
    pid, _child = _session_a(box)
    other = _free_port()
    _rewrite(box, baseUrl=f"http://127.0.0.1:{other}")
    h = _pin(pid)
    try:
        msg = NInferBackend(box.settings).stop_engine()
        assert f"not listening on port {other}" in msg, msg
        assert not _gone(h, 1)
        assert eng.registered_entry() is not None
    finally:
        _K32.CloseHandle(h)


def test_a_record_from_an_older_litetui_is_refused_and_says_what_it_lacks(box, monkeypatch):
    """0.30.0 wrote {owner, kind, pid} only: that record can never be adopted."""
    box.config.write_text(json.dumps({"version": 1, "extraEndpoints": [
        {"baseUrl": "http://127.0.0.1:49705", "kind": "ninfer", "owner": "litetui", "pid": 42772}]}),
        encoding="utf-8")
    killed = []
    monkeypatch.setattr(eng.ttyguard, "run", lambda cmd, **kw: killed.append(cmd))
    msg = NInferBackend(box.settings).stop_engine()
    assert "no creation time or exe path" in msg and "older LiteTUI" in msg, msg
    assert killed == [] and eng.registered_entry() is not None


def test_a_recorded_engine_that_already_exited_has_its_record_cleared(box):
    gone = subprocess.Popen([PYTHON, "-c", "pass"])
    identity = eng.process_identity(gone.pid)
    gone.wait()
    eng.register_host(f"http://127.0.0.1:{box.port}", pid=gone.pid, identity=identity)
    b = NInferBackend(box.settings)
    msg = b.stop_engine()
    assert "has already exited" in msg and b.last_stop_freed is True, msg
    assert eng.registered_entry() is None


def test_restart_stops_session_as_engine_then_starts_one(box, monkeypatch):
    pid, child = _session_a(box)
    h_main, h_child = _pin(pid), _pin(child)
    started = []

    def start(settings, **kw):
        started.append(1)
        return eng.OwnedEngine(proc=SimpleNamespace(pid=9, poll=lambda: None), host="http://127.0.0.1:9",
                               log_path=Path("x.log"), log_file=None, model_id="Edge0", registered=False)

    monkeypatch.setattr(eng, "start", start)
    b = NInferBackend(box.settings)
    said = []
    tasks = []
    app = SimpleNamespace(backend=b, settings=box.settings, said=said, connected=0,
                          system_message=lambda m, *a, **k: said.append(str(m)),
                          call_from_thread=lambda fn, *a, **k: fn(*a, **k),
                          _chat_running=lambda: False,
                          run_worker=lambda coro, **kw: (tasks.append(asyncio.ensure_future(coro))
                                                         or SimpleNamespace(_task=tasks[-1], cancel=tasks[-1].cancel)))
    app.connect = lambda: setattr(app, "connected", app.connected + 1)

    async def drive():
        model_switch._cmd_engine(app, "engine", "restart")
        while tasks:
            await tasks.pop(0)
            await asyncio.sleep(0)
    try:
        asyncio.run(drive())
        assert _gone(h_main, 10) and _gone(h_child, 10), said
        assert started == [1], said
        assert any("confirmed gone" in s for s in said), said
        assert not any("restarts only an engine this LiteTUI started" in s for s in said), said
    finally:
        _K32.CloseHandle(h_main)
        _K32.CloseHandle(h_child)
