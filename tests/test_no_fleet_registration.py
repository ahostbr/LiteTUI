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

THE ARMS, and why the guard needs three of them (heartbeat arms follow):
  1. the runner ARMS the guard      — a guard nothing sets is not a guard
  2. armed, register() is inert     — the behaviour we want
  3. UNARMED, register() DOES fire  — the control. Without it, arm 2 would pass
                                      just as happily if register() had been
                                      deleted, or if the argv were malformed.
"""

from __future__ import annotations

import os

import pytest

from litetui import app as app_mod
from litetui import harness as harness_mod


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


# ── heartbeat ─────────────────────────────────────────────────────────────
#
# A seat that registers once and never beats decays to [ghost] while its
# process is plainly alive: `last_seen` is written at registration and never
# again. Measured 2026-08-20 at 10 minutes, on a seat carrying a LIVE pid —
# so a correct pid is not sufficient on its own.


def test_heartbeat_is_inert_when_the_guard_is_armed(recorder):
    seat = _seat()
    seat.registered = True          # pretend a real launch got this far
    assert seat.heartbeat() is False
    assert recorder.calls == [], recorder.calls


def test_heartbeat_does_nothing_for_a_seat_that_never_registered(recorder, monkeypatch):
    monkeypatch.delenv(harness_mod.NO_HARNESS_ENV, raising=False)
    seat = _seat()                  # registered is False
    assert seat.heartbeat() is False
    assert recorder.calls == [], "an unregistered seat has no presence to refresh"


def test_heartbeat_never_passes_takeover(recorder, monkeypatch):
    """🔴 THE ARM THAT MATTERS. register() CLAIMS a name; a heartbeat only says
    "still here". Beating with --takeover would make two instances fight for
    the name every minute — and would re-arm the eviction this seat was the
    victim of in the first place.
    """
    monkeypatch.delenv(harness_mod.NO_HARNESS_ENV, raising=False)
    seat = _seat()
    seat.registered = True
    assert seat.heartbeat() is True

    assert len(recorder.register_calls) == 1, recorder.calls
    argv = recorder.register_calls[0]
    assert "--takeover" not in argv, "a heartbeat must never claim the name"
    assert "--session-pid" in argv, (
        "a beat that drops session_pid refreshes the timestamp while clearing "
        "the field that decides ghost-vs-live"
    )
    assert str(os.getpid()) in argv


def test_register_and_heartbeat_send_the_same_presence_fields(monkeypatch):
    """One argv, because two copies drift — and the drift is invisible."""
    monkeypatch.delenv(harness_mod.NO_HARNESS_ENV, raising=False)
    seat = _seat()
    base = seat._presence_argv()
    for flag in ("--agent-id", "--cli", "--model", "--tier", "--name", "--session-pid"):
        assert flag in base, flag
    assert "--takeover" not in base, "takeover belongs to register(), not to the shared argv"


# ── arm 4: armed, the disabled seat is SILENT, not merely harmless ────────
#
# Arms 1-3 prove the guard stops registration reaching the live fleet, and they
# were airtight about it. Nothing asserted the other half: that a disabled seat
# also says nothing. It did not. `_inbox_monitor`'s failure branch called
# `_system(...)`, which mounts a ChatMessage and calls `_scroll_down()` -- so
# under the suite every app instance mounted a widget and scrolled the log from
# a background worker, at a moment set by how long `register` took to refuse.
#
# That is not cosmetic. Landing after a test's own content, that scroll is
# indistinguishable from the app autoscrolling on its own, and it is what made
# the three tests in test_thinking_autoscroll.py fail ~20% of the time for
# months -- diagnosed repeatedly as an autoscroll-policy bug, because the mount
# came from a worker nobody was looking at.
#
# THE SHAPE WORTH REMEMBERING: the guard was not missing and not un-invoked. It
# was PARTIAL -- it covered the registry side effect and not the UI one -- and
# from the caller a partial remedy is indistinguishable from a complete one.
# Every isolation gate deserves the question: what does this disable, and what
# does it merely decline to do quietly?


class _FakeSeat:
    def __init__(self, error: str) -> None:
        self.model = None
        self.name = "LiteTUI"
        self.agent_id = "test-agent-id"
        self.registered = False
        self.error = error

    def register(self) -> bool:
        return False


class _FakeApp:
    """Just enough app for _inbox_monitor's failure path, and nothing more."""

    def __init__(self, seat) -> None:
        self.seat = seat
        self.model_id = "m"
        self.said: list[str] = []
        self._seat_started = False

    def _system(self, text: str) -> None:
        self.said.append(text)


@pytest.mark.asyncio
async def test_a_disabled_seat_mounts_nothing(monkeypatch):
    """Armed: the worker must reach the UI zero times."""
    assert harness_mod.harness_disabled(), "arm 1 covers this; bail loudly if it regressed"

    app = _FakeApp(_FakeSeat(f"disabled by {harness_mod.NO_HARNESS_ENV}"))
    await app_mod.LiteTUI._inbox_monitor.__wrapped__(app)

    assert app._seat_started, (
        "the worker never got as far as the branch under test, so this proved nothing"
    )
    assert app.said == [], (
        f"a deliberately-disabled seat announced itself into the chat log: {app.said}. "
        "_system() mounts a widget AND scrolls, from a background worker, at an "
        "unpredictable moment."
    )


@pytest.mark.asyncio
async def test_control_a_genuinely_broken_seat_still_says_so(monkeypatch):
    """THE CONTROL. Without it, the arm above passes just as happily if the
    notice were deleted outright — and then a seat that is genuinely
    unreachable would fail in total silence, which is the bug the notice was
    written to prevent."""
    monkeypatch.setattr(harness_mod, "harness_disabled", lambda: False)

    app = _FakeApp(_FakeSeat("connection refused"))
    await app_mod.LiteTUI._inbox_monitor.__wrapped__(app)

    assert len(app.said) == 1 and "OFFLINE" in app.said[0], (
        f"a seat that failed for a REAL reason must still report it; said={app.said}"
    )
