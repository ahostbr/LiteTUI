"""begin_shutdown: synchronously block new loads on retained sessions, fault-isolated.

Fake sessions/backends, no engine, no starts/stops. The _shutdown ORDERING test
(begin_close before the first await / backend stop) lives alongside once the app hunk
lands; this file pins the helper's collection, dedup, fault-isolation and diagnostics.
"""
from types import SimpleNamespace

import pytest

from litetui import resource_session_lifecycle as rsl


class _FakeSession:
    def __init__(self, leases=0, active=0, unsettled=0, claims=0, fail=False):
        self._closing = False
        self.leases = {f"m{i}": i for i in range(leases)}
        self.active_loads = {f"a{i}" for i in range(active)}
        self.unsettled_loads = {f"u{i}": i for i in range(unsettled)}
        self.unload_claims = {f"c{i}": i for i in range(claims)}
        self._fail = fail

    _SECRET = "super-secret-token-and-a-very-long-tail"

    def begin_close(self):
        if self._fail:
            raise RuntimeError(self._SECRET * 20)
        self._closing = True


def _app(backend_session=None, registry=None):
    backend = SimpleNamespace(_admission_session=backend_session) if backend_session else None
    return SimpleNamespace(backend=backend, _admission_sessions=registry)


def test_empty_app_is_an_empty_report():
    assert rsl.begin_shutdown(_app()) == {}
    assert rsl.begin_shutdown(_app(registry=[])) == {}


def test_current_and_registry_sessions_begin_close_deduped():
    cur = _FakeSession(leases=2, active=1)
    other = _FakeSession(claims=3)
    # `cur` appears BOTH as the current backend session and in the registry.
    app = _app(backend_session=cur, registry=[(object(), cur), (object(), other)])
    report = rsl.begin_shutdown(app)
    assert cur._closing and other._closing
    assert len(report) == 2                       # cur counted once despite two references
    assert report[id(cur)] == {"closing": True, "leases": 2, "active_loads": 1,
                               "unsettled_loads": 0, "unload_claims": 0}
    assert report[id(other)]["unload_claims"] == 3


def test_malformed_registry_entry_does_not_abort_valid_sessions(monkeypatch):
    logged = []
    monkeypatch.setattr(rsl.runtime_log, "record_error",
                        lambda event, **kw: logged.append((event, kw.get("detail", ""))))
    good = _FakeSession(leases=1)
    app = _app(registry=["not-a-tuple", (object(), good)])
    report = rsl.begin_shutdown(app)
    assert good._closing                          # the valid session still closed
    assert report[id(good)]["leases"] == 1
    assert "registry[0]" in report and "error" in report["registry[0]"]
    assert any(e == "admission.shutdown_registry_malformed" for e, _ in logged)


def test_failing_begin_close_is_isolated_surfaced_and_secret_bounded(monkeypatch):
    logged = []
    monkeypatch.setattr(rsl.runtime_log, "record_error",
                        lambda event, **kw: logged.append((event, kw.get("detail", ""))))
    bad = _FakeSession(fail=True)
    good = _FakeSession(leases=1)
    app = _app(backend_session=bad, registry=[(object(), good)])
    report = rsl.begin_shutdown(app)
    assert good._closing                          # one failure does not skip the rest
    err = report[id(bad)]["error"]
    assert err == "RuntimeError"                  # TYPE ONLY — never the exception text
    logged_details = " ".join(d for _e, d in logged)
    # the secret in the exception message reaches NEITHER the report NOR the log
    assert _FakeSession._SECRET not in err
    assert _FakeSession._SECRET not in logged_details
    assert any(e == "admission.shutdown_begin_close_failed" for e, _ in logged)  # surfaced


# ── the REAL LiteTUI._shutdown seam: begin_close BEFORE the first await ──────

class _StopShutdown(Exception):
    pass


@pytest.mark.asyncio
async def test_real_shutdown_begins_close_before_first_await():
    from litetui.app import LiteTUI

    session = _FakeSession(leases=1)
    order = []

    class _Ops:
        async def close(self):
            # The FIRST await in _shutdown. begin_close must already have run.
            order.append(("operations.close", session._closing))
            raise _StopShutdown        # abort before _settle/backend stop/super()

    class _Backend:
        name = "llama"                 # not codex -> backend stop is skipped anyway
        _admission_session = session

    app = SimpleNamespace(
        _gui_quitting=False,
        backend=_Backend(),
        _admission_sessions=[(_Backend(), session)],
        _child_delivery_timer=None,
        _agent_operations=_Ops(),
    )

    with pytest.raises(_StopShutdown):
        await LiteTUI._shutdown(app)

    # Ordering: the session was begin_closed BEFORE operations.close was awaited.
    assert order == [("operations.close", True)]
    assert session._closing is True
    # The retained-state report is stored for the shutdown diagnostic, not a bool.
    assert app._shutdown_admission_report[id(session)]["closing"] is True
    assert app._shutdown_admission_report[id(session)]["leases"] == 1
