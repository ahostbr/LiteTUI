"""Tests for the WS7 handler-generation swap (``plugin_reload_swap``).

Fake-only: the swap is exercised against a throwaway registry pair and a set of
recording fake seams. No module import/reload, no activate() side effects, no
resource teardown, no real app. These tests pin the two hard invariants from
the module docstring:

  1. NO ROLLBACK IS EVER CLAIMED -- a post-swap activate failure is ``degraded``
     (restart required), never "restored" / "rolled back".
  2. THE OLD GENERATION IS PRESERVED UNTIL THE POINT OF NO RETURN -- every
     pre-swap failure (eligibility, generation, native, idle, validate,
     deactivate) leaves ``app.plugins`` untouched and calls NO mutation seam.

Seam ordering is also pinned: validate (all owners) -> deactivate (all owners)
-> pointer swap -> activate (all owners).
"""
from types import SimpleNamespace

from litetui.plugins import PluginRegistry
from litetui.plugin_reload_state import ActivitySnapshot
from litetui.plugin_reload_swap import (
    SwapSeams,
    commit_handler_candidate,
    _changed_handler_owners,
)

_IDLE = lambda: ActivitySnapshot()  # noqa: E731  all flags False -> clear


def _handler(tag: str):
    def _fn():
        return tag
    _fn.__name__ = f"handler_{tag}"
    return _fn


def _registry(pairs):
    """pairs: iterable of (owner, tool_name, run)."""
    reg = PluginRegistry()
    for owner, name, run in pairs:
        reg.add_tool(owner, {"function": {"name": name}}, run)
    return reg


def _app(plugins, backend=None):
    return SimpleNamespace(plugins=plugins, backend=backend if backend is not None else SimpleNamespace())


def _seams(log, owner):
    def mk(kind):
        def _f():
            log.append((kind, owner))
        return _f
    return SwapSeams(validate_new=mk("validate"),
                     deactivate_old=mk("deactivate"),
                     activate_new=mk("activate"))


# ── changed-owner detection ──────────────────────────────────────────────


def test_changed_owner_detection_finds_only_handler_changes():
    old_a, new_a = _handler("a0"), _handler("a1")
    same_b = _handler("b0")
    expected = _registry([("plug_a", "alpha", old_a), ("plug_b", "beta", same_b)])
    # plug_a's handler changed; plug_b's is the SAME object; "gamma" is NEW.
    candidate = _registry([("plug_a", "alpha", new_a),
                           ("plug_b", "beta", same_b),
                           ("plug_c", "gamma", _handler("g0"))])
    assert _changed_handler_owners(expected, candidate) == {"plug_a"}


def test_no_handler_change_is_a_noop():
    shared = _handler("s")
    expected = _registry([("p", "alpha", shared)])
    candidate = _registry([("p", "alpha", shared)])  # same run object
    app = _app(expected)
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_IDLE, eligible={"p": True}, swap_seams={})
    assert r.status == "no-op"
    assert r.swapped is False
    assert app.plugins is expected  # untouched


# ── happy path ───────────────────────────────────────────────────────────


def test_happy_path_swaps_pointer_and_orders_seams():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("a", "alpha", old), ("b", "beta", _handler("bo"))])
    candidate = _registry([("a", "alpha", new), ("b", "beta", _handler("bn"))])
    app = _app(expected)
    log = []
    seams = {"a": _seams(log, "a"), "b": _seams(log, "b")}
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_IDLE,
                                 eligible={"a": True, "b": True},
                                 swap_seams=seams)
    assert r.status == "swapped"
    assert r.swapped is True
    assert app.plugins is candidate
    # validate ALL -> deactivate ALL -> (swap) -> activate ALL
    assert log == [("validate", "a"), ("validate", "b"),
                   ("deactivate", "a"), ("deactivate", "b"),
                   ("activate", "a"), ("activate", "b")]
    assert "rollback" not in " ".join(r.reasons)


# ── pre-swap refusals: old generation preserved, NO mutation seam ────────


