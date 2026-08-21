"""The seat suspends for generation and ALWAYS comes back.

Every lms interaction here is faked. A test that touched the real LM Studio
would unload Ryan's live model — the exact class of live-state damage this
repo wrote its A-TEST-MUST-NEVER-WRITE-A-PATH-THE-APP-OWNS rule about, and
this time the path is VRAM.

The load-bearing assertions are about ORDER and about the finally: record
before suspend, suspend before generate, resume after — and resume runs
even when the generation raises, because a seat that stays down turns one
failed image into a dead agent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import seat_guard
import studio_tool

#: The REAL shape lms ps --json returned on this box (2026-08-21), verbatim
#: fields — the parser is tested against what the tool actually prints.
PS_ROW = {
    "identifier": "ektome-qwen3.8-27b-pristinelyuncensored",
    "contextLength": 120064,
    "parallel": 4,
    "status": "idle",
    "queued": 0,
    "ttlMs": 3600000,
}


@pytest.fixture(autouse=True)
def _never_touch_the_real_lms(monkeypatch, tmp_path):
    """No test may reach the real lms CLI or leave a breadcrumb in the repo."""
    monkeypatch.setattr(seat_guard, "BREADCRUMB", tmp_path / "suspended_seat.json")
    monkeypatch.setattr(seat_guard, "_lms", lambda: "lms")  # pretend installed
    monkeypatch.setattr(seat_guard.time, "sleep", lambda _s: None)


def _fake_ps(rows):
    return lambda: [dict(r) for r in rows]


# --------------------------------------------------------------------------
# record / safety
# --------------------------------------------------------------------------

def test_record_reads_the_live_config(monkeypatch):
    monkeypatch.setattr(seat_guard, "_ps", _fake_ps([PS_ROW]))
    rec = seat_guard.record(PS_ROW["identifier"])
    assert rec == {
        "identifier": PS_ROW["identifier"],
        "context": 120064,
        "parallel": 4,
        "status": "idle",
        "queued": 0,
    }


def test_record_of_an_unloaded_seat_is_none(monkeypatch):
    monkeypatch.setattr(seat_guard, "_ps", _fake_ps([]))
    assert seat_guard.record("anything") is None


def test_a_busy_seat_is_not_safe_to_suspend():
    ok, why = seat_guard.safe_to_suspend({**_rec(), "status": "generating"})
    assert not ok and "generating" in why


def test_a_queued_seat_is_not_safe_to_suspend():
    """parallel=4 means someone ELSE may be mid-stream on this model —
    unloading kills their turn, not ours."""
    ok, why = seat_guard.safe_to_suspend({**_rec(), "queued": 2})
    assert not ok and "2" in why


def _rec():
    return {"identifier": "seat-x", "context": 120064, "parallel": 4,
            "status": "idle", "queued": 0}


def test_the_reload_command_restores_the_exact_config():
    cmd = seat_guard.reload_command(_rec())
    assert cmd == ["lms", "load", "seat-x", "-c", "120064",
                   "--gpu", "max", "-y", "--parallel", "4"]
    assert "--ttl" not in cmd, (
        "a TTL on resume arms an auto-unload the human never chose"
    )


# --------------------------------------------------------------------------
# suspend / resume mechanics
# --------------------------------------------------------------------------

def test_suspend_verifies_the_unload_and_leaves_a_breadcrumb(monkeypatch):
    calls = []
    monkeypatch.setattr(seat_guard.ttyguard, "run",
                        lambda cmd, timeout: calls.append(cmd))
    monkeypatch.setattr(seat_guard, "record", lambda _id: None)  # gone

    assert seat_guard.suspend(_rec()) is None
    assert calls[0][:2] == ["lms", "unload"]
    crumb = json.loads(seat_guard.BREADCRUMB.read_text(encoding="utf-8"))
    assert "lms load seat-x" in crumb["reload"]


def test_suspend_that_does_not_actually_unload_is_an_error(monkeypatch):
    monkeypatch.setattr(seat_guard.ttyguard, "run", lambda cmd, timeout: None)
    monkeypatch.setattr(seat_guard, "record", lambda _id: _rec())  # still there
    err = seat_guard.suspend(_rec())
    assert err and "still loaded" in err


def test_resume_clears_the_breadcrumb_on_success(monkeypatch):
    seat_guard.BREADCRUMB.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(seat_guard.ttyguard, "run", lambda cmd, timeout: None)
    monkeypatch.setattr(seat_guard, "record", lambda _id: _rec())
    assert seat_guard.resume(_rec()) is None
    assert not seat_guard.BREADCRUMB.exists()


def test_resume_at_the_wrong_context_says_so(monkeypatch):
    """The JIT-default trap: loaded, but with a silently shrunken window —
    that failure surfaces MUCH later as truncation nobody can explain."""
    monkeypatch.setattr(seat_guard.ttyguard, "run", lambda cmd, timeout: None)
    monkeypatch.setattr(seat_guard, "record",
                        lambda _id: {**_rec(), "context": 4096})
    err = seat_guard.resume(_rec())
    assert err and "4096" in err and "120064" in err


def test_resume_failure_retries_then_hands_the_human_the_command(monkeypatch):
    attempts = []

    def failing_run(cmd, timeout):
        attempts.append(cmd)
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(seat_guard.ttyguard, "run", failing_run)
    err = seat_guard.resume(_rec())

    assert len(attempts) == seat_guard.RESUME_RETRIES
    assert err and "RESUME FAILED" in err
    assert "lms load seat-x" in err, (
        "the error text is for the TRANSCRIPT and the human — the model that "
        "would read it is the thing that is missing"
    )


# --------------------------------------------------------------------------
# the studio integration: order, finally, and who gets suspended when
# --------------------------------------------------------------------------

def _wire(monkeypatch, log, rec=None, resume_err=None):
    rec = rec or _rec()
    monkeypatch.setattr(seat_guard, "record", lambda _id: rec and dict(rec))
    monkeypatch.setattr(seat_guard, "suspend",
                        lambda r: log.append("suspend") or None)
    monkeypatch.setattr(seat_guard, "resume",
                        lambda r: log.append("resume") or resume_err)


def test_image_generate_suspends_generates_resumes_in_order(monkeypatch):
    log = []
    _wire(monkeypatch, log)
    monkeypatch.setattr(studio_tool, "_http",
                        lambda *a, **k: log.append("generate") or {"imagePath": "x.png"})

    out = studio_tool.run({"app": "image", "action": "generate",
                           "prompt": "a fox"}, seat_model="seat-x")

    assert log == ["suspend", "generate", "resume"], log
    assert "x.png" in out
    assert "suspended for this generation" in out
    assert "slow first token" in out, "the model must know to expect the re-read"


def test_the_seat_comes_back_even_when_generation_raises(monkeypatch):
    """The finally is the feature. A seat that stays down turns one failed
    image into a dead agent."""
    log = []
    _wire(monkeypatch, log)

    def boom(*a, **k):
        log.append("generate")
        raise RuntimeError("generation exploded")

    monkeypatch.setattr(studio_tool, "_http", boom)
    with pytest.raises(RuntimeError):
        studio_tool.run({"app": "image", "action": "generate",
                         "prompt": "x"}, seat_model="seat-x")
    assert log == ["suspend", "generate", "resume"], (
        "resume must run whatever the generation did"
    )


def test_status_actions_never_touch_the_seat(monkeypatch):
    log = []
    _wire(monkeypatch, log)
    monkeypatch.setattr(studio_tool, "_http", lambda *a, **k: {"ok": True})

    studio_tool.run({"app": "image", "action": "status"}, seat_model="seat-x")
    studio_tool.run({"app": "sound", "action": "gallery"}, seat_model="seat-x")

    assert log == [], "lookups must never unload anything"


def test_no_seat_injected_means_no_suspension(monkeypatch):
    log = []
    _wire(monkeypatch, log)
    monkeypatch.setattr(studio_tool, "_http", lambda *a, **k: {"imagePath": "x"})
    studio_tool.run({"app": "image", "action": "generate", "prompt": "x"})
    assert log == []


def test_an_unloaded_seat_generates_without_ceremony(monkeypatch):
    log = []
    monkeypatch.setattr(seat_guard, "record", lambda _id: None)
    monkeypatch.setattr(seat_guard, "suspend", lambda r: log.append("suspend"))
    monkeypatch.setattr(studio_tool, "_http", lambda *a, **k: {"imagePath": "x"})
    out = studio_tool.run({"app": "image", "action": "generate",
                           "prompt": "x"}, seat_model="seat-x")
    assert log == [] and "x" in out


def test_a_busy_model_refuses_the_generation_not_the_seat(monkeypatch):
    monkeypatch.setattr(seat_guard, "record",
                        lambda _id: {**_rec(), "status": "generating"})
    out = studio_tool.run({"app": "image", "action": "generate",
                           "prompt": "x"}, seat_model="seat-x")
    assert "not suspending" in out and "mid-stream" in out


def test_resume_problems_ride_on_the_result(monkeypatch):
    log = []
    _wire(monkeypatch, log, resume_err="SEAT RESUME FAILED — run: lms load seat-x")
    monkeypatch.setattr(studio_tool, "_http", lambda *a, **k: {"imagePath": "x.png"})
    out = studio_tool.run({"app": "image", "action": "generate",
                           "prompt": "x"}, seat_model="seat-x")
    assert "x.png" in out and "SEAT RESUME PROBLEM" in out


def test_sound_generation_is_waited_to_completion_inside_the_call(monkeypatch):
    """The poller is the thing that got unloaded — the tool owns the wait."""
    log = []
    _wire(monkeypatch, log)
    states = iter([{"state": "running"}, {"state": "running"},
                   {"state": "done", "file": "C:/out/track.mp3"}])

    def fake_http(method, url, body=None, timeout=0):
        if url.endswith("/generate/music"):
            log.append("submit")
            return {"job_id": "j-1"}
        log.append("poll")
        return next(states)

    monkeypatch.setattr(studio_tool, "_http", fake_http)
    monkeypatch.setattr(studio_tool.time, "sleep", lambda _s: None)

    out = studio_tool.run({"app": "sound", "action": "generate",
                           "mode": "music", "prompt": "rain",
                           "duration": 10}, seat_model="seat-x")

    assert log[0] == "suspend" and log[1] == "submit"
    assert log.count("poll") == 3
    assert log[-1] == "resume"
    assert "track.mp3" in out
