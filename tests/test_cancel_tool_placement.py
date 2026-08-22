"""The cancel control is attached to the tool it kills, and it actually appears.

Ryan: "the cancel tool button is docked to the top left ... needs to be in next
to the tool timer." Then, after the first attempt: "still not seeing the cancel
button besides timer."

The first version of this file asserted PLACEMENT and stopped there. It passed
while the control was never created at all, because it mounted the ToolMessage
and THEN called _tool_begin -- the reverse of what _stream did. With the real
order, tool.parent was None, the mount was skipped by a `if parent is not None`
guard, and a green test certified a button nobody could see.

Two lessons are encoded below, deliberately:
  - Assert the OBSERVABLE property. "Correctly placed" is not what a user checks;
    "visible while a tool runs" is. A control that is mounted and never displayed
    is indistinguishable from an absent one.
  - Assert it under BOTH mount orders. The bug lived entirely in the caller's
    ordering, so an order-specific test can only ever catch half of it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as m
import ttyguard


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._jobs = []
    return a


class _FakeProc:
    """Stands in for a live cancellable subprocess."""

    def poll(self):
        return None


@pytest.fixture(autouse=True)
def _restore_cancellable():
    before = dict(ttyguard.CANCELLABLE)
    yield
    ttyguard.CANCELLABLE.update(before)


async def _await_control(a, pilot, tool, tries: int = 20):
    """Wait for the control to appear, rather than assuming a frame count.

    The announce-before-mount path defers via call_after_refresh, so the button
    lands a refresh LATER. Asserting after a fixed two pauses passed alone and
    failed inside the full suite, where the loop is busier -- a flaky test that
    only fails under load is worse than no test, because it teaches people to
    re-run instead of read.
    """
    for _ in range(tries):
        if a._cancel_buttons.get(tool) is not None:
            return a._cancel_buttons[tool]
        await pilot.pause()
    return None


def _assert_adjacent(a, tool) -> None:
    btn = a._cancel_buttons.get(tool)
    assert btn is not None, "no cancel control was created for a running tool"
    assert btn.parent is tool.parent, "the control is not parented to the tool's container"
    siblings = list(tool.parent.children)
    assert siblings.index(btn) == siblings.index(tool) + 1, (
        "the cancel control is not the immediate next sibling of its tool"
    )


@pytest.mark.asyncio
async def test_control_appears_beside_a_running_tool() -> None:
    """The order _stream uses: mount, then announce."""
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        assert not list(a.query(m.CancelToolButton)), "it is back on the header chrome"

        log = a.query_one("#chat-log")
        tool = m.ToolMessage("bash")
        log.mount(tool)
        a._tool_begin(tool)
        await pilot.pause()

        _assert_adjacent(a, tool)

        a._tool_end(tool)
        await pilot.pause()
        assert not list(a.query(m.CancelToolButton)), "it outlived the tool it belonged to"


@pytest.mark.asyncio
async def test_control_appears_when_announced_before_mounting() -> None:
    """The order that shipped broken: announce, THEN mount.

    tool.parent is None at _tool_begin time. That must defer, not skip -- the
    silently-skipped case was the one that always happened in the real app.
    """
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        log = a.query_one("#chat-log")
        tool = m.ToolMessage("bash")
        a._tool_begin(tool)          # parent is None here
        log.mount(tool)
        assert await _await_control(a, pilot, tool) is not None, (
            "the deferred retry never mounted the control"
        )

        _assert_adjacent(a, tool)


@pytest.mark.asyncio
async def test_control_becomes_visible_while_a_subprocess_is_live() -> None:
    """The property a user can actually check.

    Placement is worthless if `display: none` never lifts. Visibility is driven
    by the shared elapsed-repaint tick, and tracks the PROCESS, so a control is
    only offered when there is genuinely something to kill.
    """
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        log = a.query_one("#chat-log")
        tool = m.ToolMessage("bash")
        log.mount(tool)
        a._tool_begin(tool)
        await pilot.pause()
        btn = a._cancel_buttons[tool]

        # Nothing cancellable yet: offering the control would be a lie.
        ttyguard.CANCELLABLE["proc"] = None
        await asyncio.sleep(0.4)
        await pilot.pause()
        assert not btn.has_class("visible"), (
            "a cancel control is on screen with no subprocess to cancel"
        )

        # A live subprocess: now it must show.
        ttyguard.CANCELLABLE["proc"] = _FakeProc()
        await asyncio.sleep(0.4)
        await pilot.pause()
        assert btn.has_class("visible"), (
            "a subprocess is live and cancellable, but the control never appeared -- "
            "this is exactly what the user reported while the placement test was green"
        )


@pytest.mark.asyncio
async def test_one_control_per_running_tool() -> None:
    """Several tools can run at once, which is why the control carries a CLASS.

    An id must be unique in Textual, so the old id="cancel-tool" could not have
    survived a second concurrent tool.
    """
    a = make_app()
    async with a.run_test(size=(120, 30)) as pilot:
        log = a.query_one("#chat-log")
        tools = [m.ToolMessage("bash"), m.ToolMessage("read_file")]
        for t in tools:
            log.mount(t)
            a._tool_begin(t)
        await pilot.pause()

        assert len(list(a.query(m.CancelToolButton))) == 2
        for t in tools:
            _assert_adjacent(a, t)

        for t in tools:
            a._tool_end(t)
        await pilot.pause()
        assert not list(a.query(m.CancelToolButton))
