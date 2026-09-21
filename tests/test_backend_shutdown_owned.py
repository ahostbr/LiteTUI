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


# ── NInferBackend.shutdown_owned: job-proof (tree='confirmed') path ──────────

class _Owned:
    def __init__(self, proc, job=None):
        self.proc = proc
        self.job = job
        self.host = "http://127.0.0.1:9000/v1"
        self.log_file = SimpleNamespace(close=lambda: None)

    @property
    def alive(self):
        return self.proc.poll() is None


def _dead():
    return SimpleNamespace(pid=1, poll=lambda: 0)


def _live():
    return SimpleNamespace(pid=1, poll=lambda: None)


@pytest.fixture
def _proof(monkeypatch):
    """Stub the jobkill proof primitives + drain timing; drive counts per test."""
    from litetui import ninfer_engine, ninfer_backend as nb
    state = SimpleNamespace(terminated=[], closed=[], unregistered=[], logs_closed=[],
                            terminate_ok=True, close_ok=True, counts=[0])
    monkeypatch.setattr(ninfer_engine.jobkill, "terminate",
                        lambda job, exit_code=1: (state.terminated.append(job), state.terminate_ok)[1])
    monkeypatch.setattr(ninfer_engine.jobkill, "close",
                        lambda job: (state.closed.append(job), state.close_ok)[1])
    monkeypatch.setattr(ninfer_engine, "unregister_host",
                        lambda host: state.unregistered.append(host))

    def _count(job):
        return state.counts[0] if len(state.counts) == 1 else state.counts.pop(0)
    monkeypatch.setattr(ninfer_engine.jobkill, "active_process_count", _count)
    monkeypatch.setattr(nb, "_JOB_DRAIN_TIMEOUT", 0.05)
    monkeypatch.setattr(nb, "_JOB_DRAIN_INTERVAL", 0.005)
    return state


def _ninfer(proc, job=None):
    b = object.__new__(ninfer_backend.NInferBackend)
    b._owned = _Owned(proc, job=job)
    b._host = "http://127.0.0.1:9000/v1"
    return b


def test_ninfer_no_owned_is_not_owned():
    b = object.__new__(ninfer_backend.NInferBackend)
    b._owned = None
    assert b.shutdown_owned() == TerminalShutdown(owned=False)


def test_ninfer_job_drain_zero_plus_main_exit_confirms_tree(_proof):
    _proof.counts = [0]
    b = _ninfer(_dead(), job=42)
    r = b.shutdown_owned()
    assert _proof.terminated == [42]                 # terminated, handle NOT closed first
    assert r.main_exited and r.tree == "confirmed" and not r.retained
    assert _proof.closed == [42]                     # handle released only after proof
    assert _proof.unregistered == ["http://127.0.0.1:9000/v1"]
    assert b._owned is None


def test_ninfer_drain_never_zero_times_out_and_retains(_proof):
    _proof.counts = [5]                              # never drains
    b = _ninfer(_dead(), job=42)
    r = b.shutdown_owned()
    assert r.tree == "unknown" and r.retained and r.main_exited
    assert _proof.closed == []                       # never closed the handle
    assert b._owned is not None                      # entire ownership retained


def test_ninfer_query_failure_retains(_proof):
    _proof.counts = [None]                           # query failed -> cannot prove
    b = _ninfer(_dead(), job=42)
    r = b.shutdown_owned()
    assert r.tree == "unknown" and r.retained and _proof.closed == []
    assert b._owned is not None


def test_ninfer_main_not_exited_retains_even_if_drained(_proof):
    _proof.counts = [0]
    b = _ninfer(_live(), job=42)                     # proc still alive
    r = b.shutdown_owned()
    assert not r.main_exited and r.tree == "unknown" and r.retained
    assert b._owned is not None


def test_ninfer_terminate_failure_still_proves_but_reports_error(_proof):
    _proof.terminate_ok = False                      # TerminateJobObject reported failure
    _proof.counts = [0]                              # ...but the tree still drained
    r = _ninfer(_dead(), job=42).shutdown_owned()
    assert r.tree == "confirmed" and r.main_exited and r.error == "terminate_failed"


def test_ninfer_close_failure_keeps_proof_but_retains_handle(_proof):
    _proof.close_ok = False
    _proof.counts = [0]
    b = _ninfer(_dead(), job=42)
    r = b.shutdown_owned()
    assert r.tree == "confirmed" and r.retained and r.error == "close_failed"
    assert b._owned is not None                      # handle NOT discarded on close failure


def test_ninfer_concurrent_replacement_is_not_torn_down(_proof, monkeypatch):
    from litetui import ninfer_engine
    b = _ninfer(_dead(), job=42)
    other = _Owned(_dead(), job=99)
    # A replacement swaps self._owned during the drain poll.
    def _count_then_replace(job):
        b._owned = other
        return 0
    monkeypatch.setattr(ninfer_engine.jobkill, "active_process_count", _count_then_replace)
    r = b.shutdown_owned()
    assert r.tree == "confirmed" and r.retained      # proof about a job we no longer own
    assert _proof.closed == []                       # the replacement is NOT closed
    assert b._owned is other


def test_ninfer_retry_after_retain_confirms(_proof):
    b = _ninfer(_dead(), job=42)
    _proof.counts = [5]                              # first: not drained
    assert b.shutdown_owned().retained and b._owned is not None
    _proof.counts = [0]                              # second: drained -> confirmed
    r = b.shutdown_owned()
    assert r.tree == "confirmed" and not r.retained and b._owned is None


def test_ninfer_no_job_is_main_only_unknown(monkeypatch):
    from litetui import ninfer_engine
    monkeypatch.setattr(ninfer_engine, "stop", lambda o: None)
    r = _ninfer(_dead(), job=None).shutdown_owned()
    assert r.main_exited and r.tree == "unknown" and not r.retained


# ── NInferBackend.shutdown() delegates; clears host only when not retained ──

def test_ninfer_production_shutdown_confirmed_clears_host(_proof):
    _proof.counts = [0]
    b = _ninfer(_dead(), job=42)
    r = b.shutdown()
    assert r.tree == "confirmed" and b._owned is None and b._host is None


def test_ninfer_production_shutdown_retained_keeps_host(_proof):
    _proof.counts = [5]                              # not proven -> retain
    b = _ninfer(_dead(), job=42)
    r = b.shutdown()
    assert r.retained and b._host == "http://127.0.0.1:9000/v1"   # host kept for retry
