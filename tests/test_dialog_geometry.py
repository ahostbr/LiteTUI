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
        await settle_until(pilot, lambda: panel.body.region.height > 0)

        assert panel.region.height >= 30, (
            f"{name}: panel is only {panel.region.height} rows of a 40-row "
            "screen — it is not carving full height"
        )
        assert panel.body.region.height >= panel.region.height - 2, (
            f"{name}: body is {panel.body.region.height} rows inside a "
            f"{panel.region.height}-row panel — it is FLOATING, not filling"
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
        await settle_until(pilot, lambda: _box(panel.body, selector).region.width > 0)

        box = _box(panel.body, selector)
        assert box.region.width <= panel.region.width, (
            f"{name}: content is {box.region.width} cols wide inside a "
            f"{panel.region.width}-col panel — THIS IS THE MID-WORD CLIP"
        )
        ctrl.resolve(None)


@pytest.mark.parametrize("name,factory,selector", DIALOGS, ids=[d[0] for d in DIALOGS])
@pytest.mark.parametrize("style", ["sidebar", "modal"])
@pytest.mark.asyncio
async def test_no_child_is_clipped_by_its_own_container(style, name, factory, selector) -> None:
    """🔴 THE GAP SILVERBOLT FOUND IN THIS VERY FILE, AN HOUR AFTER I WROTE IT.

    Everything above asserts the BOX and the PANEL: right size, right place,
    fits its host. **A container can be all three and still cut off its last
    child.** 72 tests passed over exactly that — his swap button's label row and
    bottom border did not paint, and nothing here could see it, because nothing
    compared a CHILD against its PARENT's content area.

    The cause was `max-height: 80%` on `#picker-box`, a percentage whose base
    was an auto-height parent sized by that same box. Textual settled on a
    content height of 10 for children needing 14 and simply cut the overflow.

    This walks every container in the dialog and requires each child's region to
    be inside its parent's `content_region`. It is the general form: it does not
    know about swap buttons, and it fails for ANY last-child clip in ANY of the
    four dialogs, in either host.
    """
    a = make_app()
    async with a.run_test(size=(100, 32)) as pilot:
        ctrl = DialogController(a, factory, style, "right")
        a.run_worker(ctrl.open(), name="dlg")
        if style == "sidebar":
            await settle_until(pilot, lambda: a.screen.query(SidePanel))
            root = a.screen.query_one(SidePanel).body
        else:
            await settle_until(pilot, lambda: _box(a.screen, selector).region.height > 0)
            root = _box(a.screen, selector)
        await settle_until(pilot, lambda: root.region.height > 0)
        for _ in range(4):
            await pilot.pause()

        offenders = []
        stack = [root]
        while stack:
            parent = stack.pop()
            pr = parent.content_region
            for child in parent.children:
                cr = child.region
                if cr.height and not (
                    cr.y >= pr.y and cr.y + cr.height <= pr.y + pr.height
                ):
                    offenders.append(
                        f"{type(child).__name__}(id={child.id}) rows "
                        f"{cr.y}..{cr.y + cr.height - 1} outside "
                        f"{type(parent).__name__} content rows "
                        f"{pr.y}..{pr.y + pr.height - 1}"
                    )
                stack.append(child)

        assert not offenders, (
            f"{name} [{style}]: CHILD CLIPPED BY ITS CONTAINER — the container "
            f"can still be the right size and in the right place:\n  "
            + "\n  ".join(offenders)
        )
        ctrl.resolve(None)


@pytest.mark.parametrize("h", [24, 32, 50])
@pytest.mark.asyncio
async def test_the_picker_cap_still_caps_a_long_list(h) -> None:
    """The cap was MOVED, so it has to be re-proved where it now lives.

    `max-height: 80%` exists so a long convo list cannot outgrow the screen.
    Deleting it would also have cleared the clipping bug — which is exactly why
    it was moved to `PickerBody` instead. A relocated guard that is never
    re-tested is a deleted guard with a comment on it.
    """
    a = make_app()
    long_rows = [(f"id-{i}", f"a fairly long conversation label number {i}")
                 for i in range(60)]
    async with a.run_test(size=(100, h)) as pilot:
        a.push_screen(PickerScreen("Select", long_rows))
        await settle_until(pilot, lambda: a.screen.query("#picker-box"))
        box = a.screen.query_one("#picker-box")
        await settle_until(pilot, lambda: box.region.height > 0)
        for _ in range(4):
            await pilot.pause()

        assert box.region.height <= h, (
            f"box is {box.region.height} rows on a {h}-row screen — the cap is "
            "not capping"
        )


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
        await settle_until(pilot, lambda: box.region.width > 0)

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
            pilot, lambda: _box(a.screen, selector).region.width > 0
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
