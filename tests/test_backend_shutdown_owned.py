"""shutdown_owned(): prove terminal MAIN exit on the RETAINED handle, retain on doubt.

Fake process handles only — no real engine, no taskkill, no SQLite. wait_for_exit is
stubbed in the shutdown_owned tests (deterministic/fast); its own timeout behaviour is
pinned separately. tree is ALWAYS 'unknown' here: a kill request is not descendant proof.
"""
from types import SimpleNamespace

import pytest

from litetui import llm_backend, ninfer_backend
from litetui.llm_backend import LlamaCppBackend, TerminalShutdown, wait_for_exit


# ── wait_for_exit: real function, tiny timeouts ─────────────────────────────

def test_wait_for_exit_true_when_process_exits():
    polls = iter([None, None, 0])
    proc = SimpleNamespace(poll=lambda: next(polls))
    assert wait_for_exit(proc, timeout=1.0, interval=0.0) is True


def test_wait_for_exit_false_when_still_alive():
    proc = SimpleNamespace(poll=lambda: None)
    assert wait_for_exit(proc, timeout=0.02, interval=0.01) is False


# ── a controllable fake child process ───────────────────────────────────────

class _Proc:
    def __init__(self, exits_on):        # 'terminate' | 'kill' | 'never' | 'already'
        self.pid = 4321
        self._exits_on = exits_on
        self._dead = exits_on == "already"
        self.terminated = self.killed = False

    def poll(self):
        return 0 if self._dead else None

    def terminate(self):
        self.terminated = True
        if self._exits_on == "terminate":
            self._dead = True

    def kill_now(self):                  # driven by the stubbed tree-kill
        self.killed = True
        if self._exits_on == "kill":
            self._dead = True


class _RaisingProc:
    pid = 1

    def poll(self):
        raise RuntimeError("poll boom")

    def terminate(self):
        pass


@pytest.fixture(autouse=True)
def _fast_wait(monkeypatch):
    # shutdown_owned polls the retained handle; reflect the fake's state immediately.
    monkeypatch.setattr(llm_backend, "wait_for_exit", lambda proc, **kw: proc.poll() is not None)


def _llama(proc):
    b = object.__new__(LlamaCppBackend)
    b._owned = SimpleNamespace(proc=proc, log_file=SimpleNamespace(close=lambda: None))
    return b


# ── LlamaCppBackend.shutdown_owned ──────────────────────────────────────────

def test_llama_no_owned_is_not_owned():
    b = object.__new__(LlamaCppBackend)
    b._owned = None
    assert b.shutdown_owned() == TerminalShutdown(owned=False)


def test_llama_terminate_exit_is_main_only_tree_unknown(monkeypatch):
    monkeypatch.setattr(llm_backend.router_record, "remove_if_mine", lambda pid: None)
    proc = _Proc("terminate")
    b = _llama(proc)
    r = b.shutdown_owned()
    assert proc.terminated and not proc.killed
    assert r.owned and r.attempted and r.main_exited and r.tree == "unknown" and not r.retained
    assert b._owned is None                     # cleared only after confirmed exit


def test_llama_tree_kill_does_not_confirm_tree(monkeypatch):
    # taskkill /T is issued and the main exits, but a kill REQUEST is not descendant
    # evidence — tree stays 'unknown'.
    monkeypatch.setattr(llm_backend.router_record, "remove_if_mine", lambda pid: None)
    monkeypatch.setattr(LlamaCppBackend, "_kill_tree", staticmethod(lambda proc: proc.kill_now()))
    proc = _Proc("kill")
    r = _llama(proc).shutdown_owned()
    assert proc.terminated and proc.killed and r.main_exited
    assert r.tree == "unknown"                  # NOT 'confirmed'


def test_llama_never_exits_retains_handle(monkeypatch):
    monkeypatch.setattr(LlamaCppBackend, "_kill_tree", staticmethod(lambda proc: None))
    proc = _Proc("never")
    b = _llama(proc)
    r = b.shutdown_owned()
    assert not r.main_exited and r.retained and r.tree == "unknown" and r.attempted
    assert b._owned is not None                 # RETAINED for retry


def test_llama_already_dead_is_main_only_no_attempt():
    proc = _Proc("already")
    r = _llama(proc).shutdown_owned()
    assert not proc.terminated                  # never signalled a dead process
    assert r.main_exited and not r.attempted and r.tree == "unknown"


def test_llama_exception_preserves_type_and_retains():
    proc = _RaisingProc()
    b = _llama(proc)
    r = b.shutdown_owned()
    assert r.error == "RuntimeError" and not r.main_exited and r.retained
    assert b._owned is not None                 # RETAINED on error


