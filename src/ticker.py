"""A number with − / + buttons and arrow keys. The spinner a terminal can have.

An Input in the middle so DIRECT TYPING still works -- a ticker that forces
sixty clicks to reach minute 59 is QoL in the wrong direction. Typing and
ticking are both first-class:

  - up/down arrows step the value (keys bubble up from the unhandled Input)
  - the − / + buttons step it with the mouse
  - typed digits are accepted live while they are in range, and the display
    is normalised (clamped, zero-padded) on blur rather than mid-keystroke:
    rewriting a field while someone is typing in it is hostile
  - stepping WRAPS (23 -> 0): a clock field with a hard stop at the top
    makes "one past midnight" a seven-step trip
"""

from __future__ import annotations

from textual import events, on
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Input


class NumberTicker(Horizontal):
    DEFAULT_CSS = """
    NumberTicker {
        width: auto;
        height: 3;
    }
    NumberTicker Button {
        min-width: 5;
        width: 5;
    }
    NumberTicker Input {
        width: 8;
    }
    """

    class Changed(Message):
        """The value changed, by any route: key, button, or accepted typing."""

        def __init__(self, ticker: "NumberTicker", value: int) -> None:
            self.ticker = ticker
            self.value = value
            super().__init__()

        @property
        def control(self) -> "NumberTicker":
            return self.ticker

    def __init__(self, value: int, lo: int, hi: int, *,
                 pad: int = 2, id: str | None = None) -> None:
        super().__init__(id=id)
        self.lo, self.hi, self.pad = lo, hi, pad
        self._value = max(lo, min(hi, int(value)))

    def compose(self):
        yield Button("−", classes="tick-down")
        yield Input(value=self._fmt(self._value), id=None, classes="tick-value")
        yield Button("+", classes="tick-up")

    # -- value ------------------------------------------------------------

    def _fmt(self, v: int) -> str:
        return str(v).zfill(self.pad)

    @property
    def value(self) -> int:
        """The current value, clamped. The INPUT may transiently hold text
        outside the range mid-typing; reads never see it."""
        try:
            typed = int(self.query_one(Input).value)
        except Exception:
            return self._value      # not mounted yet, or mid-typing garbage
        return max(self.lo, min(self.hi, typed))

    def set_value(self, v: int) -> None:
        """Programmatic set: clamp, repaint, announce."""
        self._value = max(self.lo, min(self.hi, int(v)))
        self.query_one(Input).value = self._fmt(self._value)
        self.post_message(self.Changed(self, self._value))

    def set_bounds(self, lo: int, hi: int) -> None:
        """Rebound (the N ticker means 1-59 for minutes, 1-23 for hours) and
        clamp the held value into the new range."""
        self.lo, self.hi = lo, hi
        if not (lo <= self._value <= hi):
            self.set_value(self._value)      # set_value clamps + announces

    def _step(self, delta: int) -> None:
        span = self.hi - self.lo + 1
        self._value = self.lo + (self._value - self.lo + delta) % span
        self.query_one(Input).value = self._fmt(self._value)
        self.post_message(self.Changed(self, self._value))

    # -- routes in --------------------------------------------------------

    @on(Button.Pressed, ".tick-up")
    def _up(self, event: Button.Pressed) -> None:
        event.stop()
        self._step(+1)

    @on(Button.Pressed, ".tick-down")
    def _down(self, event: Button.Pressed) -> None:
        event.stop()
        self._step(-1)

    def on_key(self, event: events.Key) -> None:
        # The Input does not bind up/down, so they bubble here.
        if event.key == "up":
            event.stop()
            self._step(+1)
        elif event.key == "down":
            event.stop()
            self._step(-1)

    @on(Input.Changed, ".tick-value")
    def _typed(self, event: Input.Changed) -> None:
        event.stop()
        try:
            typed = int(event.value)
        except ValueError:
            return                      # mid-typing; blur will normalise
        if self.lo <= typed <= self.hi and typed != self._value:
            self._value = typed
            self.post_message(self.Changed(self, typed))

    @on(Input.Blurred, ".tick-value")
    def _left(self, event: Input.Blurred) -> None:
        event.stop()
        # Normalise the DISPLAY only now: "7" -> "07", "99" -> the clamp.
        self.query_one(Input).value = self._fmt(self.value)
        if self.value != self._value:
            self._value = self.value
            self.post_message(self.Changed(self, self._value))
