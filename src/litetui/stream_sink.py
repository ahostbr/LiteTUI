"""Stream an answer into an AssistantMessage without repainting per token.

Every backend used to write the whole growing answer into the card's body and
ask for a scroll on EVERY token, then swap that plain text for rendered
Markdown when the turn ended. Measured in a pilot (500 deltas, 2026-09-25):
580 scroll_end calls and 575 body repaints, plus a reflow at the end. That was
the streaming flicker Ryan reported.

`StreamSink` is the one place a streamed answer is drawn:

* ``show(text)`` records the answer so far and schedules ONE render. Deltas
  that arrive before it fires fold into it.
* The render is Markdown from the first frame, through the same selectable
  ``AnswerBody.set_markdown`` the finished answer uses, so finishing changes
  nothing on screen.
* The interval adapts to the render's own cost. A full Markdown render costs
  ~17ms at 8k chars and ~48ms at 24k, so a fixed per-frame render would starve
  the event loop on long answers. Waiting ``COST_FACTOR`` times the last cost
  keeps rendering to a bounded share of the loop.
* ``finish(text)`` renders the final answer now; ``cancel()`` drops a pending
  render so an error message written into the body is never overwritten.
"""

from __future__ import annotations

import time

#: Never render faster than this: 30 fps is smooth for text.
MIN_INTERVAL = 1 / 30
#: Wait this many times the last render's cost before the next render.
COST_FACTOR = 3.0


class StreamSink:
    """Draws one card's streamed answer at most once per interval."""

    def __init__(self, app, card) -> None:
        self.app = app
        self.card = card
        self.text = ""
        self._shown: str | None = None
        self._timer = None
        self._render_cost = 0.0

    @property
    def pending(self) -> bool:
        return self._timer is not None

    def show(self, text: str) -> None:
        """The answer so far. Drawn on the next render, not now."""
        self.text = text
        if self._timer is None and text != self._shown:
            delay = max(MIN_INTERVAL, COST_FACTOR * self._render_cost)
            self._timer = self.app.set_timer(delay, self._render)

    def finish(self, text: str | None = None) -> None:
        """Draw the final answer immediately and keep it as the card's answer."""
        self.cancel()
        if text is not None:
            self.text = text
        self.card.set_answer(self.text)
        self._shown = self.text
        self.app._scroll_down()

    def cancel(self) -> None:
        """Drop a pending render. Whatever the body shows now stays."""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _render(self) -> None:
        self._timer = None
        if not self.card.is_attached or self.text == self._shown:
            return
        started = time.perf_counter()
        self.card.body.set_markdown(self.text)
        self._render_cost = time.perf_counter() - started
        self._shown = self.text
        self.app._scroll_down()
