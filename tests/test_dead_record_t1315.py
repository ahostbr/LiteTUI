"""T1315: LiteTUI clears its NInfer registry record when the engine is gone.

Found by T1314: ~/.litesuite/llm/config.json still held a `kind: ninfer, owner: litetui` record
(pid 28084, port 56682) for a server that had died on its own. LiteSuite's Model Hub and every
later LiteTUI session read that record.

Two cases, both only ever REMOVING a record of an engine that is already gone (nothing is killed):
  1. this session's own engine process exits without /engine stop;
  2. an earlier session's record: owner litetui, kind ninfer, pid gone, nothing answering.

🔴 NO ENGINE, NO GPU, NO MODEL. Processes are fakes (a `poll` and a `pid`) or a python child that
has already exited. The config lives under tmp_path; the real ~/.litesuite is never touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import ninfer_engine as eng
from litetui.llm_backend import BackendError
from litetui.ninfer_backend import NInferBackend

HOST = "http://127.0.0.1:56682"


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
    llm = tmp_path / "llm"
    llm.mkdir()
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(llm))
    # Another ninfer-serve is running (LiteSuite's own): the case T1311's "no process at all" clear
    # never reached, and the one the dead record in the incident sat in.
    monkeypatch.setattr(eng, "engine_process_alive", lambda: True)
    monkeypatch.setattr(NInferBackend, "_health", staticmethod(lambda host, timeout=2.0: False))
    return SimpleNamespace(config=llm / "config.json", settings=_Settings())


@pytest.fixture
def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _entries(box) -> list[dict]:
    if not box.config.is_file():
        return []
    return [e for e in json.loads(box.config.read_text(encoding="utf-8")).get("extraEndpoints", [])
            if e.get("kind") == "ninfer"]


def _write(box, **entry):
    box.config.write_text(json.dumps({"version": 1, "extraEndpoints": [
        {"baseUrl": HOST, "kind": "ninfer", **entry}]}), encoding="utf-8")


def _owned(pid: int, poll) -> eng.OwnedEngine:
    return eng.OwnedEngine(proc=SimpleNamespace(pid=pid, poll=poll), host=HOST,
                           log_path=Path("x.log"), log_file=None, model_id="m")


# ── case 1: this session's own engine exits on its own ──────────────────────

def test_an_owned_engine_that_exits_on_its_own_has_its_record_cleared_by_status(box):
    assert eng.register_host(HOST, pid=4242)
    b = NInferBackend(box.settings)
    b._owned = _owned(4242, poll=lambda: 1)            # exited, nobody ran /engine stop
    assert "NOT answering" in b.engine_status()
    assert _entries(box) == []


def test_an_owned_engine_that_exits_on_its_own_has_its_record_cleared_by_ensure_running(box):
    assert eng.register_host(HOST, pid=4242)
    b = NInferBackend(box.settings)
    b._owned = _owned(4242, poll=lambda: 1)
    with pytest.raises(BackendError):
        b._ensure_running_sync()
    assert _entries(box) == []


def test_an_owned_engine_still_running_keeps_its_record(box):
    assert eng.register_host(HOST, pid=4242)
    b = NInferBackend(box.settings)
    b._owned = _owned(4242, poll=lambda: None)
    b.engine_status()
    assert [e["pid"] for e in _entries(box)] == [4242]


def test_an_exited_owned_engine_never_removes_a_replacements_record(box):
    assert eng.register_host(HOST, pid=9999)           # same port, another process
    assert eng.clear_exited_owned(_owned(4242, poll=lambda: 1)) is False
    assert [e["pid"] for e in _entries(box)] == [9999]


def test_an_exited_owned_engine_that_was_never_registered_touches_nothing(box):
    assert eng.register_host(HOST, pid=4242)
    owned = _owned(4242, poll=lambda: 1)
    owned.registered = False
    assert eng.clear_exited_owned(owned) is False
    assert [e["pid"] for e in _entries(box)] == [4242]


# ── case 2: an earlier session's record ─────────────────────────────────────

def test_a_dead_pid_with_nothing_answering_is_cleared_on_start(box, dead_pid):
    assert eng.register_host(HOST, pid=dead_pid)
    b = NInferBackend(box.settings)                    # a fresh session: only the record
    with pytest.raises(BackendError, match="no NInfer engine is registered"):
        b._ensure_running_sync()                       # not "still loading", though another ninfer-serve runs
    assert _entries(box) == []


def test_registration_state_reports_a_dead_pid_as_cleared(box, dead_pid):
    assert eng.register_host(HOST, pid=dead_pid)
    assert eng.registration_state(HOST, healthy=lambda h: False) == "cleared"
    assert _entries(box) == []


def test_status_clears_a_dead_record_and_says_nothing_is_registered(box, dead_pid):
    assert eng.register_host(HOST, pid=dead_pid)
    assert "no engine registered" in NInferBackend(box.settings).engine_status()
    assert _entries(box) == []


def test_a_dead_pid_is_kept_while_something_answers_at_its_address(box, dead_pid):
    assert eng.register_host(HOST, pid=dead_pid)
    entry = eng.registered_entry()
    assert eng.clear_dead_record(entry, healthy=lambda h: True) is False
    assert eng.registration_state(HOST, healthy=lambda h: True) == "answering"
    assert [e["pid"] for e in _entries(box)] == [dead_pid]


def test_a_live_pid_is_kept_even_when_nothing_answers(box):
    assert eng.register_host(HOST, pid=os.getpid())    # a pid that exists, not an engine of ours
    assert eng.clear_dead_record(eng.registered_entry(), healthy=lambda h: False) is False
    assert eng.registration_state(HOST, healthy=lambda h: False) == "loading"
    assert [e["pid"] for e in _entries(box)] == [os.getpid()]


@pytest.mark.parametrize("owner", [None, "litesuite", "someone-else"])
def test_a_record_not_written_by_litetui_is_never_cleared(box, dead_pid, owner):
    _write(box, **({"owner": owner} if owner else {}), pid=dead_pid)
    assert eng.clear_dead_record(eng.registered_entry(), healthy=lambda h: False) is False
    assert eng.registration_state(HOST, healthy=lambda h: False) == "loading"
    assert [e["pid"] for e in _entries(box)] == [dead_pid]


def test_a_record_with_no_pid_is_never_cleared(box):
    _write(box, owner="litetui")
    assert eng.clear_dead_record(eng.registered_entry(), healthy=lambda h: False) is False
    assert len(_entries(box)) == 1


def test_stop_adopted_still_clears_an_exited_engine_and_still_refuses_an_answering_one(box, dead_pid):
    assert eng.register_host(HOST, pid=dead_pid, identity={"created": 1.0, "exe": "x.exe"})
    entry = eng.registered_entry()
    refused = eng.stop_adopted(entry, healthy=lambda h: True)
    assert not refused.freed and "something answers" in refused.reason
    assert _entries(box)
    cleared = eng.stop_adopted(entry, healthy=lambda h: False)
    assert cleared.freed and "already exited" in cleared.message
    assert _entries(box) == []
