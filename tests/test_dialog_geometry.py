"""🔴 THE GATE THAT WAS MISSING ALL DAY: DOES THE DIALOG ACTUALLY FIT AND SIT RIGHT?

Four dialogs were converted to the sidebar host and 1337 tests passed. Ryan then
opened /model and saw three things no assertion in this repo could see:

  1. the modal rendered JAMMED TO THE LEFT instead of centred
  2. the sidebar panel was a FLOATING BOX in the top corner, not full height
  3. sidebar rows were CLIPPED MID-WORD -- "LiteTUI ea"

Every existing test asked BEHAVIOURAL questions -- did it resolve, did focus
land inside, did the swap keep the future pending -- and every one of them is
true of a dialog rendered in the wrong place at the wrong size. **Behaviour and
geometry are separate axes, and the suite only had one of them.**

The three causes were one mechanism: a body written for a ModalScreen carries
`height: auto` and a fixed `width`, which are correct under a screen that
centres its child and wrong inside a full-height, 60-column strip. Two of the
four bodies had already been given `max-width`; the two converted EARLIEST had
not, because the lesson was learned mid-conversion and never swept backwards.

⚠️ THESE ARE DELIBERATELY LOOSE. They are not pixel goldens -- a golden would
fail on every legitimate restyle and get deleted within a week. Each one
encodes only what was ACTUALLY BROKEN: fills the panel, fits the panel, sits in
the middle of the screen. A dialog can be redesigned freely and still pass.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui import ask_user_question as aq
from litetui import tool_policy
from litetui.picker import PickerBody, PickerScreen
from litetui.side_panel import DialogController, SidePanel
from litetui.tool_approval import ToolApprovalBody
from litetui.widgets import ConfirmStop, ConfirmStopBody

from _settle import settle_until

ROWS = [(f"id-{i}", f"LiteTUI entry number {i} with a long label") for i in range(12)]


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


def _decision():
    return tool_policy.PolicyDecision(
        action=tool_policy.CONFIRM,
        profile="interactive",
        capabilities=frozenset({"PROCESS_EXECUTION"}),
        reason="test",
    )


def _auq_body():
    st = aq._parse_questions({
        "questions": [
            {"label": "Q1", "question": "Pick one",
             "options": [{"title": "alpha"}, {"title": "beta"}]},
        ]
    })
    return aq.AskUserQuestionBody(st, threading.Event(), [])


# (name, body factory, selector for the visible BOX -- None means the body IS the box)
DIALOGS = [
    ("picker", lambda: PickerBody("Pick one", ROWS), "#picker-box"),
    ("confirm_stop", ConfirmStopBody, "#confirm-box"),
    ("tool_approval", lambda: ToolApprovalBody("bash", {"command": "echo hi"}, _decision()), None),
    ("ask_user_question", _auq_body, None),
]


def _box(panel_or_body, selector):
    return panel_or_body if selector is None else panel_or_body.query_one(selector)


@pytest.mark.parametrize("name,factory,selector", DIALOGS, ids=[d[0] for d in DIALOGS])
@pytest.mark.asyncio
async def test_sidebar_body_fills_the_panel_height(name, factory, selector) -> None:
    """SYMPTOM 2: the panel rendered as a floating box in the top corner.

    A body carrying `height: auto` from its modal origin is only as tall as its
    content. In a `height: 100%` panel that reads as a box floating at the top,
    which is what Ryan photographed.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await settle_until(pilot, lambda: a.screen.query(SidePanel))
        panel = a.screen.query_one(SidePanel)
        await settle_until(pilot, lambda: panel.body.outer_size.height > 0)

        assert panel.outer_size.height >= 30, (
            f"{name}: panel is only {panel.outer_size.height} rows of a 40-row "
            "screen — it is not carving full height"
        )
        assert panel.body.outer_size.height >= panel.outer_size.height - 2, (
            f"{name}: body is {panel.body.outer_size.height} rows inside a "
            f"{panel.outer_size.height}-row panel — it is FLOATING, not filling"
        )
        ctrl.resolve(None)


