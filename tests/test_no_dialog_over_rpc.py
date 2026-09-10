"""T572 — no keyboard dialog is opened over `--rpc`, by ANY route.

T558-A measured the failure and fixed ONE door: `/think` with no argument.
The mechanism was never specific to `/think`. A dialog pushed headless waits on
a keyboard that is not attached, and the caller — a model, not a person — hangs
holding a turn that can never finish.

🔴 SO THE GUARD GOES IN THE SHARED DOORS, NOT IN THE COMMANDS. `present_dialog`
and `open_dialog` are what every picker and every panel goes through
(`/model`, `/convo`, `/skills`, `/tools`, `/mcp`, `/loop`, both T570 panels,
the engine and reasoning pickers). Guarding them one command at a time is a
list that is wrong the day someone adds the twelfth — and wrong SILENTLY, since
the symptom is a hang in someone else's process. The bare `/loop` this card
names is the instance; the doors are the fix.

⚠️ `show_dialog` IS DELIBERATELY NOT GUARDED HERE, and that is a finding rather
than an omission — see the arm at the bottom. Its awaiting caller is tool
approval, where "no answer" means DENY AND STOP THE TURN. A blanket refusal
there would convert a hang into a silent policy change, which is worse: the
hang is at least visible.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _settle import settle_until  # noqa: E402

from litetui import app as m  # noqa: E402
from litetui import (
    goal_loop,  # noqa: E402
    side_panel,  # noqa: E402
)
from litetui.picker import PickerScreen, pick  # noqa: E402


def make_app(rpc: bool = False):
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.bg_tasks = {}
    a._rpc = rpc
    a.said = []
    a._system = a.said.append
    a.system_message = a.said.append
    return a


ROWS = [("a", "the first"), ("b", "the second")]


# ── the doors ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["modal", "sidebar"])
async def test_a_picker_never_reaches_the_screen_stack_over_rpc(style) -> None:
    """BOTH styles, because `present_dialog` forks on the setting and only the
    modal half pushes directly — a guard on one branch leaves the other open,
    and which branch you get depends on a setting nobody sets for a headless
    child."""
    a = make_app(rpc=True)
    async with a.run_test(size=(100, 30)) as pilot:
        a.settings.dialog_style = style
        answered: list = []
        pick(a, "Pick one", ROWS, answered.append)
        for _ in range(6):
            await pilot.pause()

        assert not isinstance(a.screen, PickerScreen), "a picker opened over rpc"
        assert not a.screen.query(side_panel.SidePanel).filter(".open"), (
            "a sidebar dialog opened over rpc"
        )
        assert answered == [], (
            "the callback fired with a fabricated answer — a swap does not "
            "answer, and neither does a refusal"
        )


@pytest.mark.asyncio
async def test_the_refusal_says_what_to_do_instead() -> None:
    """A model that gets silence cannot tell a refusal from a tool that did
    nothing. The line has to name the transport AND a way forward."""
    a = make_app(rpc=True)
    async with a.run_test(size=(100, 30)) as pilot:
        pick(a, "Thinking level", ROWS, lambda r: None)
        await pilot.pause()
        assert a.said, "the refusal was silent"
        said = " ".join(a.said).lower()
        assert "rpc" in said or "headless" in said
        assert "thinking level" in said, "the refusal does not say WHICH dialog"


@pytest.mark.asyncio
async def test_open_dialog_is_guarded_too_not_just_the_picker() -> None:
    """`/tools`, `/mcp`, `/loop` and both T570 panels call `open_dialog`
    directly, never `present_dialog`. A guard on the picker alone would leave
    every panel hanging.

    ⚠️ THE ASSERTION IS ON THE SCREEN STACK, and the first version of this arm
    was not — it asked whether the BODY had composed, and passed against the
    UNFIXED code. Measured: over rpc `_ModalHost` was on the stack and the body
    query returned 0 anyway, so "no body" was true while a modal nobody can
    dismiss sat on the stack. The host being pushed IS the hang; the body is a
    detail of how far it got.
    """
    from litetui.task_screens import BackgroundProcessesBody

    tui = make_app(rpc=False)
    async with tui.run_test(size=(100, 30)) as pilot:
        base = len(tui.screen_stack)
        side_panel.open_dialog(tui, BackgroundProcessesBody)
        assert await settle_until(pilot, lambda: len(tui.screen_stack) > base), (
            "the panel does not open even in the TUI — the arm measures nothing"
        )

    a = make_app(rpc=True)
    async with a.run_test(size=(100, 30)) as pilot:
        base = len(a.screen_stack)
        side_panel.open_dialog(a, BackgroundProcessesBody)
        for _ in range(25):
            await pilot.pause()
        assert len(a.screen_stack) == base, (
            f"a dialog host was pushed over rpc: "
            f"{[type(s).__name__ for s in a.screen_stack]}"
        )


# ── the negative control ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_TUI_still_opens_everything() -> None:
    """Without this, a guard that refused ALWAYS would pass every arm above and
    the product would have no dialogs at all."""
    a = make_app(rpc=False)
    async with a.run_test(size=(100, 30)) as pilot:
        a.settings.dialog_style = "modal"
        pick(a, "Pick one", ROWS, lambda r: None)
        for _ in range(6):
            await pilot.pause()
        assert isinstance(a.screen, PickerScreen), "the picker stopped opening in the TUI"
        # NOT `a.said == []`: the app posts its boot banner through the same
        # channel, so an empty-list assertion here fails on the banner and says
        # "the TUI path printed a refusal" about a splash screen.
        assert not any("rpc" in line.lower() for line in a.said), (
            "the TUI path printed a headless refusal"
        )


# ── the instance the card names ────────────────────────────────────────────

def test_bare_loop_prints_the_list_over_rpc_instead_of_opening_a_panel():
    """The specific fallback beats the generic refusal where one exists: `/loop`
    has printed a text list under `/loop list` since goal_loop.py was written,
    so the headless branch answers with THAT rather than "not available here".
    Same shape as T558-A's `/think`."""
    a = make_app(rpc=True)
    opened: list = []
    real = side_panel.open_dialog
    side_panel.open_dialog = lambda *args, **kw: opened.append(args)
    try:
        goal_loop.loop_command(a, "")
    finally:
        side_panel.open_dialog = real

    assert opened == [], "bare /loop opened a panel over rpc"
    assert a.said, "bare /loop answered with nothing at all"
    assert "loop" in " ".join(a.said).lower()


