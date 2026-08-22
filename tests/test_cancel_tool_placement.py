"""The cancel control is attached to the tool it kills, not to the header.

Ryan: "the cancel tool button is docked to the top left ... needs to be in next
to the tool timer."

This is a LAYOUT claim, and layout is exactly what a green unit suite does not
check -- 784 tests passed with the control pinned to the opposite corner of the
screen from the thing it acts on. So assert the two facts that carry the intent:

  1. It is NOT a fixture of the app chrome. Nothing is mounted at startup.
  2. When a tool starts it appears as the IMMEDIATE next sibling of that tool,
     which is where the elapsed timer is being drawn, and it leaves with it.

Both would fail against the previous version: the control was yielded from
compose() (so claim 1 fails at startup) and lived on the overlay layer with a
fixed offset, never parented to any tool (so claim 2 can never hold).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as m


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._jobs = []
    return a


@pytest.mark.asyncio
async def test_cancel_control_rides_its_tool() -> None:
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        # 1. Not part of the chrome.
        assert not list(a.query(m.CancelToolButton)), (
            "a cancel control exists before any tool is running -- it is back on "
            "the header, and it cannot say which tool it would kill"
        )

        log = a.query_one("#chat-log")
        tool = m.ToolMessage("bash")
        log.mount(tool)
        await pilot.pause()

        a._tool_begin(tool)
        await pilot.pause()

        buttons = list(a.query(m.CancelToolButton))
        assert len(buttons) == 1, f"expected exactly one cancel control, got {len(buttons)}"
        btn = buttons[0]

        # 2. Immediately after its own tool, in the same parent. "Next to the
        #    tool timer" is precisely this adjacency -- the timer is the last
        #    line ToolMessage paints while the call is still running.
        siblings = list(tool.parent.children)
        assert btn.parent is tool.parent, "the control is not parented to the tool's container"
        assert siblings.index(btn) == siblings.index(tool) + 1, (
            "the cancel control is not the immediate next sibling of its tool"
        )

        # It dies with the call: a control for a finished tool has nothing to kill.
        a._tool_end(tool)
        await pilot.pause()
        assert not list(a.query(m.CancelToolButton)), (
            "the cancel control outlived the tool it belonged to"
        )


@pytest.mark.asyncio
async def test_one_control_per_running_tool() -> None:
    """Several tools can run at once, which is why the control carries a CLASS.

    An id must be unique in Textual, so the old id="cancel-tool" could not have
    survived a second concurrent tool -- and query_one() would have raised on it.
    """
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        log = a.query_one("#chat-log")
        tools = [m.ToolMessage("bash"), m.ToolMessage("read_file")]
        for t in tools:
            log.mount(t)
        await pilot.pause()
        for t in tools:
            a._tool_begin(t)
        await pilot.pause()

        assert len(list(a.query(m.CancelToolButton))) == 2

        # Each one still sits against its own tool, not bunched at the end.
        siblings = list(log.children)
        for t in tools:
            btn = a._cancel_buttons[t]
            assert siblings.index(btn) == siblings.index(t) + 1

        for t in tools:
            a._tool_end(t)
        await pilot.pause()
        assert not list(a.query(m.CancelToolButton))
