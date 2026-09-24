"""jobkill read-only query + terminate primitives — FAKE win32 only, no real calls.

active_process_count is the ONLY honest tree-drain evidence; terminate kills the job
WITHOUT closing the handle so the count can then be read. No real process/job ops.
"""
import ctypes

import pytest

from litetui import jobkill


class _FakeK32:
    """A stand-in kernel32 that fills the accounting struct via the void* it is given."""
    def __init__(self, *, active=0, query_ok=True, terminate_ok=True):
        self.active = active
        self.query_ok = query_ok
        self.terminate_ok = terminate_ok
        self.terminated = []

    def QueryInformationJobObject(self, job, cls, void_ptr, size, ret_len):
        if not self.query_ok:
            return 0
        acc = ctypes.cast(void_ptr, ctypes.POINTER(jobkill._BASIC_ACCOUNTING))
        acc.contents.ActiveProcesses = self.active
        return 1

    def TerminateJobObject(self, job, code):
        self.terminated.append((job, code))
        return 1 if self.terminate_ok else 0


@pytest.fixture
def fake(monkeypatch):
    def _install(**kw):
        fk = _FakeK32(**kw)
        monkeypatch.setattr(jobkill, "WINDOWS", True)
        monkeypatch.setattr(jobkill, "_k32", fk)
        return fk
    return _install


# ── active_process_count ────────────────────────────────────────────────────

def test_active_count_reads_zero(fake):
    fake(active=0)
    assert jobkill.active_process_count(1234) == 0


def test_active_count_reads_positive(fake):
    fake(active=7)
    assert jobkill.active_process_count(1234) == 7


def test_active_count_api_failure_is_none(fake):
    fake(query_ok=False)
    assert jobkill.active_process_count(1234) is None


def test_active_count_none_handle_is_none(fake):
    fake(active=0)
    assert jobkill.active_process_count(None) is None


def test_active_count_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(jobkill, "_k32", None)      # platform cannot do it
    assert jobkill.active_process_count(1234) is None


# ── terminate (keeps the handle open; owns-handle-only) ─────────────────────

def test_terminate_returns_typed_bool_and_calls_api(fake):
    fk = fake(terminate_ok=True)
    result = jobkill.terminate(4321, exit_code=1)
    assert result is True                            # typed bool, not a truthy int
    assert fk.terminated == [(4321, 1)]


def test_terminate_failure_is_false(fake):
    fake(terminate_ok=False)
    assert jobkill.terminate(4321) is False


def test_terminate_none_handle_is_false(fake):
    assert jobkill.terminate(None) is False          # never opens anything by pid/name


def test_terminate_false_when_unavailable(monkeypatch):
    monkeypatch.setattr(jobkill, "_k32", None)
    assert jobkill.terminate(4321) is False