def test_bare_loop_still_opens_the_panel_in_the_TUI(monkeypatch):
    a = make_app(rpc=False)
    opened: list = []
    monkeypatch.setattr(side_panel, "open_dialog", lambda *args, **kw: opened.append(args))
    goal_loop.loop_command(a, "")
    assert len(opened) == 1, "the /loop panel stopped opening in the TUI"


def test_loop_list_is_unchanged_on_both_transports():
    """The text verb is what the headless branch reuses; if it drifted, the
    fallback would drift with it."""
    for rpc in (True, False):
        a = make_app(rpc=rpc)
        goal_loop.loop_command(a, "list")
        assert a.said, f"/loop list said nothing (rpc={rpc})"


# ── the door that is NOT guarded, and why ──────────────────────────────────

def test_show_dialog_is_left_alone_on_purpose():
    """🔴 A GUARD HERE WOULD BE A SILENT POLICY CHANGE, NOT A FIX.

    `show_dialog` is awaited by ONE caller that blocks a turn on the answer:
    tool approval (app.py, the `tool_policy.CONFIRM` branch). Its contract is
    `not answer` -> DENY, and DENY STOPS THE TURN. Refusing over rpc would turn
    every confirm-profile tool call in a headless child into a denial that ends
    the turn, with no dialog and no explanation — a hang at least announces
    itself by never finishing.

    This arm exists so the omission is a DECISION with a reason attached rather
    than a door somebody notices is missing and closes. What a headless child
    actually needs is approval routed to its HOST over the wire; that is a
    feature, not a guard, and it is not built.
    """
    import inspect

    src = inspect.getsource(side_panel.show_dialog)
    assert "_rpc" not in src, (
        "show_dialog grew an rpc guard — read this arm's docstring first: it "
        "silently converts every headless tool-approval prompt into DENY, which "
        "stops the turn"
    )
