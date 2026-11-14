"""T1311 — the engine flow rework: /engine restart, a send that auto-starts, dead
registrations cleared, and an /engine stop that ends a stalled or starting engine.

Ryan, 2026-11-14, after Edge0 + "hello" got "Nothing is serving on ninfer yet" and a line
about an engine at :54149 that "may still be loading weights" when nothing existed there:

    "ya that whole engine start thing needs to be reworked , and theres no slash engine restart"
    "slash engine stop wouldnt even kill it had to close litetui completely to me it was busy"

His picks: "/engine restart", "Send auto-starts", "Clear dead engines" (NOT "Pick = serve").

🔴 NO MODEL IS EVER LOADED HERE. The engine is a stub: either a fake process object, or a
real `python` child that prints the engine's progress lines, ignores Ctrl+C/Ctrl+Break and
never exits by itself. Config and data roots live under tmp_path.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import ninfer_engine as eng

try:
    from litetui import engine_autostart
except ImportError:   # the develop control: its arms must FAIL there, not vanish at collection
    engine_autostart = None
from litetui.llm_backend import BackendError, TerminalShutdown
from litetui.ninfer_backend import NInferBackend
from litetui.plugins import model_switch


# ── shared fixtures ──────────────────────────────────────────────────────────

class _Settings:
    ninfer_host = ""
    ninfer_executable = ""
    ninfer_artifact = ""
    ninfer_max_context = 32768
    ninfer_max_concurrency = 1
    ninfer_kv_dtype = "fp8"
    ninfer_kv_capacity = "auto"
    ninfer_host_kv_mib = 0


@pytest.fixture
def box(tmp_path, monkeypatch):
    """An isolated machine (conftest already isolates the data root): LiteSuite config, an exe
    and an artifact that exist."""
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path / "llm"))
    (tmp_path / "llm").mkdir()
    monkeypatch.setattr(eng.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: 30000)
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    exe = tmp_path / "bin" / "ninfer-serve.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    art = tmp_path / "models" / "Edge0.ninfer"
    art.parent.mkdir()
    art.write_bytes(b"not a real artifact")
    s = _Settings()
    s.ninfer_executable, s.ninfer_artifact = str(exe), str(art)
    return SimpleNamespace(tmp=tmp_path, settings=s, config=tmp_path / "llm" / "config.json")


def _register(box, url, **extra):
    box.config.write_text(json.dumps({"version": 1, "extraEndpoints": [
        {"baseUrl": url, "kind": "ninfer", "owner": "litetui", "pid": 54149, **extra}]}), encoding="utf-8")


class _FakeProc:
    pid = 777

    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


def _writer_spawn(lines, *, gap=0.05, ready=True, procs=None):
    """A spawn whose 'engine' prints `lines` one by one, then (optionally) the ready line."""
    def spawn(cmd, **kw):
        out = kw["stdout"]
        proc = _FakeProc()
        if procs is not None:
            procs.append(proc)

        def run():
            for line in lines:
                time.sleep(gap)
                out.write(line + "\n")
                out.flush()
            if ready:
                time.sleep(gap)
                out.write("listening on http://127.0.0.1:1\n")
                out.flush()
        threading.Thread(target=run, daemon=True).start()
        return proc
    return spawn


# ── a real stub engine: ignores the shutdown keys, never exits, has a child ──

STUB = r'''
import signal, subprocess, sys, time
for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"])
open(sys.argv[2], "w").write(str(child.pid))
if sys.argv[1] == "ready":
    print("listening on http://127.0.0.1:1", flush=True)
pct = 0.0
while True:
    if sys.argv[1] == "load":
        pct = min(pct + 3.0, 99.0)
        print(f"loading weights {pct:.1f}% | {pct * 0.544:.1f} GiB/54.4 GiB | 1.2 GiB/s | ETA 9.0s", flush=True)
    time.sleep(0.2)
'''

_K32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None


def _pin(pid: int):
    """An OpenProcess handle: the pid cannot be recycled while it is open."""
    h = _K32.OpenProcess(0x00100000 | 0x1000, False, pid)   # SYNCHRONIZE | QUERY_LIMITED
    assert h, f"could not open pid {pid}"
    return h


def _gone(h, timeout_s: float) -> bool:
    return _K32.WaitForSingleObject(h, int(timeout_s * 1000)) == 0


@pytest.fixture
def real_stub(box, monkeypatch):
    """A spawn that starts the stub for real (mode "load" or "ready"); yields what it made.
    `ninfer_engine.start` is bound to it, so the backend's own start reaches the stub."""
    made = SimpleNamespace(procs=[], pidfile=box.tmp / "child.pid", mode="load")
    script = box.tmp / "stub_engine.py"
    script.write_text(STUB, encoding="utf-8")

    def spawn(cmd, **kw):
        proc = subprocess.Popen([sys.executable, str(script), made.mode, str(made.pidfile)],
                                stdin=subprocess.DEVNULL, stdout=kw["stdout"], stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        made.procs.append(proc)
        return proc

    made.spawn = spawn
    real_start = eng.start
    monkeypatch.setattr(eng, "start", lambda settings, **kw: real_start(settings, spawn=spawn, **kw))
    yield made
    for proc in made.procs:                  # a failed arm must not leave a stub behind
        if proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)


