"""A swap must not prune a body while a child is mid-lifecycle. T704.

WHAT THIS FILE IS A GATE ON, and what it deliberately is NOT.

`test_swap_control_is_wired.py::test_pressing_the_control_swaps_in_a_sidebar`
[model_config] and [job] have been failing intermittently for weeks with
`NoMatches` for `SelectOverlay`, raised from inside Textual's own
`Select._on_mount`. It was carried as a flake. It is a race, and it is ours:
`DialogController.swap` tears the old view down while a `Select` inside the
body it is pruning has been attached but has not yet run `_pre_process`.

THE MECHANISM, read out of the INSTALLED Textual 8.1.0 source, not inferred:

  1. `App._prune` marks `_pruning` across `walk_children`.
  2. `Widget.mount` EARLY RETURNS `AwaitMount(self, [])` when `_pruning`
     (widget.py:1424) — and says nothing.
  3. `_pre_process` dispatches `Compose()` and then `Mount()` unconditionally
     (message_pump.py:599/604), and sets `_is_mounted = True` in its `finally`
     (message_pump.py:612) whatever happened in between.
  4. So the `Select` DOES compose — and the mount of what compose produced is
     the part that is silently thrown away. Then `Select._on_mount`
     (_select.py:623) calls `_setup_options_renderables`, which does
     `self.query_one(SelectOverlay)` at _select.py:546 -> NoMatches, raised out
     of a message handler, which kills the app.

CORRECTION, and it is one frame deep: the handoff and the `_mount_view` comment
both say a pruned `Select` "never got its children" and so crashed on Mount.
Compose runs. The mount of its OUTPUT is what is dropped. Same failure, same
raise site, one frame further in than the earlier account.

WHY THE ARM FORCES THE STATE INSTEAD OF WAITING FOR IT. The window is between a
`Select` being attached and its own `_pre_process` running, and it is invisible
at `asyncio.sleep(0)` granularity: pumping ticks after a sidebar opens gives
0 children, then 1 child with 0 Selects, then 10 Selects with 10 overlays, with
no observable step in between. Swapping in that middle state is green 3/3 —
there are no Select tasks in flight to catch. The state that fails lives inside
Textual's scheduler and only appears under machine load, which is why the only
instrument for the PRODUCT's timing is the seven-file reproducer:

    cd C:/Projects/LiteTUI/.worktrees/silverbolt-t596
    PYTHONUTF8=1 C:/Projects/LiteTUI/.venv/Scripts/python.exe -m pytest -q \\
      tests/test_hooks_ui.py tests/test_hook_boundaries.py \\
      tests/test_lifecycle_hooks.py tests/test_settings.py \\
      tests/test_settings_live.py tests/test_theme_extra_tokens.py \\
      tests/test_swap_control_is_wired.py

Order is part of the reproducer; do not sort it. THE INTERPRETER IS PART OF IT
TOO — the worktree `.venv` is py3.14.0 and does NOT reproduce; the main `.venv`
is py3.11.9 (textual 8.1.0 in both) and gave 2 red in 5 reps at 64ddf88, ~105s
each. Three "not reproduced" reports were filed against the wrong interpreter
before that was noticed. Print `sys.executable` before believing any run.

A THIRD TEARDOWN PATH IS NOT COVERED, AND IT WAS FOUND BY THIS FILE'S OWN
FLAKINESS. `_await_subtree_composed` sits on `close_view`, which is the single
door for both of OUR teardowns — `DialogController.swap` and `resolve()`, the
latter being every dialog that is simply answered. It is NOT the door for app
shutdown: Textual prunes the screen itself on exit, and a body left
half-composed at that moment raises the same NoMatches on the same line
(_select.py:546) through `app.py:2168 in run_test`. Measured at 2 in 14 runs of
this file before the second arm was made to settle before exiting. Nothing in
this module can guard that path, and a green file must not be read as covering
it.

THE HONEST LIMIT. The arm below proves the GUARD: given the state, OUR teardown
no longer crashes. It does not prove the product never reaches the state under
load, and a green here must never be quoted as "T704 is fixed" — the guard is
BOUNDED (an unbounded wait in a teardown is a hung dialog), so a load heavy
enough to need more than four ticks can still reach it.

WITHDRAWN, so nobody re-derives it: the hypothesis that T706 (`20daf09`) masked
this race. It was built on three greens whose coincidence probability had been
computed at 12.5% in the same message. Six further reps gave 2 red. The rate is
the same either side of T706.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from textual.widgets import Select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _settle import settle_until
from litetui import app as m
from litetui.plugins.model_switch import ModelConfigBody
from litetui.side_panel import DialogController, SidePanel, _is_composing


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


async def _open_sidebar(a, pilot):
    ctrl = DialogController(a, lambda: ModelConfigBody("a-model"), "sidebar", "right")
    a.run_worker(ctrl.open(), name="dlg")
    await settle_until(pilot, lambda: bool(a.screen.query(SidePanel)))
    return ctrl, a.screen.query_one(SidePanel).body


@pytest.mark.asyncio
async def test_a_swap_does_not_prune_a_select_that_has_not_composed():
    """THE ARM THAT GOES RED WITHOUT THE GUARD, pinned to the real raise site.

    The precondition is the traceback's own state: a `Select` attached to the
    body, its children not yet mounted, and a swap pruning that body. Without
    `_await_subtree_composed` this raises

        textual.css.query.NoMatches: No nodes match 'SelectOverlay'
                                     on Select(id='t704-arm')

    from `Select._on_mount` -> `_setup_options_renderables` -> _select.py:546,
    which is the same origin and the same widget path as the intermittent
    failure in test_swap_control_is_wired.py.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl, body = await _open_sidebar(a, pilot)

        sel = Select([("x", 1)], id="t704-arm")
        body.mount(sel)          # DELIBERATELY NOT AWAITED — this is the state
        assert not sel.children, "precondition lost: the Select composed too early"

        await ctrl.swap()        # prunes the body with that mount in flight

        for _ in range(6):
            await pilot.pause()  # the Mount that used to kill the app lands here
        assert ctrl.style == "modal", "the swap itself did not happen"
        assert ctrl.pending, "the teardown answered the dialog"


