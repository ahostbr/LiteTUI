"""T1355: an engine LiteTUI started stays stoppable after /agent-new (Strata, Xolotl, Basalt).

Ryan, testing Basalt: /engine start, then /agent new created basalt-test-1 in the same window, and that seat's
/engine stop answered "was not started by this LiteTUI". The window is relaunched as a new process for the new
agent (cli.py), and the handle to the engine stays in the thin parent. NInfer already had a record for this
(T1311b, `ninfer_engine.stop_adopted`); the three engines that share `strata_engine.spawn_server` had none.

Real processes, as in test_engine_adopt_t1311b: the stub is a loopback server that answers /health.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from litetui import paths, strata_engine
from litetui.basalt_backend import BasaltBackend
from litetui.settings import Settings
from litetui.strata_backend import StrataBackend
from litetui.xolotl_backend import XolotlBackend

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="taskkill is Windows")

#: The base interpreter: a venv's python.exe is a launcher whose CHILD is the real process.
PYTHON = getattr(sys, "_base_executable", None) or sys.executable

STUB = r'''
import http.server, json, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "loaded": True, "loading": False, "service": sys.argv[2]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''
#: What a venv's python.exe does: the process LiteTUI spawned is not the one that listens.
LAUNCHER = "import subprocess, sys\nraise SystemExit(subprocess.call([sys.executable, *sys.argv[1:]]))\n"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _answers(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path / "llm"))
    monkeypatch.setattr(strata_engine, "port_holder", lambda host: strata_engine.Holder())
    (tmp_path / "stub.py").write_text(STUB, encoding="utf-8")
    (tmp_path / "launcher.py").write_text(LAUNCHER, encoding="utf-8")
    made = SimpleNamespace(tmp=tmp_path, procs=[])

    def engine(service: str, *, through_launcher: bool = False):
        port = _free_port()
        argv = [PYTHON, str(tmp_path / "stub.py"), str(port), service]
        if through_launcher:
            argv = [PYTHON, str(tmp_path / "launcher.py"), *argv[1:]]
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        made.procs.append(proc)
        deadline = time.time() + 20
        while not _answers(port):
            assert proc.poll() is None and time.time() < deadline, "the stub engine did not come up"
            time.sleep(0.1)
        return proc, f"http://127.0.0.1:{port}", port

    made.engine = engine
    yield made
    for proc in made.procs:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)


def _record(box, proc, host, label="Basalt", folder=".basalt"):
    log = box.tmp / folder / f"litetui-{folder[1:]}-server.log"
    log.parent.mkdir(exist_ok=True)
    strata_engine.write_owner_record(SimpleNamespace(proc=proc, host=host, log_path=log, model_id="m"), label)
    return log


def test_the_engine_an_earlier_session_started_is_stopped_from_its_record(box):
    proc, host, port = box.engine("basalt")
    log = _record(box, proc, host)
    rec = json.loads(strata_engine.owner_record_path(log).read_text(encoding="utf-8"))
    assert rec["pid"] == proc.pid and rec["host"] == host and rec["created"] and rec["exe"]
    out = strata_engine.stop_recorded(log, host, "Basalt", healthy=lambda h: _answers(port))
    assert out.freed and f"pid {proc.pid}" in out.message and "LiteTUI started" in out.message
    assert proc.wait(timeout=10) is not None and not _answers(port)
    assert not strata_engine.owner_record_path(log).exists()


def test_a_listener_that_is_the_spawned_process_child_is_still_ours(box):
    """A venv python.exe (Basalt's server, Strata's) is a launcher: the pid LiteTUI holds does not listen."""
    proc, host, port = box.engine("basalt", through_launcher=True)
    log = _record(box, proc, host)
    out = strata_engine.stop_recorded(log, host, "Basalt", healthy=lambda h: _answers(port))
    assert out.freed, out.reason
    assert proc.wait(timeout=10) is not None and not _answers(port)


def test_a_reused_pid_is_never_killed(box):
    proc, host, port = box.engine("basalt")
    log = _record(box, proc, host)
    path = strata_engine.owner_record_path(log)
    rec = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**rec, "created": rec["created"] - 60}), encoding="utf-8")
    out = strata_engine.stop_recorded(log, host, "Basalt", healthy=lambda h: _answers(port))
    assert not out.freed and "the pid was reused" in out.reason
    assert proc.poll() is None and _answers(port) and path.exists()


def test_a_record_for_another_address_or_none_at_all_is_not_an_answer(box):
    proc, host, port = box.engine("basalt")
    log = _record(box, proc, host)
    assert strata_engine.stop_recorded(log, "http://127.0.0.1:1", "Basalt", healthy=lambda h: True) is None
    strata_engine.clear_owner_record(log, proc.pid + 1)                 # another engine's pid: kept
    assert strata_engine.owner_record_path(log).exists()
    strata_engine.clear_owner_record(log, proc.pid)
    assert strata_engine.stop_recorded(log, host, "Basalt", healthy=lambda h: True) is None
    assert proc.poll() is None


def test_a_recorded_engine_that_already_exited_has_its_record_cleared(box):
    proc, host, port = box.engine("basalt")
    log = _record(box, proc, host)
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    proc.wait(timeout=10)
    out = strata_engine.stop_recorded(log, host, "Basalt", healthy=lambda h: _answers(port))
    assert out.freed and "already exited" in out.message and not strata_engine.owner_record_path(log).exists()


@pytest.mark.parametrize("backend_cls,service,field,folder", [
    (BasaltBackend, "basalt", "basalt_host", ".basalt"),
    (StrataBackend, "strata", "strata_host", ".strata"),
    (XolotlBackend, "strata", "xolotl_host", ".xolotl"),
])
def test_engine_stop_in_the_new_agents_seat_stops_the_engine_the_window_started(box, backend_cls, service, field, folder):
    """The seat after /agent-new: a fresh backend object with no handle, the same data folder."""
    proc, host, port = box.engine(service)
    backend = backend_cls(Settings(**{field: host}))
    assert "was not started by this LiteTUI" in backend.stop_engine()          # no record: somebody else's
    assert proc.poll() is None
    _record(box, proc, host, backend.label, folder)
    assert "started by LiteTUI" in backend.engine_status()
    said = backend.stop_engine()
    assert said.startswith(f"stopped the {backend.label} server LiteTUI started at {host}"), said
    assert proc.wait(timeout=10) is not None and not _answers(port)
