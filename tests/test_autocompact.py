"""AUTO-COMPACT: the budget must be checked by the unit that spends it.

The bug this file guards, watched live: auto-compact enabled at 80%, and the
window sailed 80 -> 84 -> 87% while the turn kept starting tool calls. The
flag was on, the threshold was 80, and the comparison fires at `>=` — all
three correct. Both `_maybe_autocompact()` calls sat at the two `return`
statements of the agent loop, so a turn that keeps calling tools never reached
either. Context is consumed WITHIN a turn and the budget was checked only
BETWEEN turns; with tool_iterations at 100, one turn can spend the lot.

The fix is NOT "call _maybe_autocompact inside the loop". `_stream` and
`_compact` are both @work(exclusive=True, group="chat"), so starting a
compaction from inside the loop CANCELS the loop that started it — mid-turn,
possibly between an assistant message carrying tool_calls and its results.
The loop TESTS with `_autocompact_due()`, breaks, and schedules the
compaction for after the worker exits.

Second bug, same guard: `_autocompact_running` was set and then cleared in a
`finally` wrapped around the call that starts the compaction. That call is
@work — it SCHEDULES and returns immediately, so the flag was false again
microseconds later while the compaction was still in flight. It guarded the
scheduling call, never the compaction, which is how a failed compact retried
with no backoff.
"""
import ast
import io
from pathlib import Path
from types import SimpleNamespace

from app import LiteTUI

APP_SRC = Path(__file__).resolve().parent.parent / "app.py"


def stub(used=8700, mx=10000, enabled=True, at=80, loaded=True, failed_at=None):
    return SimpleNamespace(
        settings=SimpleNamespace(autocompact_enabled=enabled, autocompact_at_percent=at),
        ctx_used=used, ctx_max=mx, ctx_loaded=loaded,
        _autocompact_failed_at=failed_at,
    )


due = LiteTUI._autocompact_due


# --- the test itself: does it fire when it should, and only then? -----------
def test_due_fires_over_threshold():
    """POSITIVE CONTROL. Without this the rest could all pass on a dead test."""
    assert due(stub(used=8700, mx=10000, at=80)) == 87


def test_due_fires_exactly_at_threshold():
    """80% of the window must fire at >=, not >. The original was already
    correct here; this pins it so the rewrite did not regress it."""
    assert due(stub(used=8000, mx=10000, at=80)) == 80


def test_due_silent_below_threshold():
    assert due(stub(used=7999, mx=10000, at=80)) is None


def test_due_silent_when_disabled():
    assert due(stub(used=9900, enabled=False)) is None


def test_due_silent_when_window_unknown():
    """Never guess a threshold against a number the session does not have."""
    assert due(stub(used=9900, mx=0)) is None
    assert due(stub(used=0, mx=10000)) is None


def test_due_silent_when_model_not_loaded():
    """ctx_max is the model's CEILING until it loads, not its window."""
    assert due(stub(used=9900, loaded=False)) is None


# --- the retry loop: a failed compact must not be re-attempted identically ---
def test_failed_compact_blocks_an_identical_retry():
    assert due(stub(used=8700, failed_at=8700)) is None


def test_failed_compact_stops_blocking_once_the_window_moves():
    """NEGATIVE ARM — without this the test above is satisfied by a guard that
    blocks auto-compact forever, which is a worse bug than the retry loop."""
    assert due(stub(used=8701, failed_at=8700)) == 87


def test_unrelated_failure_marker_does_not_block():
    assert due(stub(used=8700, failed_at=1234)) == 87


# --- placement: the actual defect was WHERE the check ran -------------------
def _stream_loop() -> ast.For:
    tree = ast.parse(io.open(APP_SRC, encoding="utf-8").read())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "LiteTUI")
    fn = next(n for n in cls.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_stream")
    loops = [n for n in ast.walk(fn)
             if isinstance(n, ast.For)
             and isinstance(n.target, ast.Name) and n.target.id == "_iteration"]
    assert len(loops) == 1, f"expected one `for _iteration` loop, got {len(loops)}"
    return loops[0]


def _called_names(node) -> set:
    return {n.func.attr for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def test_the_budget_is_tested_inside_the_loop_that_spends_it():
    """The regression guard for the original bug. If this check drifts back
    out to the return statements, the window sails past the threshold again
    and nothing else in this file would notice."""
    assert "_autocompact_due" in _called_names(_stream_loop())


def test_the_loop_never_starts_a_compaction_from_inside_itself():
    """_compact is exclusive in the same worker group as _stream, so a direct
    call here would cancel the turn it was called from. Scheduling it
    (call_after_refresh(self._maybe_autocompact)) is a reference, not a Call,
    and stays allowed — this forbids only the direct invocation."""
    assert "_maybe_autocompact" not in _called_names(_stream_loop())


def test_the_loop_still_has_its_two_return_site_checks():
    """The between-turns checks are still correct and still needed — a turn
    that ends with a plain answer never reaches the in-loop check."""
    src = io.open(APP_SRC, encoding="utf-8").read()
    assert src.count("self.call_after_refresh(self._maybe_autocompact)") >= 2
