"""The manual reasoning number, asked for from the /think picker's "number…" row (T1398).

Ryan: "allow both manual number and presets low med high and xhigh with associated to diffrent numbers".
Prefix (5c1db80f): the picker gets "a 'number…' row ... that asks for the value and goes through
refuse_number".

The dialog checks each answer with effort_scale.number_refusal, the rule /think <n> uses, and keeps
itself open with the reason until a number this model can take is typed, or Esc / "Cancel".
Same shape as context_choice.py (a host-agnostic body plus its modal).
"""
from __future__ import annotations

from functools import partial
from typing import Callable

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Static

from litetui.side_panel import SwapButton, close_dialog, present_dialog
from litetui.steady_input import HostedInput


class NumberBody(Widget):
    """The dialog content, host-agnostic (modal or sidebar). Exits through `close_dialog`."""

    DEFAULT_CSS = """
    NumberBody { width: 100%; height: 80%; align: center middle; layout: vertical; }
    NumberBody #number-box { width: 72; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    NumberBody #number-actions { height: auto; margin-top: 1; }
    NumberBody #number-error { color: $error; height: auto; }
    """

    def __init__(self, title: str, hint: str, value: str, check: Callable[[str], object]):
        super().__init__()
        self._title, self._hint, self._value, self._check = title, hint, value, check

    def compose(self) -> ComposeResult:
        with Vertical(id="number-box"):
            yield Static(self._title, id="number-title", markup=False)
            yield Static(self._hint, id="number-hint", markup=False)
            yield HostedInput(value=self._value, placeholder="a number, e.g. 0.1", id="number-input")
            yield Static("", id="number-error", markup=False)
            with Horizontal(id="number-actions"):
                yield Button("Set number", variant="primary", id="number-set")
                yield Button("Cancel", id="number-cancel")
            yield SwapButton()

    def on_mount(self) -> None:
        self.query_one("#number-input", Input).focus()

    def get_state(self) -> dict:
        return {"text": self.query_one("#number-input", Input).value}

    def set_state(self, state: dict) -> None:
        self.query_one("#number-input", Input).value = str(state.get("text", ""))

    def _set(self) -> None:
        try:
            number = self._check(self.query_one("#number-input", Input).value)
        except ValueError as exc:
            self.query_one("#number-error", Static).update(str(exc))
            return
        close_dialog(self, number)

    @on(Button.Pressed, "#number-set")
    def _pressed_set(self) -> None:
        self._set()

    @on(Input.Submitted, "#number-input")
    def _submitted(self) -> None:
        self._set()

    @on(Button.Pressed, "#number-cancel")
    def _pressed_cancel(self) -> None:
        close_dialog(self, None)


class NumberScreen(ModalScreen[object]):
    CSS = "NumberScreen { align: center middle; }"

    def __init__(self, title: str, hint: str, value: str, check: Callable[[str], object]):
        super().__init__()
        self._args = (title, hint, value, check)

    def compose(self) -> ComposeResult:
        yield NumberBody(*self._args)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


def ask_number(app, title: str, hint: str, value: str, check: Callable[[str], object],
               then: Callable[[object], None]) -> None:
    """Ask for a number; `then(number)` once `check(text)` accepts it, `then(None)` on Esc / Cancel.
    `check` returns the number or raises ValueError with the reason, which the dialog shows."""
    present_dialog(app, partial(NumberBody, title, hint, value, check),
                   partial(NumberScreen, title, hint, value, check), then, what="thinking number")
