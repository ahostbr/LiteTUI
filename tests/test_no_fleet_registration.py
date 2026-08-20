"""The test suite must never write to the live LiteHarness registry.

WHAT HAPPENED (2026-08-20). Constructing LiteTUI registers a harness seat, and
`Seat.register()` passes `--takeover`. Takeover is DOCUMENTED to refuse a live
holder; measurably it does not. So every `python tests/run_all.py` took the name
"LiteTUI" from Ryan's running instance and moved its registry row into
`~/.liteharness/.ghost_evicted_<date>/`.

Measured: the live app was pid 474900 and its record was in the graveyard, while
the roster's only LiteTUI row carried the pid of a test process that had already
exited — so `discover` showed a ghost and the real app showed nothing. It was
diagnosed as a stale-pid bug in the app for some time before the experiment
(snapshot registry -> run suite -> new seat appears) named the actual cause.

Same family as `lms load` on connect and the `.convos` pollution: the app's own
startup path reaching LIVE SHARED STATE from a test.

THE ARMS, and why there are three:
  1. the runner ARMS the guard      — a guard nothing sets is not a guard
  2. armed, register() is inert     — the behaviour we want
  3. UNARMED, register() DOES fire  — the control. Without it, arm 2 would pass
                                      just as happily if register() had been
                                      deleted, or if the argv were malformed.
"""

from __future__ import annotations

import os

import pytest

import harness as harness_mod


class _Recorder:
    """Stands in for ttyguard.run and remembers every argv it was handed.

    Behavioural, not a grep: a source scan passes the moment the call moves
    behind a helper.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)])

        class _P:
            returncode = 0
            stdout = "Registered agent x: cli=litetui, model=m, tier=worker, name=LiteTUI"
            stderr = ""

        return _P()

    @property
    def register_calls(self) -> list[list[str]]:
        return [c for c in self.calls if "register" in c]


@pytest.fixture
def recorder(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(harness_mod, "ttyguard", type("_T", (), {"run": staticmethod(r)}))
    return r


def _seat() -> harness_mod.Seat:
    return harness_mod.Seat(agent_id="test-agent-id", name="LiteTUI", model="m")


# ── arm 1: the runner arms the guard ──────────────────────────────────────
def test_the_suite_arms_the_guard():
    """conftest.py (pytest) and run_all.py (scripts) both set this.

    This is the arm that catches the guard being disarmed by deleting one
    line in a runner — the failure that leaves every other assertion here
    passing while the live registry is writable again.
    """
    assert os.environ.get(harness_mod.NO_HARNESS_ENV, "").strip(), (
        f"{harness_mod.NO_HARNESS_ENV} is not set while the suite is running. "
        "tests/conftest.py must set it before app/harness import."
    )
    assert harness_mod.harness_disabled()


# ── arm 2: armed, registration is inert ───────────────────────────────────
def test_register_is_a_noop_when_disabled(recorder):
    seat = _seat()
    assert seat.register() is False
    assert seat.registered is False
    assert recorder.register_calls == [], (
        f"registration reached the live fleet: {recorder.register_calls}"
    )


def test_a_disabled_seat_says_so_rather_than_faking_success(recorder):
    """The footer must read "unregistered", not a name the fleet never granted.

    Reporting a quiet success here would hide the exact state the guard exists
    to produce.

    ⚠️ TAKES `recorder` EVEN THOUGH THE GUARD SHOULD STOP THE CALL. It did not
    take it originally, and when the guard was PLANTED OUT to prove this file
    discriminates, this test reached the real registry and left a live row
    named `test-agent-id`. A test that only stays safe while the thing it
    tests is working is not isolated — it is lucky. Stub the transport in
    EVERY test that can call register(), not only the ones expected to.
    """
    seat = _seat()
    seat.register()
    assert seat.error and harness_mod.NO_HARNESS_ENV in seat.error


# ── arm 3: THE CONTROL — unarmed, it really does register ─────────────────
def test_control_registration_fires_when_not_disabled(recorder, monkeypatch):
    """Proves arms 1-2 could have failed.

    ttyguard is replaced, so this exercises the real code path WITHOUT
    touching the real registry. Deleting register()'s body would fail here
    while leaving every other test in this file green.
    """
    monkeypatch.delenv(harness_mod.NO_HARNESS_ENV, raising=False)
    assert not harness_mod.harness_disabled()

    seat = _seat()
    assert seat.register() is True

    assert len(recorder.register_calls) == 1, recorder.calls
    argv = recorder.register_calls[0]
    assert "--takeover" in argv
    assert "--session-pid" in argv
    assert str(os.getpid()) in argv, "the seat must register its OWN pid"
