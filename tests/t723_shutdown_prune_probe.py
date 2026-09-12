"""AN INSTRUMENT, NOT A GATE. The reproduction T723's fix is measured against.

    PYTHONUTF8=1 C:/Projects/LiteTUI/.venv/Scripts/python.exe -m pytest -q -s \\
        tests/t723_shutdown_prune_probe.py

Count the `[faithful]` lines that report NoMatches. Roughly 3 whole-file runs in
10 reproduce it WITHOUT the guard; `LITETUI_T723_GUARD=off` restores that
control arm on a tree that has the guard, so both arms run identical code.

📌 DELIBERATELY NOT NAMED `test_*.py`, like `picture_probe.py` beside it:
`tests/run_all.py` globs `test_*.py` and pytest's default collection does the
same, so neither sees this. Passing the path explicitly still collects the
`test_*` functions inside it, which is the point — it runs under pytest so
`conftest.py` applies.

🔴 WHAT T723 WAS. `test_swap_control_is_wired.py::test_pressing_the_control_
swaps_in_a_sidebar[job]/[model_config]` failed intermittently for weeks with
`NoMatches: No nodes match 'SelectOverlay'`. T704 captured its frames and proved
the raise comes from Textual pruning the screen at APP SHUTDOWN:

    contextlib.py:217 __aexit__ -> textual/app.py:2168 run_test
    -> message_pump.py:604 _pre_process -> _select.py:625 _on_mount
    -> _select.py:546 _setup_options_renderables -> NoMatches

Not one frame is ours. T704's guard on `close_view` fixes a real race on OUR
teardown and does not touch this one.

🔴 FOUR MEASUREMENTS, in the order they killed each candidate fix. The card was
on the point of closing NO-FIX on the strength of (iv); (iv) was wrong, and the
correction is why there is a guard at all.

  (i)  `on_unmount` CAN NEVER BE THE PLACE. Textual 8.1.0's `App._shutdown`
       (app.py:3687) runs `await self._close_all()` — the prune, where the
       crash is — BEFORE `await self._dispatch_message(events.Unmount())`. A
       wait installed in `on_unmount` left the reproduction red at every pause
       count: unchanged, not partly fixed. `run_test` calls `_shutdown`
       directly (app.py:2163), so no quit action of ours is on that path
       either; the only earlier seam is `_shutdown` itself.

  (ii) NOTHING IS LOST WHEN IT CRASHES. `on_unmount` was observed ENTERING and
       COMPLETING on every single reproduction, crash included — so the
       `app_shutdown` lifecycle drain still runs. The cost is a traceback on
       exit and an intermittent suite red, not lost hooks and not lost state.
       So the guard removes a traceback and an intermittent red — it does not
       recover state, because none was being lost. Anyone weighing a change
       here against its risk should weigh it against THAT, not against
       imagined data loss.

  (iii) THE DETERMINISTIC REPRODUCTION IS UNFAITHFUL. Mounting a `Select`
       un-awaited and calling `app.exit()` in the SAME turn raises 4-for-4 —
       but at that moment `app._running` is False and the widget's `_running`
       is False, because `exit()` has already stopped the pumps. That Select
       can never compose, so no wait could ever fix it, and a fix built against
       it would be a fix for a state the product does not reach.

  (iv) RETRACTED, AND THE RETRACTION IS THE REASON THIS FILE IS THE
       INSTRUMENT. I first reported that the faithful state "does not
       reproduce", on two 5-run samples that happened to come back green. It
       does reproduce: **3 whole-file runs in 10** raise NoMatches in the
       faithful case, on `Select(id='mc-preset-apply')` — a real body Select,
       the same class as the captured production failure.

       🔴 THE INSTRUMENT IS THE FILE, NOT THE TEST. Run the faithful case
       ALONE and it is 0 in 12; run it as part of this file and it is 3 in 10.
       The unfaithful cases above run first and leave the loop in the state the
       faithful ones need. Isolating the case to "focus" on it destroys the
       very condition it measures — the seven-file-order lesson from T704, in
       miniature, and I nearly threw the reproduction away by doing exactly
       that.

       So: run the WHOLE FILE, count the `[faithful]` lines that say NoMatches,
       and never quote a green from fewer runs than the rate deserves. Two
       clean 5-run samples were the evidence for a claim that was simply false.

⚠️ THE INSTRUMENT TRAP THIS COST ME FIRST, because it produced the answer I was
looking for: `app.on_unmount = spy` NEVER FIRES. Textual resolves handlers on
the CLASS, so an instance attribute is not the handler it dispatches — and the
probe printed `entered=None`, which reads exactly like "the crash skipped
on_unmount". Patch the class (`monkeypatch.setattr(m.LiteTUI, "on_unmount", ...)`)
or measure nothing.

THE A/B THAT SHIPPED THE GUARD, interleaved one run each so neither arm could
be confounded with the box's other work (T716's whisper transcription and a
Resolve render began partway through, visible in the elapsed column):

    WITHOUT guard: 6 reproductions in 30 runs      (2/12 before, 4/18 after)
    WITH    guard: 0 reproductions in 30 runs
    P(0 in 30 | p = 0.20) = 0.80 ** 30 = 0.0012

Both parametrisations are kept: the unfaithful one because it sets up the state
the faithful one needs, and the faithful one because it IS the measurement.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui.plugins.model_switch import ModelConfigBody
from litetui.side_panel import DialogController, SidePanel


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


async def _open_sidebar(a, pilot, pauses: int):
    ctrl = DialogController(a, lambda: ModelConfigBody("a-model"), "sidebar", "right")
    a.run_worker(ctrl.open(), name="dlg")
    for _ in range(pauses):
        await pilot.pause()
    panels = a.screen.query(SidePanel)
    return panels.first().body if panels else None


@pytest.mark.asyncio
@pytest.mark.parametrize("pauses", [0, 1, 2, 3, 4])
async def test_unfaithful_exit_in_the_same_turn(monkeypatch, pauses):
    """(iii) Reproduces 4-for-4 at pauses>=1 — and cannot be fixed by waiting.

    `exit()` in the same turn stops the pumps, so the attached Select's
    `_pre_process` will never run no matter how long anything waits.
    """
    from textual.widgets import Select

    seen: dict[str, bool] = {}
    original = m.LiteTUI.on_unmount

    async def spy(self):
        seen["entered"] = True
        await original(self)
        seen["completed"] = True

    monkeypatch.setattr(m.LiteTUI, "on_unmount", spy)

    a = make_app()
    raised = ""
    try:
        async with a.run_test(size=(120, 40)) as pilot:
            body = await _open_sidebar(a, pilot, pauses)
            if body is not None:
                body.mount(Select([("x", 1)], id="t723-forced"))
            a.exit()
    except Exception as error:  # noqa: BLE001 - the probe's job is to report
        # WHATEVER escapes the shutdown, including a class we have not seen yet.
        raised = f"{type(error).__name__}: {str(error)[:90]}"
    print(f"\n[unfaithful] pauses={pauses} raised={raised or 'none'} "
          f"on_unmount entered={seen.get('entered')} completed={seen.get('completed')}")


@pytest.mark.asyncio
@pytest.mark.parametrize("pauses", [0, 1, 2, 3, 4])
async def test_faithful_state_reproduces_without_the_guard(monkeypatch, pauses):
    """(iv) The state the real failures are in, and the measurement itself.

    No `exit()`: the app is still running when `run_test`'s finally calls
    `_shutdown`, which is the shape of every captured failure. Thirty
    un-awaited mounts is more than one tick's worth of composing.

    ⚠️ ITS FIRST NAME WAS `..._does_not_reproduce`, which was FALSE and would
    have outlived the claim that produced it. A file named for a conclusion
    keeps asserting that conclusion to every later reader, long after the
    measurement behind it has been retracted.
    """
    from textual.widgets import Select

    seen: dict[str, bool] = {}
    original = m.LiteTUI.on_unmount

    async def spy(self):
        seen["entered"] = True
        await original(self)
        seen["completed"] = True

    monkeypatch.setattr(m.LiteTUI, "on_unmount", spy)

    a = make_app()
    raised = ""
    try:
        async with a.run_test(size=(120, 40)) as pilot:
            body = await _open_sidebar(a, pilot, pauses)
            if body is not None:
                for n in range(30):
                    body.mount(Select([("x", 1)], id=f"t723-forced-{n}"))
    except Exception as error:  # noqa: BLE001 - the probe's job is to report
        # WHATEVER escapes the shutdown, including a class we have not seen yet.
        raised = f"{type(error).__name__}: {str(error)[:90]}"
    print(f"\n[faithful] pauses={pauses} raised={raised or 'none'} "
          f"on_unmount entered={seen.get('entered')} completed={seen.get('completed')}")
