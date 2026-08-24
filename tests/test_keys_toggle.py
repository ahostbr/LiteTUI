"""`/keys` TOGGLES the help panel. A second `/keys` used to do nothing at all.

Ryan: "the /keys popup doesnt close after a second /keys is sent."

THE ROOT CAUSE IS IN TEXTUAL, NOT IN OUR LOGIC, and it is worth writing down
because the plugin looked correct. `App.action_show_help_panel` is IDEMPOTENT
BY DESIGN in textual 8.1.0 -- it queries for a HelpPanel and mounts one only on
NoMatches -- so it can never close what it opened. That is the right behaviour
for a "show" verb; it just means the verb cannot be a toggle.

AND THERE IS NO TOGGLE TO CALL. Measured on the installed 8.1.0, not recalled:
`[n for n in dir(App) if "help" in n.lower()]` -> HELP, action_help_quit,
action_hide_help_panel, action_show_help_panel. No action_toggle_help_panel.
So the toggle has to be ours.

The assertion below counts MOUNTED PANELS across three invocations rather than
checking a flag we maintain ourselves. A boolean of our own would have passed
against the broken version -- the bug was never in our bookkeeping, it was that
the panel outlived the command that was supposed to close it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.widgets import HelpPanel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui.plugins.misc import _cmd_keys


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


async def _panels(a, pilot) -> int:
    await pilot.pause()
    return len(a.screen.query(HelpPanel))


@pytest.mark.asyncio
async def test_keys_opens_then_closes_then_opens() -> None:
    """Three invocations, alternating. The middle one is the whole bug."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        seen = [await _panels(a, pilot)]
        for _ in range(3):
            _cmd_keys(a, "/keys", "")
            seen.append(await _panels(a, pilot))
    assert seen == [0, 1, 0, 1], (
        f"expected open/close/open, got {seen} — "
        "[0, 1, 1, 1] is the shipped bug: show is idempotent, so /keys only ever opened"
    )


@pytest.mark.asyncio
async def test_a_second_keys_actually_closes_it() -> None:
    """The user-visible claim, asserted on its own so a failure names itself."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        _cmd_keys(a, "/keys", "")
        assert await _panels(a, pilot) == 1, "the first /keys did not open the panel"
        _cmd_keys(a, "/keys", "")
        assert await _panels(a, pilot) == 0, "the second /keys left the panel open"