@pytest.mark.parametrize("name,factory,selector", DIALOGS, ids=[d[0] for d in DIALOGS])
@pytest.mark.asyncio
async def test_sidebar_content_is_not_wider_than_the_panel(name, factory, selector) -> None:
    """SYMPTOM 3: rows clipped mid-word — "LiteTUI ea".

    A flat `width: 78` box inside a strip that caps at 60 columns overflows, and
    a terminal does not scroll it into view; it just cuts. `max-width: 100%` is
    the whole fix and two of these four were missing it.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await settle_until(pilot, lambda: a.screen.query(SidePanel))
        panel = a.screen.query_one(SidePanel)
        await settle_until(pilot, lambda: _box(panel.body, selector).outer_size.width > 0)

        box = _box(panel.body, selector)
        assert box.outer_size.width <= panel.outer_size.width, (
            f"{name}: content is {box.outer_size.width} cols wide inside a "
            f"{panel.outer_size.width}-col panel — THIS IS THE MID-WORD CLIP"
        )
        ctrl.resolve(None)


@pytest.mark.parametrize(
    "name,screen_factory,selector",
    [
        ("picker", lambda: PickerScreen("Pick one", ROWS), "#picker-box"),
        ("confirm_stop", ConfirmStop, "#confirm-box"),
    ],
    ids=["picker", "confirm_stop"],
)
@pytest.mark.asyncio
async def test_THE_PATH_RYAN_ACTUALLY_SAW_is_centred(name, screen_factory, selector) -> None:
    """🔴 THE ORIGINAL SCREEN, PUSHED THE WAY THE APP PUSHES IT.

    The other modal test drives `_ModalHost`, which is the SWAP destination —
    not what /model opens. `present_dialog`'s modal branch deliberately pushes
    the ORIGINAL `PickerScreen` / `ConfirmStop`, so a fix verified only through
    `_ModalHost` would leave the path Ryan photographed untested and I would
    have "fixed" the screenshot without covering it.

    Same defect, different host: `align: center middle` on the screen centres
    the screen's child, and the conversion made that child the BODY rather than
    the box.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        a.push_screen(screen_factory())
        await settle_until(pilot, lambda: a.screen.query(selector))
        box = a.screen.query_one(selector)
        await settle_until(pilot, lambda: box.outer_size.width > 0)

        region = box.region
        left = region.x
        right = a.size.width - (region.x + region.width)
        assert abs(left - right) <= 2, (
            f"{name}: THE REAL MODAL PATH is off-centre — box at x={left}, "
            f"{right} cols to its right, width={region.width}"
        )


@pytest.mark.parametrize("name,factory,selector", DIALOGS, ids=[d[0] for d in DIALOGS])
@pytest.mark.asyncio
async def test_modal_box_is_horizontally_centred(name, factory, selector) -> None:
    """SYMPTOM 1: the modal sat hard against the left edge.

    `align: center middle` lives on the SCREEN and centres the screen's CHILD.
    The conversion put the body between the screen and the box, so a body with
    no width rule spanned the screen and the box inside it stayed left. Asserted
    as a symmetry of the side gaps, not a coordinate, so a width change is free.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl = DialogController(a, factory, "modal", "right")
        a.run_worker(ctrl.open(), name="dlg")
        await settle_until(
            pilot, lambda: _box(a.screen, selector).outer_size.width > 0
        )
        box = _box(a.screen, selector)
        region = box.region
        left = region.x
        right = a.size.width - (region.x + region.width)

        assert abs(left - right) <= 2, (
            f"{name}: box sits at x={left} with {right} cols to its right on a "
            f"{a.size.width}-col screen — NOT CENTRED (width={region.width})"
        )
        ctrl.resolve(None)