def _child_handle(made, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return _pin(int(made.pidfile.read_text()))
        except (OSError, ValueError):
            time.sleep(0.05)
    raise AssertionError("the stub never wrote its child's pid")


# ── item 3: "Clear dead engines" ─────────────────────────────────────────────

def test_a_dead_registration_is_cleared_with_no_may_still_be_loading_line(box, monkeypatch):
    """Ryan's :54149 — registered, silent on /health, no ninfer process: cleared silently."""
    _register(box, "http://127.0.0.1:54149")
    monkeypatch.setattr(NInferBackend, "_health", staticmethod(lambda host, timeout=2.0: False))
    b = NInferBackend(box.settings)
    with pytest.raises(BackendError) as excinfo:
        asyncio.run(b.ensure_running())
    said = str(excinfo.value)
    assert "may still be loading" not in said and "54149" not in said, said
    assert "no NInfer engine is registered" in said and "/engine start" in said
    assert eng.registered_entry() is None, "the dead entry is still in LiteSuite's config"


def test_a_silent_registration_with_a_live_process_is_loading_and_kept(box, monkeypatch):
    """The other half: a process exists, so it IS loading — said so, and the entry stays."""
    _register(box, "http://127.0.0.1:54149")
    monkeypatch.setattr(NInferBackend, "_health", staticmethod(lambda host, timeout=2.0: False))
    monkeypatch.setattr(eng, "engine_process_alive", lambda: True)
    with pytest.raises(BackendError, match="still loading"):
        asyncio.run(NInferBackend(box.settings).ensure_running())
    assert eng.registered_host() == "http://127.0.0.1:54149"


def test_one_helper_clears_for_both_the_backend_and_the_launcher(box, monkeypatch):
    """refuse_reason and ensure_running reach the SAME helper (the brief: no second copy)."""
    seen = []
    real = eng.registration_state
    monkeypatch.setattr(eng, "registration_state", lambda reg, **kw: seen.append(reg) or real(reg, **kw))
    _register(box, "http://127.0.0.1:54149")
    healthy = lambda host, timeout=2.0: False  # noqa: E731
    assert eng.refuse_reason(box.settings, healthy=healthy) is None
    _register(box, "http://127.0.0.1:54150")
    monkeypatch.setattr(NInferBackend, "_health", staticmethod(healthy))
    with pytest.raises(BackendError):
        asyncio.run(NInferBackend(box.settings).ensure_running())
    assert seen == ["http://127.0.0.1:54149", "http://127.0.0.1:54150"]


# ── the start wait: progress, and a stall window instead of a wall ───────────

def test_progress_lines_are_reported_per_step_and_phase(box):
    lines = [f"loading weights {p:.1f}% | {p * 0.544:.1f} GiB/54.4 GiB | 1.0 GiB/s | ETA 9.0s"
             for p in (1.0, 4.0, 12.0, 15.0, 33.0, 82.7, 99.9)]
    lines += ["weights ready", "CUDA graphs ready", "engine ready"]
    got = []
    eng.start(box.settings, healthy=lambda h: False, spawn=_writer_spawn(lines, gap=0.6),
              progress=got.append)
    assert [g.split("%")[0] for g in got[:5]] == ["loading weights 1.0", "loading weights 12.0",
                                                   "loading weights 33.0", "loading weights 82.7",
                                                   "loading weights 99.9"], got
    assert got[5:] == ["weights ready", "CUDA graphs ready", "engine ready"], got


def test_a_start_that_keeps_printing_is_not_cut_off_by_the_window(box, monkeypatch):
    """The window restarts on every new byte: 1.6 s of progress outlives a 0.5 s window."""
    monkeypatch.setattr(eng, "NINFER_START_TIMEOUT_S", 0.5)
    lines = [f"loading weights {p}.0% | x" for p in range(0, 100, 12)]
    owned = eng.start(box.settings, healthy=lambda h: False, spawn=_writer_spawn(lines, gap=0.2))
    assert owned.host.startswith("http://127.0.0.1:")


def test_a_start_that_prints_nothing_new_is_called_stalled(box, monkeypatch):
    monkeypatch.setattr(eng, "NINFER_START_TIMEOUT_S", 0.5)
    monkeypatch.setattr(eng, "terminate_owned",
                        lambda owned: TerminalShutdown(owned=True, main_exited=True, retained=False))
    with pytest.raises(eng.EngineStartFailed, match="printed nothing new for 0.5s"):
        eng.start(box.settings, healthy=lambda h: False,
                  spawn=_writer_spawn(["loading weights 3.0% | x"], ready=False))


# ── item 4: /engine stop ends a stalled, busy or starting engine ─────────────

@pytest.mark.skipif(sys.platform != "win32", reason="job-object tree kill is Windows")
def test_stop_during_the_startup_wait_kills_a_stub_that_ignores_shutdown(box, real_stub):
    b = NInferBackend(box.settings)
    seen = []

    async def drive():
        task = asyncio.ensure_future(b.start_engine(progress=seen.append))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if seen:
                break
        assert seen and seen[0].startswith("loading weights"), seen
        main, child = _pin(real_stub.procs[0].pid), _child_handle(real_stub)
        assert b.begin_stop() is False            # a start holds the claim, as before...
        assert b.cancel_start() is True           # ...and stop now ends it instead of "busy"
        with pytest.raises(BackendError) as excinfo:
            await task
        return str(excinfo.value), main, child

    said, main, child = asyncio.run(drive())
    assert "/engine stop ended the NInfer start" in said and "is gone" in said, said
    assert _gone(main, 5) and _gone(child, 5), "the stub (or its child) survived the stop"
    assert b._owned is None and b._starting is False
    assert eng.registered_entry() is None


@pytest.mark.skipif(sys.platform != "win32", reason="job-object tree kill is Windows")
def test_stop_kills_a_running_stub_that_ignores_shutdown(box, real_stub):
    real_stub.mode = "ready"
    b = NInferBackend(box.settings)
    asyncio.run(b.start_engine())
    main, child = _pin(real_stub.procs[0].pid), _child_handle(real_stub)
    said = b.stop_engine()
    assert said.startswith("stopped the engine LiteTUI started"), said
    assert _gone(main, 5) and _gone(child, 5)
    assert b._owned is None


def test_engine_stop_command_during_a_start_ends_it_rather_than_saying_busy():
    class _B:
        name, label = "ninfer", "NInfer"
        cancelled = False

        def begin_stop(self):
            return False                          # a start is in flight

        def cancel_start(self):
            self.cancelled = True
            return True

    app = SimpleNamespace(backend=_B(), said=[])
    app.system_message = app.said.append
    model_switch._cmd_engine(app, "engine", "stop")
    assert app.backend.cancelled
    assert app.said and "busy" not in app.said[-1] and "still starting" in app.said[-1], app.said


def test_strata_stop_gives_an_unanswered_unload_a_short_grace(monkeypatch):
    """A server that never answers /v1/unload no longer holds the stop for 30 s."""
    from litetui import strata_engine

    asked = []
    monkeypatch.setattr(strata_engine, "request_unload", lambda host, timeout=30.0: asked.append(timeout))
    monkeypatch.setattr(strata_engine.ninfer_engine, "terminate_owned", lambda owned: "killed")
    owned = SimpleNamespace(alive=True, host="http://127.0.0.1:1")
    assert strata_engine.terminate_owned(owned) == "killed"
    assert asked == [strata_engine.STOP_UNLOAD_GRACE_S] and asked[0] <= 10


# ── item 1: /engine restart ──────────────────────────────────────────────────

class _App:
    def __init__(self, backend):
        self.backend = backend
        self.settings = backend._settings
        self.said: list[str] = []
        self.connected = 0
        self.tasks = []

    def system_message(self, msg, *a, **k):
        self.said.append(str(msg))

    def call_from_thread(self, fn, *a, **k):
        return fn(*a, **k)

    def _chat_running(self):
        return False

    def connect(self):
        self.connected += 1

    def run_worker(self, coro, **kw):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return SimpleNamespace(_task=task, cancel=task.cancel)


def _owned(pid):
    return eng.OwnedEngine(proc=SimpleNamespace(pid=pid, poll=lambda: None), host="http://127.0.0.1:9",
                           log_path=Path("x.log"), log_file=None, model_id="Edge0", registered=False)


async def _settle(app):
    while app.tasks:
        await app.tasks.pop(0)
        await asyncio.sleep(0)


def test_restart_stops_then_starts_on_the_current_settings(box, monkeypatch):
    events = []
    monkeypatch.setattr(eng, "terminate_owned", lambda owned: events.append(("stop", owned.proc.pid))
                        or TerminalShutdown(owned=True, main_exited=True, retained=False, tree="confirmed"))

    def start(settings, *, healthy, notice=None, progress=None, cancel=None):
        events.append(("start", settings.ninfer_max_context))
        return _owned(8)

    monkeypatch.setattr(eng, "start", start)
    b = NInferBackend(box.settings)
    b._owned = _owned(7)
    box.settings.ninfer_max_context = 131072          # a change the running engine does not have
    app = _App(b)

    async def drive():
        model_switch._cmd_engine(app, "engine", "restart")
        await _settle(app)
    asyncio.run(drive())
    assert events == [("stop", 7), ("start", 131072)], events
    assert b._owned.proc.pid == 8 and app.connected == 1
    assert not b._stopping and not b._starting


def test_restart_of_an_engine_litetui_did_not_start_refuses_in_words(box, monkeypatch):
    box.config.write_text(json.dumps({"extraEndpoints": [
        {"baseUrl": "http://127.0.0.1:7480", "kind": "ninfer"}]}), encoding="utf-8")   # LiteSuite's
    started = []
    monkeypatch.setattr(eng, "start", lambda *a, **k: started.append(1))
    b = NInferBackend(box.settings)
    app = _App(b)

    async def drive():
        model_switch._cmd_engine(app, "engine", "restart")
        await _settle(app)
    asyncio.run(drive())
    assert started == [] and app.connected == 0
    assert "restarts only an engine this LiteTUI started" in app.said[-1], app.said
    assert "was not started by LiteTUI" in app.said[-1]
    assert not b._stopping


def test_restart_with_nothing_running_starts(box, monkeypatch):
    monkeypatch.setattr(eng, "start", lambda settings, **kw: _owned(9))
    b = NInferBackend(box.settings)
    app = _App(b)

    async def drive():
        model_switch._cmd_engine(app, "engine", "restart")
        await _settle(app)
    asyncio.run(drive())
    assert b._owned.proc.pid == 9 and app.connected == 1
    assert any("No NInfer engine is running" in s for s in app.said), app.said


def test_restart_does_not_start_a_second_engine_when_the_stop_is_unconfirmed(box, monkeypatch):
    monkeypatch.setattr(eng, "terminate_owned",
                        lambda owned: TerminalShutdown(owned=True, main_exited=False, retained=True))
    started = []
    monkeypatch.setattr(eng, "start", lambda *a, **k: started.append(1))
    b = NInferBackend(box.settings)
    b._owned = _owned(7)
    app = _App(b)

    async def drive():
        model_switch._cmd_engine(app, "engine", "restart")
        await _settle(app)
    asyncio.run(drive())
    assert started == [] and "did not start a new engine" in app.said[-1]


# ── item 2: "Send auto-starts" ───────────────────────────────────────────────

def _send_app(box, monkeypatch, *, start, rpc=False, spawned=False):
    """A real owned LiteTUI on ninfer with nothing serving; only the engine start, the
    reconnect, the UI widgets and provider dispatch are stand-ins."""
    from _owned_seat import owned_app
    from litetui import app as app_mod

    monkeypatch.setattr(eng, "start", start)
    app = owned_app(initial_backend="ninfer", rpc=rpc)
    app.backend.set_settings(box.settings)
    app._spawned_marker = spawned
    app.said, app.sent, app.t_workers = [], [], []
    app._system = app.system_message = lambda m, *a, **k: app.said.append(str(m))
    app._user_bubble = lambda *a, **k: SimpleNamespace(remove=lambda: app.said.append("[bubble removed]"))
    app._scroll_down = lambda *a, **k: None
    app.notify = lambda *a, **k: None
    app.call_from_thread = lambda fn, *a, **k: fn(*a, **k)
    app.run_worker = lambda coro, **k: app.t_workers.append(coro)
    class _Widget:                                # the input box; any other widget call is inert
        value = ""

        def __getattr__(self, name):
            return lambda *a, **k: None
    box_input = _Widget()
    app.query_one = lambda *a, **k: box_input
    app.input_box = box_input

    def connect():                                # the reconnect finds what the engine serves
        app.model_id = "Edge0"
    app.connect = connect
    monkeypatch.setattr(app_mod.hook_host, "start_prompt", lambda _app, item: app.sent.append(item["content"]))
    return app


def _run_workers(app):
    async def drive():
        while app.t_workers:
            await app.t_workers.pop(0)
    asyncio.run(drive())


def test_a_send_with_nothing_serving_starts_the_engine_and_sends_once_ready(box, monkeypatch):
    starts = []

    def start(settings, *, healthy, notice=None, progress=None, cancel=None):
        starts.append(settings.ninfer_artifact)
        notice()
        progress("loading weights 50.0% | 27.2 GiB/54.4 GiB | ETA 2.0s")
        progress("engine ready")
        return _owned(5)

    app = _send_app(box, monkeypatch, start=start)
    assert engine_autostart.nothing_serving(app)
    app._submit_text("hello", alt_chord=False)
    assert app.sent == [] and len(app._pending_input) == 1          # held, not refused
    assert not any("Nothing is serving on ninfer yet" in s for s in app.said)
    assert any("starting Edge0" in s for s in app.said), app.said
    app._submit_text("and a second one", alt_chord=False)            # arrives during the start
    _run_workers(app)
    assert starts == [box.settings.ninfer_artifact]                   # ONE start for both
    assert app.sent == ["hello"], app.sent                            # first goes now; the rest on turn end
    assert [i["text"] for i in app._pending_input] == ["and a second one"]
    assert any("NInfer: loading weights 50.0%" in s for s in app.said), app.said
    assert any("NInfer: engine ready" in s for s in app.said)
    assert not app._engine_autostart


def test_a_failed_auto_start_keeps_the_message_and_says_why(box, monkeypatch):
    def start(settings, **kw):
        raise BackendError("only 1024 MiB free on the GPU; the engine needs about 26624 MiB")

    app = _send_app(box, monkeypatch, start=start)
    app._submit_text("hello", alt_chord=False)
    _run_workers(app)
    assert app.sent == [] and app._pending_input == []                # nothing sends to no engine
    assert any("only 1024 MiB free" in s for s in app.said), app.said  # why
    assert app.input_box.value == "hello"                             # kept
    assert any("not sent" in s and "back in the input box" in s for s in app.said)
    assert not app._engine_autostart


@pytest.mark.parametrize("how", ["rpc", "spawned", "source"])
def test_a_headless_or_spawned_seat_never_starts_an_engine(box, monkeypatch, how):
    starts = []
    app = _send_app(box, monkeypatch, start=lambda *a, **k: starts.append(1),
                    rpc=how == "rpc", spawned=how == "spawned")
    source = "rpc" if how == "source" else "typed"
    assert engine_autostart.nothing_serving(app)
    assert not engine_autostart.should_hold(app, source)
    app._submit_text("hello", alt_chord=False, source=source)
    _run_workers(app)
    assert starts == [] and app.t_workers == []
    assert not getattr(app, "_engine_autostart", False)
    assert app.sent == ["hello"]     # the ordinary path: the turn refuses in today's words

