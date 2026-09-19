"""The loop breaker stops a model repeating an identical call that returns an
identical result — the failure measured 2026-09-19 (convo b4c93292): 16+
back-to-back `chrome shot` calls, byte-identical result each time, no
state change, nothing in the harness noticing.

The breaker must ALSO let legitimate repetition live: a poll repeats the same
call until the answer changes. The discriminator is the result — identical
results escalate, a changed result breaks the run.

Routes through the unbound helpers on a fake self (they only touch
self._loop_history, so no running app is needed), in the same order
_execute_tool uses them: refusal -> record -> warn.
"""
from __future__ import annotations

import types

from litetui.app import LiteTUI


def _fake():
    ns = types.SimpleNamespace()
    # The helpers call their siblings via self (idiomatic app code), so the
    # fake must resolve them: bind the REAL unbound methods onto the namespace.
    # _loop_key is a staticmethod — attach it plain, or the call would pass
    # the namespace as an extra first argument.
    ns._loop_key = LiteTUI._loop_key
    for m in ("_loop_run", "_loop_refusal", "_loop_warn", "_loop_record"):
        setattr(ns, m, types.MethodType(getattr(LiteTUI, m), ns))
    return ns


def call(fake, name, args, result):
    """One round of _execute_tool's loop-breaker integration, in order."""
    refusal = LiteTUI._loop_refusal(fake, name, args)
    if refusal is not None:
        return refusal, False
    LiteTUI._loop_record(fake, name, args, result)
    return LiteTUI._loop_warn(fake, name, args, result), True


def test_first_call_is_unmarked():
    fake = _fake()
    out, ok = call(fake, "chrome", {"action": "shot"}, "Saved C:/x/chrome-shot.png.")
    assert ok
    assert out == "Saved C:/x/chrome-shot.png."


def test_second_identical_call_gets_a_watch_note():
    fake = _fake()
    r = "Saved C:/x/chrome-shot.png."
    call(fake, "chrome", {"action": "shot"}, r)
    out, ok = call(fake, "chrome", {"action": "shot"}, r)
    assert ok
    assert out.startswith(r)
    assert "loop-watch" in out


def test_third_and_fourth_get_the_escalation_note():
    fake = _fake()
    r = "Saved C:/x/chrome-shot.png."
    for _ in range(3):
        out, ok = call(fake, "chrome", {"action": "shot"}, r)
        assert ok
    assert "refused" in out
    assert "loop-watch" in out


def test_fifth_identical_call_is_refused_before_it_runs():
    fake = _fake()
    r = "Saved C:/x/chrome-shot.png."
    for _ in range(4):
        call(fake, "chrome", {"action": "shot"}, r)
    out, ok = call(fake, "chrome", {"action": "shot"}, r)
    assert not ok
    assert out.startswith("[loop-break]")
    assert "5th" in out


def test_the_refusal_stays_refused_while_the_run_is_unchanged():
    fake = _fake()
    r = "Saved C:/x/chrome-shot.png."
    for _ in range(4):
        call(fake, "chrome", {"action": "shot"}, r)
    # Refused calls are not recorded, so the run stays at 4 — the next
    # attempt is refused again, not allowed through.
    out, ok = call(fake, "chrome", {"action": "shot"}, r)
    assert not ok
    out, ok = call(fake, "chrome", {"action": "shot"}, r)
    assert not ok


def test_a_changed_result_breaks_the_run():
    """The poll case: same call, the world finally changes."""
    fake = _fake()
    call(fake, "studio", {"action": "job"}, "running")
    call(fake, "studio", {"action": "job"}, "running")
    out, ok = call(fake, "studio", {"action": "job"}, "done")
    assert ok
    assert "loop-watch" not in out
    # After the change, repeats start a FRESH run.
    out, ok = call(fake, "studio", {"action": "job"}, "running")
    assert ok
    assert "loop-watch" not in out


def test_identical_results_with_an_intervening_different_call_do_not_stack():
    fake = _fake()
    r = "Saved C:/x/chrome-shot.png."
    call(fake, "chrome", {"action": "shot"}, r)
    call(fake, "chrome", {"action": "tabs"}, "1  about:blank")
    out, ok = call(fake, "chrome", {"action": "shot"}, r)
    assert ok
    assert "loop-watch" not in out


def test_different_arguments_are_a_different_call():
    """Two arg variants interleave without inheriting each other's run."""
    fake = _fake()
    r = "Saved C:/x/chrome-shot.png."
    for _ in range(3):
        out, ok = call(fake, "chrome", {"action": "shot"}, r)
        assert ok
        assert "loop-watch" not in out
        out, ok = call(fake, "chrome", {"action": "shot", "tab": 2}, r)
        assert ok
        assert "loop-watch" not in out


def test_different_tools_do_not_stack():
    fake = _fake()
    r = "ok"
    for i in range(5):
        out, ok = call(fake, f"tool{i}", {}, r)
        assert ok


def test_repeated_failures_are_a_loop_too():
    """An identical ERROR string four times is the same stuck shape."""
    fake = _fake()
    e = "[error] ChromeError: selector not found: #nope"
    for _ in range(4):
        call(fake, "chrome", {"action": "click", "selector": "#nope"}, e)
    out, ok = call(fake, "chrome", {"action": "click", "selector": "#nope"}, e)
    assert not ok
    assert out.startswith("[loop-break]")


def test_history_is_capped():
    fake = _fake()
    for i in range(30):
        LiteTUI._loop_record(fake, f"tool{i}", {}, "r")
    assert len(fake._loop_history) == 12


def test_two_instances_do_not_share_history():
    a, b = _fake(), _fake()
    r = "Saved C:/x/chrome-shot.png."
    for _ in range(4):
        call(a, "chrome", {"action": "shot"}, r)
    out, ok = call(b, "chrome", {"action": "shot"}, r)
    assert ok
    assert out == r


def test_execute_tool_wires_all_three_hooks():
    """Integration guard: the helpers are useless if _execute_tool stops
    calling them (T135-class wiring regression)."""
    import inspect

    src = inspect.getsource(LiteTUI._execute_tool)
    assert "_loop_refusal" in src
    assert "_loop_record" in src
    assert "_loop_warn" in src
