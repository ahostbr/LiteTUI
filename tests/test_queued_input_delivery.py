"""A queued message reaches the model mid-turn, at a round boundary.

Ryan: "if i queue a message the agent has to fully stop to get it ... it wont
deliver between tool calls or agent thinking / output."

That was structural, not a race. The only flush point was
on_worker_state_changed, which fires when a chat-group worker ENDS -- and
_stream is ONE worker running the entire agent loop. With tool_iterations at
100, a queued message could wait out a hundred rounds while the user watched the
agent carry on without it. Nothing was lost; it was simply unreachable until the
turn died.

Delivery now happens in the same slot the staged-image drain uses, and for the
same invariant that code already spells out: every tool_call_id must be answered
before a non-tool turn appears. That is what makes the round boundary the only
correct place -- mid-round would break the pairing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m

APP_SRC = (Path(__file__).resolve().parent.parent / "src" / "litetui" / "app.py").read_text(
    encoding="utf-8", errors="ignore"
)


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.jobs[:] = []
    return a


class _Bubble:
    def __init__(self) -> None:
        self.border_title = "You · queued"


@pytest.mark.asyncio
async def test_queued_messages_are_delivered_at_a_round_boundary() -> None:
    a = make_app()
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = len(a.conversation)
        b1, b2 = _Bubble(), _Bubble()
        a._pending_input = [
            {"content": "first queued", "text": "first queued", "bubble": b1},
            {"content": "second queued", "text": "second queued", "bubble": b2},
        ]

        # ONE per boundary, FIFO. Consecutive role:user turns are a chat-template
        # gamble -- qwen's template 500s on some shapes -- which is why the
        # turn-end flush has always sent exactly one. Draining the queue here
        # would rebuild that hazard at a new site.
        assert a._deliver_queued_input() is True
        assert [msg["content"] for msg in a.conversation[before:]] == ["first queued"]
        assert [i["content"] for i in a._pending_input] == ["second queued"], (
            "the whole queue was drained into one request"
        )
        assert b1.border_title == "You", "the delivered bubble still says queued"
        assert b2.border_title == "You · queued", "an undelivered bubble lost its badge"

        # The next round takes the next one.
        assert a._deliver_queued_input() is True
        assert [msg["content"] for msg in a.conversation[before:]] == [
            "first queued", "second queued",
        ]
        assert a._pending_input == []
        assert b2.border_title == "You"
        assert all(msg["role"] == "user" for msg in a.conversation[before:])


@pytest.mark.asyncio
async def test_nothing_queued_is_a_no_op() -> None:
    a = make_app()
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = len(a.conversation)
        assert a._deliver_queued_input() is False
        assert len(a.conversation) == before


@pytest.mark.asyncio
async def test_an_interrupt_is_not_delivered_here() -> None:
    """Queue and interrupt are two different verbs and must stay so.

    The interrupt path sets _stop_requested and inserts at the FRONT; it ends
    the turn and sends next. If this drain claimed it too, an interrupt would
    silently become a queue -- the turn would carry on with the message folded
    in, which is the opposite of what the user asked for.
    """
    a = make_app()
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = len(a.conversation)
        a._stop_requested = True
        a._pending_input = [{"content": "stop now", "text": "stop now", "bubble": _Bubble()}]

        assert a._deliver_queued_input() is False
        assert len(a.conversation) == before, "an interrupt was folded into the running turn"
        assert a._pending_input, "the interrupt was consumed instead of ending the turn"


def test_delivery_is_wired_into_the_agent_loop() -> None:
    """The drain must be CALLED from the loop, not merely defined.

    A method that exists and is never invoked is this repo's most-repeated
    defect, and the whole bug here was a delivery point that only ran at
    worker-exit. Asserted against the source because the alternative -- driving
    a full multi-round tool loop against a live model -- is not a unit test.
    """
    assert "def _deliver_queued_input" in APP_SRC
    call_sites = APP_SRC.count("await hook_host.queued_prompt(self)")
    assert call_sites >= 1, "_deliver_queued_input is defined but never called"

    # It has to sit at the round boundary, beside the staged-image drain, which
    # is the point that guarantees every tool_call_id has been answered.
    boundary = APP_SRC.index("if self._pending_tool_images:")
    call = APP_SRC.index("await hook_host.queued_prompt(self)")
    assert call < boundary, (
        "the queued-input drain is not at the round boundary the image drain uses"
    )