def _assert_preserved(app, expected, log):
    assert app.plugins is expected
    assert not any(k in ("deactivate", "activate") for k, _ in log)


def test_non_eligible_owner_refuses_before_any_seam():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("p", "alpha", old)])
    candidate = _registry([("p", "alpha", new)])
    app = _app(expected)
    log = []
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_IDLE, eligible={"p": False},
                                 swap_seams={"p": _seams(log, "p")})
    assert r.status == "restart-required"
    assert r.swapped is False
    _assert_preserved(app, expected, log)
    assert any("not handler-reload-eligible" in x for x in r.reasons)


def test_missing_seams_refuses_before_any_seam():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("p", "alpha", old)])
    candidate = _registry([("p", "alpha", new)])
    app = _app(expected)
    log = []
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_IDLE, eligible={"p": True}, swap_seams={})
    assert r.status == "restart-required"
    assert r.swapped is False
    _assert_preserved(app, expected, log)
    assert any("no lifecycle seams" in x for x in r.reasons)


def test_not_idle_defers_before_any_seam():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("p", "alpha", old)])
    candidate = _registry([("p", "alpha", new)])
    app = _app(expected)
    log = []
    busy = lambda: ActivitySnapshot(turn_active=True, tool_active=True)  # noqa: E731
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=busy, eligible={"p": True},
                                 swap_seams={"p": _seams(log, "p")})
    assert r.status == "deferred"
    assert r.swapped is False
    _assert_preserved(app, expected, log)
    assert len(r.reasons) == 2


def test_generation_changed_defers():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("p", "alpha", old)])
    candidate = _registry([("p", "alpha", new)])
    stranger = _registry([("p", "alpha", old)])
    app = _app(stranger)  # app.plugins is NOT expected
    log = []
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_IDLE, eligible={"p": True},
                                 swap_seams={"p": _seams(log, "p")})
    assert r.status == "deferred"
    assert r.swapped is False
    assert app.plugins is stranger  # still untouched
    _assert_preserved(app, stranger, log)


def test_native_backend_refuses():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("p", "alpha", old)])
    candidate = _registry([("p", "alpha", new)])
    app = _app(expected, backend=SimpleNamespace(app_server=object()))
    log = []
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_IDLE, eligible={"p": True},
                                 swap_seams={"p": _seams(log, "p")})
    assert r.status == "restart-required"
    assert r.swapped is False
    _assert_preserved(app, expected, log)


def _boom():
    raise RuntimeError("activity down")


def test_activity_error_fails():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("p", "alpha", old)])
    candidate = _registry([("p", "alpha", new)])
    app = _app(expected)
    log = []
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_boom, eligible={"p": True},
                                 swap_seams={"p": _seams(log, "p")})
    assert r.status == "failed"
    assert r.swapped is False
    _assert_preserved(app, expected, log)


def test_activity_not_a_snapshot_fails():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("p", "alpha", old)])
    candidate = _registry([("p", "alpha", new)])
    app = _app(expected)
    log = []
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=lambda: {"turn_active": False},  # noqa: E731
                                 eligible={"p": True},
                                 swap_seams={"p": _seams(log, "p")})
    assert r.status == "failed"
    assert r.swapped is False
    _assert_preserved(app, expected, log)


# ── validate / deactivate failures: still before the point of no return ──


def test_validate_failure_refuses_before_swap():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("p", "alpha", old)])
    candidate = _registry([("p", "alpha", new)])
    app = _app(expected)
    log = []

    def _bad_validate():
        log.append(("validate", "p"))
        raise ValueError("refusing")
    seams = SwapSeams(validate_new=_bad_validate,
                      deactivate_old=lambda: log.append(("deactivate", "p")),
                      activate_new=lambda: log.append(("activate", "p")))
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_IDLE, eligible={"p": True},
                                 swap_seams={"p": seams})
    assert r.status == "failed"
    assert r.swapped is False
    assert app.plugins is expected  # NOT swapped
    assert log == [("validate", "p")]  # validate ran; deactivate/activate did NOT
    assert any("validate refused" in x for x in r.reasons)


