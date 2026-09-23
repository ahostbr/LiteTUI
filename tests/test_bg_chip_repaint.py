"""T581 — the footer chip and the Background panel must agree.

Ryan's screenshot, 2026-09-10 20:5x on a live Codex-OAuth seat: the footer read
`bg:1` while the Background panel read "(0) Nothing running in the background",
right after task `t-cc94ad` (a 300s timeout) finished and its inbox line was
delivered.

🔴 NOT TWO SOURCES — A MISSING REPAINT. Both surfaces already read
`tasks.split_live`: the chip through `ctx_label_text`, the panel through
`BackgroundProcessesBody.rows`. The panel is right because its own `sync()`
rebuilds on a membership change. The chip is wrong because `ctx_label_text` is a
PROPERTY: it recomputes correctly every time it is called, and nothing calls it
when a task starts or finishes. `_start_background` and `_run_background` both
move `bg_tasks` and neither repaints the footer, so the label keeps whatever it
was painted with the last time something unrelated refreshed it.

⚠️ AND THAT IS WHY THESE ARMS READ THE LABEL WIDGET, NEVER `app.ctx_label_text`.
The property is CORRECT before the fix — asserting it would be green against the
bug, which is the exact shape of an assertion satisfiable by the wrong answer.
The staleness lives in the rendered `.ctx-label`, so that is what is queried.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as m  # noqa: E402
from litetui import tasks as tasks_mod  # noqa: E402


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.bg_tasks = {}
    a.convo_id = "c1"
    return a


def painted(app) -> str:
    """What the footer ACTUALLY SHOWS — the rendered label, not the property."""
    labels = list(app.query(".ctx-label"))
    assert labels, "the footer label is not composed; the arm would measure nothing"
    return "".join(str(getattr(lb, "content", "")) for lb in labels)


def _painted_or_none(app) -> str | None:
    """`painted()` without its validity gate: None while the footer has no label.

    That window is REAL and is the whole flake. `ContextFooter.compose`
    (widgets.py) recomposes -- Textual removes the footer's children and re-runs
    compose -- so between the removal and the mount there are ZERO `.ctx-label`
    widgets. `painted()` asserts there is at least one, deliberately, because a
    measurement of nothing must never read as a pass. Calling it inside a wait
    loop turns that correct gate into the failure.
    """
    labels = list(app.query(".ctx-label"))
    if not labels:
        return None
    return "".join(str(getattr(lb, "content", "")) for lb in labels)


async def settle(pilot, app, *, chip: bool, tries: int = 20) -> str:
    """Pump the message loop until the painted chip agrees, then read it ONCE.

    🔴 THESE ARMS WERE MERGED FLAKY AND THIS IS WHY. Measured on merged main
    d040e7a, this file ALONE: 1 failure in 8 runs, then 4 across 6 runs, with
    ALL FOUR arms failing at least once -- the shared wait, not one arm. Not
    cold-start: cold and warm runs both failed and both passed. Every failure
    was the same exception, and it was NOT a stale chip --

        AssertionError: the footer label is not composed; the arm would
        measure nothing

    -- so the arms were landing inside the recompose window described in
    `_painted_or_none`, one `pilot.pause()` after the change.

    ⚠️ THE FINAL READ IS STILL `painted()`, WITH ITS GATE. Only the polling
    tolerates an absent label; the value the assertion sees is taken by the
    strict reader, so a footer that never composes still fails loudly instead of
    passing on an empty string.

    ⚠️ AND THIS CANNOT MASK THE BUG THE ARMS EXIST FOR. Against the unfixed
    code nothing repaints the footer at all, the condition never becomes true,
    and the arm fails after the whole budget -- the delete-equivalent probe in
    the commit re-runs to prove exactly that.
    """
    for _ in range(tries):
        text = _painted_or_none(app)
        if text is not None and ("bg:" in text) == chip:
            break
        await pilot.pause()
    return painted(app)


@pytest.mark.asyncio
async def test_the_chip_appears_when_a_task_starts() -> None:
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        assert "bg:" not in await settle(pilot, a, chip=False), (
            "a chip before anything started"
        )

        task = tasks_mod.new_task("bash", {"command": "sleep 300"}, "c1")
        a.bg_tasks[task.id] = task
        a._save_background()
        await pilot.pause()

        assert "bg:1" in await settle(pilot, a, chip=True), (
            "the footer did not repaint when a task started — the property is "
            "right and the label is stale"
        )


@pytest.mark.asyncio
async def test_the_chip_DROPS_when_the_task_finishes() -> None:
    """Ryan's exact case: the task ends on its own and the chip must go with it.

    Nothing is clicked and no turn runs, which is the whole difficulty — a
    background task finishing is the one state change with no user action
    attached to hang a repaint on.
    """
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        task = tasks_mod.new_task("bash", {"command": "sleep 300"}, "c1")
        a.bg_tasks[task.id] = task
        a._save_background()
        assert "bg:1" in await settle(pilot, a, chip=True)

        # It timed out. `finish` is what `_run_background` calls.
        tasks_mod.finish(task, "[error] timeout after 300s", False, tmp_root(a))
        a._save_background()
        await pilot.pause()

        assert "bg:" not in await settle(pilot, a, chip=False), (
            "the footer still shows a background task after it finished — this "
            "is the screenshot: chip says bg:1, panel says 0"
        )


@pytest.mark.asyncio
async def test_the_chip_and_the_panel_read_the_same_answer() -> None:
    """The card's actual requirement, asserted as an EQUALITY between the two
    surfaces rather than as two separate expectations — the failure Ryan saw was
    a DISAGREEMENT, and two arms that each check one side can both pass while
    the pair still disagrees."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        task = tasks_mod.new_task("bash", {"command": "sleep 300"}, "c1")
        a.bg_tasks[task.id] = task
        a._save_background()
        await pilot.pause()

        # The panel's own source, called directly. `BackgroundProcessesBody.rows`
        # is `_live(self.app)[1]`, and `app` is a read-only Textual property, so
        # the body cannot be pointed at an app without mounting it — the same
        # function is called here instead of faking the widget.
        await settle(pilot, a, chip=True)

        def agree() -> bool:
            chip_says_one = "bg:1" in painted(a)
            panel_rows = len(tasks_mod.split_live(a.bg_tasks.values())[1])
            return chip_says_one == (panel_rows == 1)

        assert agree(), "chip and panel disagree while the task is running"

        tasks_mod.finish(task, "done", True, tmp_root(a))
        a._save_background()
        await settle(pilot, a, chip=False)

        chip_has_bg = "bg:" in painted(a)
        panel_rows = len(tasks_mod.split_live(a.bg_tasks.values())[1])
        assert chip_has_bg == (panel_rows > 0), (
            f"chip shows a background task: {chip_has_bg}, panel rows: "
            f"{panel_rows} — the two surfaces disagree"
        )


@pytest.mark.asyncio
async def test_a_kill_drops_the_chip_too() -> None:
    """The third transition. `_kill_background` moves the state to KILLED, which
    takes the task out of `split_live` — so the chip has to follow, by the same
    route as a normal finish."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        task = tasks_mod.new_task("bash", {"command": "sleep 300"}, "c1")
        task.proc = object()
        a.bg_tasks[task.id] = task
        a._save_background()
        a._kill_background_tree = lambda t: None
        await pilot.pause()
        assert "bg:1" in await settle(pilot, a, chip=True)

        a._kill_background(task.id)

        assert "bg:" not in await settle(pilot, a, chip=False), (
            "the chip survived a kill"
        )


def tmp_root(app) -> Path:
    """A scratch root for `finish`, so the arm never writes the repo's
    output/tasks/ — the log write is incidental to what is being measured."""
    import tempfile

    return Path(tempfile.mkdtemp(prefix="litetui-t581-"))