@pytest.mark.asyncio
async def test_the_guard_asks_about_children_because_is_mounted_cannot_answer():
    """`is_mounted` is the tempting predicate and it is the wrong one.

    Swapping `_is_composing` to `w.is_mounted` leaves the arm above GREEN —
    the guard would exhaust its four tries, and four `sleep(0)` ticks are
    themselves enough to let the Select compose in an idle test. So the arm
    above cannot see that substitution, and this one can: it asserts the
    measurement that disqualifies `is_mounted`, which is that a widget reads
    `is_mounted is False` while its composed children ALREADY EXIST.

    If a future Textual sets `_is_mounted` earlier this goes red — which is
    the right outcome: the choice recorded here would then be stale and worth
    re-reading rather than silently carried.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        _ctrl, body = await _open_sidebar(a, pilot)

        assert not _is_composing(body), (
            "a settled body should not look mid-compose — the guard would then "
            "wait on every teardown, which is a blanket sleep, not a guard"
        )

        sel = Select([("x", 1)], id="t704-predicate")
        body.mount(sel)
        assert _is_composing(body), "the predicate cannot see the state it guards"

        await asyncio.sleep(0)
        assert sel.children, "the Select did not compose within one tick"
        assert sel.is_mounted is False, (
            "is_mounted flipped once the children existed — the reason this "
            "guard asks about children instead no longer holds; re-read "
            "_await_subtree_composed before changing it"
        )

        # 🔴 NOT TIDY-UP. Leaving this body half-composed at app shutdown
        # reproduced the IDENTICAL crash — NoMatches on the real
        # Select(id='mc-preset-apply'), raised from _select.py:546 through
        # app.py:2168 in run_test — in 2 of 14 runs of this file. That path is
        # Textual pruning the screen on exit; it never touches close_view, so
        # the guard cannot see it and this arm must not depend on it. The
        # uncovered sibling is described in the module docstring; it is a
        # finding, not this arm's subject.
        await settle_until(pilot, lambda: not _is_composing(body))
