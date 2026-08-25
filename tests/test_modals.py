"""Drive the modals through Textual's headless pilot — a real running app.

compose() cannot be called outside a live App (the container context managers
need one), so stubbing it proves nothing. run_test() gives an actual app with a
real DOM, so clicking and key presses are exercised rather than simulated.
"""

import re
import sys
import tempfile
from pathlib import Path

import pytest

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m  # noqa: E402
from litetui import paths  # noqa: E402
from litetui.picker import PickerScreen  # noqa: E402
from litetui.plugins.help_plugin import HelpScreen  # noqa: E402
from textual.widgets import OptionList  # noqa: E402

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-modal-"))

pytestmark = pytest.mark.asyncio


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model", "b-model", "c-model"]
    a.model_id = "b-model"
    # never touch the network in a test
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


async def test_model_opens_a_clickable_picker_and_keyboard_selects():
    a = make_app()
    async with a.run_test() as pilot:
        a._handle_command("/model")
        await pilot.pause()
        scr = a.screen
        assert isinstance(scr, PickerScreen), "a PickerScreen is on top"
        ol = scr.query_one(OptionList)
        assert ol.option_count == 3, "one row per model"
        assert ol.highlighted == 1, "current model is pre-highlighted"
        labels = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
        assert labels[1].startswith("▸"), "current row is marked with the arrow"
        assert not labels[0].startswith("▸"), "others are not marked"

        # move down and select with the keyboard
        await pilot.press("down", "enter")
        await pilot.pause()
        assert not isinstance(a.screen, PickerScreen), "picker dismissed after select"
        assert a.model_id == "c-model", "model switched to the highlighted row"


async def test_escape_cancels_without_changing_anything():
    b = make_app()
    async with b.run_test() as pilot:
        b._handle_command("/model")
        await pilot.pause()
        assert isinstance(b.screen, PickerScreen), "picker open"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(b.screen, PickerScreen), "dismissed"
        assert b.model_id == "b-model", "model unchanged"


async def test_clicking_a_row_selects_it():
    c = make_app()
    async with c.run_test() as pilot:
        c._handle_command("/model")
        await pilot.pause()
        ol = c.screen.query_one(OptionList)
        await pilot.click(ol, offset=(3, 0))  # first visible row
        await pilot.pause()
        assert not isinstance(c.screen, PickerScreen), "click dismissed the picker"
        assert c.model_id == "a-model", "click selected the clicked row"


async def test_help_is_a_scrollable_modal_with_a_close_button():
    d = make_app()
    async with d.run_test() as pilot:
        d._handle_command("/help")
        await pilot.pause()
        assert isinstance(d.screen, HelpScreen), "HelpScreen on top"
        body = d.screen.query_one("#help-body", m.Static)
        text = str(body.content)  # Textual 8: Static exposes .content
        assert "/compact" in text and "Esc" in text, "help text carried through"
        btn = d.screen.query_one("#help-close", m.Button)
        await pilot.click(btn)
        await pilot.pause()
        assert not isinstance(d.screen, HelpScreen), "Close button dismisses"


async def test_esc_stop_dialog_is_clickable_and_only_yes_stops():
    e = make_app()
    async with e.run_test() as pilot:
        e._chat_running = lambda: True
        e.action_stop_turn()
        await pilot.pause()
        assert isinstance(e.screen, m.ConfirmStop), "ConfirmStop on top"
        # THREE since T086: Yes / No, plus the swap control Ryan asked for
        # ("a button to swap back in forth DURING the dialog"). The count is
        # scaffolding for the click test below, not the subject of it.
        assert len(e.screen.query(m.Button)) == 3, "yes, no, and the swap control"
        await pilot.click(e.screen.query_one("#no", m.Button))
        await pilot.pause()
        assert not isinstance(e.screen, m.ConfirmStop), "No dismissed it"
        assert e._stop_requested is False, "No did NOT request a stop"

        e.action_stop_turn()
        await pilot.pause()
        await pilot.click(e.screen.query_one("#yes", m.Button))
        await pilot.pause()
        assert e._stop_requested is True, "Yes requested the stop"


async def test_typed_forms_still_work():
    f = make_app()
    async with f.run_test() as pilot:
        f._handle_command("/model 3")
        await pilot.pause()
        assert f.model_id == "c-model", "/model <n> still switches without a modal"
        assert not isinstance(f.screen, PickerScreen), "no modal was pushed"
        f._handle_command("/model a-model")
        await pilot.pause()
        assert f.model_id == "a-model", "/model <name> still switches"


async def test_connection_banner_still_offers_exactly_one_switch_hint():
    """The picker work must not duplicate or delete the boot banner's hint.

    🔴 THIS ASSERTION DRIFTED AND WENT RED WITHOUT THE BEHAVIOUR CHANGING. It
    pinned the literal "Use /model <number> to switch" and the banner was later
    reworded to "/model <n> to switch" — count() fell to 0 and reported a
    regression that did not exist.

    What it is actually protecting is a COUNT, not a phrasing: exactly one place
    tells the user how to switch. Matching the placeholder as \\w+ keeps the
    guard against duplication and deletion while surviving a rewording, which is
    the only part that was ever load-bearing.
    """
    src = (Path(__file__).resolve().parent.parent / "src" / "litetui" / "app.py").read_text(encoding="utf-8")
    hints = re.findall(r"/model <\w+> to switch", src)
    assert len(hints) == 1, f"expected exactly one switch hint, found {len(hints)}: {hints}"