def test_deactivate_failure_preserves_old_gen_before_swap():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("p", "alpha", old)])
    candidate = _registry([("p", "alpha", new)])
    app = _app(expected)
    log = []

    def _bad_deactivate():
        log.append(("deactivate", "p"))
        raise RuntimeError("join timed out")
    seams = SwapSeams(validate_new=lambda: log.append(("validate", "p")),
                      deactivate_old=_bad_deactivate,
                      activate_new=lambda: log.append(("activate", "p")))
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_IDLE, eligible={"p": True},
                                 swap_seams={"p": seams})
    assert r.status == "failed"
    assert r.swapped is False
    assert app.plugins is expected  # point of no return NOT crossed
    assert log == [("validate", "p"), ("deactivate", "p")]  # activate NOT called
    assert any("deactivate failed" in x for x in r.reasons)
    assert any("old generation preserved" in x for x in r.reasons)


def test_deactivate_partial_teardown_is_reported_not_rolled_back():
    # two owners, sorted a < b: a deactivates, b fails -> partial teardown
    a_old, a_new = _handler("a0"), _handler("a1")
    b_old, b_new = _handler("b0"), _handler("b1")
    expected = _registry([("a", "alpha", a_old), ("b", "beta", b_old)])
    candidate = _registry([("a", "alpha", a_new), ("b", "beta", b_new)])
    app = _app(expected)
    log = []

    def _ok_deact(o):
        def _f():
            log.append(("deactivate", o))
        return _f
    def _bad_deact():
        def _f():
            log.append(("deactivate", "b"))
            raise RuntimeError("b join failed")
        return _f
    seams = {
        "a": SwapSeams(validate_new=lambda: log.append(("validate", "a")),
                       deactivate_old=_ok_deact("a"),
                       activate_new=lambda: log.append(("activate", "a"))),
        "b": SwapSeams(validate_new=lambda: log.append(("validate", "b")),
                       deactivate_old=_bad_deact(),
                       activate_new=lambda: log.append(("activate", "b"))),
    }
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_IDLE, eligible={"a": True, "b": True},
                                 swap_seams=seams)
    assert r.status == "failed"
    assert r.swapped is False
    assert app.plugins is expected  # NOT swapped
    assert any("partial teardown" in x and "a" in x for x in r.reasons)
    # a's activate must NOT have been attempted (swap never happened)
    assert not any(k == "activate" for k, _ in log)


# ── activate failure AFTER the swap: degraded, no rollback ───────────────


def test_activate_failure_after_swap_is_degraded_with_no_rollback_claim():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("p", "alpha", old)])
    candidate = _registry([("p", "alpha", new)])
    app = _app(expected)
    log = []

    def _bad_activate():
        log.append(("activate", "p"))
        raise RuntimeError("new module global clobbered")
    seams = SwapSeams(validate_new=lambda: log.append(("validate", "p")),
                      deactivate_old=lambda: log.append(("deactivate", "p")),
                      activate_new=_bad_activate)
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_IDLE, eligible={"p": True},
                                 swap_seams={"p": seams})
    assert r.status == "degraded"
    assert r.swapped is True
    assert app.plugins is candidate  # pointer WAS swapped (point of no return)
    assert log == [("validate", "p"), ("deactivate", "p"), ("activate", "p")]
    joined = " ".join(r.reasons)
    assert "restart required" in joined
    assert "no rollback" in joined
    # never claim the old generation was restored
    assert "restored" not in joined.lower()
    assert "rolled back" not in joined.lower()


def test_happy_path_never_mentions_rollback():
    old, new = _handler("o"), _handler("n")
    expected = _registry([("p", "alpha", old)])
    candidate = _registry([("p", "alpha", new)])
    app = _app(expected)
    r = commit_handler_candidate(app, expected, candidate,
                                 activity=_IDLE, eligible={"p": True},
                                 swap_seams={"p": _seams([], "p")})
    assert r.status == "swapped"
    assert "rollback" not in " ".join(r.reasons).lower()
