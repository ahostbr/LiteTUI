"""T082: AskUserQuestion in the sidebar — the one driven from a WORKER THREAD.

🔴 THE ANSWER DOES NOT COME BACK THROUGH THE HOST'S FUTURE, AND MUST NOT.
Every other converted dialog returns its value through `close_dialog`. This one
signals a `threading.Event` and appends to a `result_box`, because the caller is
`asyncio.to_thread`-ed tool code blocked on that Event. `close_dialog` only tears
the view down.

That makes two events look identical from the host's side while being opposite:

    SWAP          -> `done` is NEVER set. The thread keeps waiting. CORRECT —
                     nobody answered, and the dialog is rebuilt in the other host.
    APP TEARDOWN  -> `done` is never set either, so the polling loop's
                     `app.is_running` check is the ONLY thing that releases the
                     tool thread. Without it the thread blocks forever, and
                     `asyncio.to_thread` threads are not daemons.

⚠️ Which is why the polling loop is carried across UNCHANGED rather than
collapsed into an await on the dialog's result. A bare await cannot tell those
two apart.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui import ask_user_question as aq
from litetui.side_panel import DialogController, SidePanel, request_swap


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


def _states():
    return aq._parse_questions({
        "questions": [
            {"label": "Q1", "question": "Pick one",
             "options": [{"title": "alpha"}, {"title": "beta"}]},
            {"label": "Q2", "question": "And another",
             "options": [{"title": "gamma"}, {"title": "delta"}]},
        ]
    })


def _harness():
    st = _states()
    done = threading.Event()
    box: list[dict] = []
    return st, done, box, (lambda: aq.AskUserQuestionBody(st, done, box))


async def _settle(pilot, app, n=20):
    """Wait until the dialog is really there.

    🔴 THE OBVIOUS SECOND CLAUSE IS WORTHLESS IN A SIDEBAR. `screen.focused is
    not None` looks like "the dialog took focus" and is not: the panel shares a
    screen with the chat, so `focused` is ALREADY the message input before this
    dialog has laid out at all. A wait built on it exits on the first pause and
    is a tick count wearing a condition's clothes — which is precisely how a
    test passes alone, passes on one machine, and fails inside a full suite on
    another.

    So the condition is the body being present AND FOCUS BEING INSIDE IT.
    """
    for _ in range(n):
        await pilot.pause()
        found = app.screen.query(aq.AskUserQuestionBody)
        if not found:
            continue
        focused = app.screen.focused
        if focused is not None and focused in found[0].walk_children(with_self=True):
            return


async def _settle_clickable(pilot, app, selector, n=20):
    """Wait until `selector` is what the MOUSE would actually hit.

    A widget can exist, be queryable and still have a zero region, or be under
    something that has not finished laying out. `get_widget_at` on its own centre
    is the only check that asks the question the click asks.
    """
    for _ in range(n):
        await pilot.pause()
        hits = app.screen.query(selector)
        if not hits:
            continue
        w = hits[0]
        r = w.region
        if not r.width or not r.height:
            continue
        top = app.screen.get_widget_at(r.x + r.width // 2, r.y + r.height // 2)[0]
        if top is w or w in top.ancestors_with_self:
            return w
    return None


@pytest.mark.asyncio
async def test_sidebar_mounts_the_body_and_the_modal_path_still_pushes_a_screen():
    st, done, box, factory = _harness()
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "sidebar", "right")
        a.run_worker(ctrl.open(), name="auq")
        await _settle(pilot, a)
        assert len(a.screen.query(SidePanel)) == 1, "sidebar mounted no panel"
        assert not isinstance(a.screen, aq.AskUserQuestionScreen)
        ctrl.resolve(None)

    b = make_app()
    async with b.run_test(size=(120, 40)) as pilot:
        b.push_screen(aq.AskUserQuestionScreen(*_harness()[:3]))
        await pilot.pause()
        assert isinstance(b.screen, aq.AskUserQuestionScreen), "modal path changed"
        assert b.screen.query_one(aq.AskUserQuestionBody), "screen lost its body"


@pytest.mark.asyncio
async def test_A_SWAP_DOES_NOT_SET_DONE_so_the_tool_thread_keeps_waiting():
    """🔴 The catastrophic path for THIS dialog: a swap that sets `done` would
    return a half-filled answer to the model as though the human submitted it."""
    st, done, box, factory = _harness()
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "sidebar", "right")
        a.run_worker(ctrl.open(), name="auq")
        await _settle(pilot, a)

        st[0].selected.add(0)          # the human ticked something
        request_swap(a.screen.query_one(aq.AskUserQuestionBody))
        for _ in range(8):
            await pilot.pause()

        assert ctrl.style == "modal", "the swap did not change host"
        assert not done.is_set(), (
            "THE SWAP SET `done` — the tool thread would wake and report a "
            "half-filled answer nobody submitted"
        )
        assert box == [], f"a swap produced a result: {box}"
        assert ctrl.pending, "the swap resolved the host future"
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_the_answer_still_arrives_through_done_and_result_box_in_the_sidebar():
    """The positive half: submitting in the sidebar must wake the thread."""
    st, done, box, factory = _harness()
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "sidebar", "right")
        a.run_worker(ctrl.open(), name="auq")
        await _settle(pilot, a)

        st[0].selected.add(1)
        a.screen.query_one(aq.AskUserQuestionBody)._finish("submit")
        for _ in range(4):
            await pilot.pause()

    assert done.is_set(), "submitting in the sidebar never woke the tool thread"
    assert box and box[0]["action"] == "submit", f"result_box was {box}"
    assert box[0]["questions"][0]["selected"] == [1], "the tick did not carry"


@pytest.mark.asyncio
async def test_escape_cancels_in_the_sidebar_and_records_no_answers():
    st, done, box, factory = _harness()
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "sidebar", "right")
        a.run_worker(ctrl.open(), name="auq")
        await _settle(pilot, a)
        await pilot.press("escape")
        for _ in range(4):
            await pilot.pause()

    assert done.is_set(), "escape did not release the tool thread"
    assert box and box[0]["action"] == "cancelled", f"result_box was {box}"


@pytest.mark.asyncio
async def test_TAB_MOVES_BETWEEN_QUESTIONS_not_between_panel_controls():
    """The body's own tab binding must WIN over the panel's focus trap.

    SidePanel binds tab to cycle focus inside the dialog; this body binds tab to
    "next question". The body is closer to the focused widget, so it takes the
    key first — and that is the behaviour the dialog was designed around. If the
    panel's trap swallowed tab, this dialog's navigation would silently die.
    """
    st, done, box, factory = _harness()
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "sidebar", "right")
        a.run_worker(ctrl.open(), name="auq")
        await _settle(pilot, a)

        body = a.screen.query_one(aq.AskUserQuestionBody)
        assert body._active == 0
        await pilot.press("tab")
        await pilot.pause()
        assert body._active == 1, (
            "tab did not advance the question — the panel's focus trap ate the "
            "key that this dialog uses for navigation"
        )
        assert not done.is_set(), "tabbing answered the question"
        ctrl.resolve(None)


@pytest.mark.asyncio
async def test_a_real_MOUSE_CLICK_ticks_an_option_in_the_sidebar():
    """This dialog has three @on(events.Click) handlers — the modality that hid
    a whole defect on the approval dialog is the primary interaction here."""
    st, done, box, factory = _harness()
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "sidebar", "right")
        a.run_worker(ctrl.open(), name="auq")
        await _settle(pilot, a)

        # Wait for the row to be REACHABLE BY THE MOUSE, not merely present.
        # The previous version asserted hit-testing on the first frame where the
        # body existed, which is a race: this test was reported RED on another
        # machine at the same sha while passing 1322/1322 here.
        row = await _settle_clickable(pilot, a, ".auq-row")
        assert row is not None, (
            "no option row ever became mouse-reachable — either nothing rendered "
            "or something is permanently on top of it"
        )
        await pilot.click(".auq-row")
        for _ in range(4):
            await pilot.pause()
        assert st[0].selected, "a real click did not tick the option"
        ctrl.resolve(None)