# ── LlamaCppBackend.shutdown() delegates ────────────────────────────────────

def test_llama_production_shutdown_delegates(monkeypatch):
    monkeypatch.setattr(llm_backend.router_record, "remove_if_mine", lambda pid: None)
    proc = _Proc("terminate")
    b = _llama(proc)
    r = b.shutdown()                            # production path
    assert isinstance(r, TerminalShutdown) and r.main_exited and proc.terminated
    assert b._owned is None


def test_llama_production_shutdown_attached_is_not_owned():
    b = object.__new__(LlamaCppBackend)
    b._owned = None
    assert b.shutdown() == TerminalShutdown(owned=False)   # attached/none: nothing killed


# ── NInferBackend.shutdown_owned ────────────────────────────────────────────

class _Owned:
    def __init__(self, proc, job=None):
        self.proc = proc
        self.job = job
        self.host = "http://127.0.0.1:9000/v1"
        self.log_file = SimpleNamespace(close=lambda: None)

    @property
    def alive(self):
        return self.proc.poll() is None


@pytest.fixture
def _ninfer_engine_stubs(monkeypatch):
    from litetui import ninfer_engine
    monkeypatch.setattr(ninfer_engine, "unregister_host", lambda host: None)
    monkeypatch.setattr(ninfer_engine, "_kill", lambda proc: proc.kill_now())
    monkeypatch.setattr(ninfer_engine.jobkill, "close", lambda job: None)
    return ninfer_engine


def _ninfer(proc, job=None):
    b = object.__new__(ninfer_backend.NInferBackend)
    b._owned = _Owned(proc, job=job)
    b._host = "http://127.0.0.1:9000/v1"
    return b


def test_ninfer_no_owned_is_not_owned():
    b = object.__new__(ninfer_backend.NInferBackend)
    b._owned = None
    assert b.shutdown_owned() == TerminalShutdown(owned=False)


def test_ninfer_job_close_plus_poll_is_main_only_tree_unknown(_ninfer_engine_stubs):
    proc = _Proc("terminate")
    closed = []
    _ninfer_engine_stubs.jobkill.close = lambda job: (closed.append(job), setattr(proc, "_dead", True))
    b = _ninfer(proc, job=99)
    r = b.shutdown_owned()
    assert closed == [99] and r.attempted and r.main_exited
    assert r.tree == "unknown" and not r.retained    # job close is not tree proof
    assert b._owned is None


def test_ninfer_already_dead_with_job_still_closes_job(_ninfer_engine_stubs):
    # Regression: owned.alive False + a job present must STILL close the job — the job
    # may hold descendants. The old code skipped the close and leaked them.
    proc = _Proc("already")
    closed = []
    _ninfer_engine_stubs.jobkill.close = lambda job: closed.append(job)
    r = _ninfer(proc, job=77).shutdown_owned()
    assert closed == [77]                             # job closed despite the main already dead
    assert r.attempted and r.main_exited and r.tree == "unknown"


def test_ninfer_no_job_fallback_kill_is_main_only(_ninfer_engine_stubs):
    proc = _Proc("kill")
    r = _ninfer(proc, job=None).shutdown_owned()
    assert proc.killed and r.main_exited and r.tree == "unknown"


def test_ninfer_job_close_but_no_exit_retains(_ninfer_engine_stubs):
    proc = _Proc("never")
    b = _ninfer(proc, job=99)
    r = b.shutdown_owned()
    assert not r.main_exited and r.retained and r.tree == "unknown"
    assert b._owned is not None                       # RETAINED for retry


def test_ninfer_exception_preserves_type_and_retains(_ninfer_engine_stubs):
    proc = _RaisingProc()                             # wait_for_exit poll raises
    b = _ninfer(proc, job=99)
    r = b.shutdown_owned()
    assert r.error == "RuntimeError" and not r.main_exited and r.retained
    assert b._owned is not None


# ── NInferBackend.shutdown() delegates + clears host ────────────────────────

def test_ninfer_production_shutdown_delegates_and_clears_host(_ninfer_engine_stubs):
    proc = _Proc("terminate")
    _ninfer_engine_stubs.jobkill.close = lambda job: setattr(proc, "_dead", True)
    b = _ninfer(proc, job=5)
    r = b.shutdown()
    assert isinstance(r, TerminalShutdown) and r.main_exited
    assert b._owned is None and b._host is None


def test_ninfer_production_shutdown_attached_is_not_owned():
    b = object.__new__(ninfer_backend.NInferBackend)
    b._owned = None
    b._host = "http://somewhere/v1"
    r = b.shutdown()
    assert r == TerminalShutdown(owned=False) and b._host is None   # attached: forget host, no kill
