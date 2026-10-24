"""The context-length question that follows choosing a model with /model.

Ryan 2026-10-24: "anytime you use slash model to pick a model, there's supposed to be a dialogue that asks you
to directly pitch the context you want for any model that you're loading, at the time on all BACKENDS ... it
per model selection override and it must effect that backends main settings."

So, after a pick:

  * the dialog asks for a number of tokens, pre-filled with what was last chosen for THAT model, else with the
    backend's own setting;
  * the answer is remembered per model (`model_context_overrides`, keyed "<backend>:<model>") AND written to the
    backend's main context setting (`MAIN_KEY`), because that setting is what the backend actually reads;
  * Esc / "Keep current" leaves every context setting alone and the pick itself still happens.

Backends with no context to set (the hosted providers) are not asked.
"""
from __future__ import annotations

import dataclasses
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

#: A typo guard, not a VRAM model (see plugins/model_switch.CTX_TYPO_CEILING for why no per-model maximum).
CTX_FLOOR = 512
CTX_CEILING = 1 << 20

#: backend -> the Settings field that backend reads its context length from.
MAIN_KEY = {
    "lmstudio": "default_context_length",
    "llamacpp": "default_context_length",
    "ninfer": "ninfer_max_context",
    "strata": "strata_max_context",
    "xolotl": "xolotl_max_context",
    "custom": "custom_context_length",
}

#: The engines LiteTUI starts itself: the number applies at the next engine start, not now.
APPLIES_ON_ENGINE_START = frozenset({"ninfer", "strata", "xolotl"})


def main_key(backend: str) -> str | None:
    return MAIN_KEY.get(backend)


def override_key(backend: str, model_key: str) -> str:
    return f"{backend}:{model_key}"


def remembered(settings, backend: str, model_key: str) -> int | None:
    value = (getattr(settings, "model_context_overrides", None) or {}).get(override_key(backend, model_key))
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def suggestion(settings, backend: str, model_key: str) -> int | None:
    """What the dialog opens with: this model's last choice, else the backend's current setting."""
    last = remembered(settings, backend, model_key)
    if last:
        return last
    key = main_key(backend)
    value = getattr(settings, key, None) if key else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def parse_context(text: str) -> int:
    """A plain number of tokens (commas allowed). No k/m suffixes: 100k is 100000 to some and 102400 to others."""
    raw = (text or "").strip().replace(",", "").replace("_", "").replace(" ", "")
    if not raw.isdigit():
        raise ValueError("Type a number of tokens, for example 100000 or 131072.")
    value = int(raw)
    if not CTX_FLOOR <= value <= CTX_CEILING:
        raise ValueError(f"{value:,} is not a plausible context length ({CTX_FLOOR:,} to {CTX_CEILING:,} tokens).")
    return value


def settings_after(settings, backend: str, model_key: str, tokens: int):
    """`settings` with the model's override recorded and the backend's main context setting set to it."""
    key = main_key(backend)
    if key is None:
        return settings
    overrides = dict(getattr(settings, "model_context_overrides", None) or {})
    overrides[override_key(backend, model_key)] = tokens
    return dataclasses.replace(settings, **{key: tokens, "model_context_overrides": overrides})


class ContextBody(Widget):
    """The dialog content, host-agnostic (modal or sidebar). Exits through `close_dialog`."""

    DEFAULT_CSS = """
    ContextBody { width: 100%; height: 80%; align: center middle; layout: vertical; }
    ContextBody #context-box { width: 64; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    ContextBody #context-actions { height: auto; margin-top: 1; }
    ContextBody #context-error { color: $error; height: auto; }
    """

    def __init__(self, title: str, hint: str, value: int | None):
        super().__init__()
        self._title = title
        self._hint = hint
        self._value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="context-box"):
            yield Static(self._title, id="context-title", markup=False)
            yield Static(self._hint, id="context-hint", markup=False)
            yield HostedInput(value=str(self._value) if self._value else "", placeholder="tokens, e.g. 100000",
                              id="context-input")
            yield Static("", id="context-error", markup=False)
            with Horizontal(id="context-actions"):
                yield Button("Set context", variant="primary", id="context-set")
                yield Button("Keep current", id="context-keep")
            yield SwapButton()

    def on_mount(self) -> None:
        self.query_one("#context-input", Input).focus()

    def get_state(self) -> dict:
        return {"text": self.query_one("#context-input", Input).value}

    def set_state(self, state: dict) -> None:
        self.query_one("#context-input", Input).value = str(state.get("text", ""))

    def _set(self) -> None:
        try:
            tokens = parse_context(self.query_one("#context-input", Input).value)
        except ValueError as exc:
            self.query_one("#context-error", Static).update(str(exc))
            return
        close_dialog(self, tokens)

    @on(Button.Pressed, "#context-set")
    def _pressed_set(self) -> None:
        self._set()

    @on(Input.Submitted, "#context-input")
    def _submitted(self) -> None:
        self._set()

    @on(Button.Pressed, "#context-keep")
    def _pressed_keep(self) -> None:
        close_dialog(self, None)


class ContextScreen(ModalScreen[int | None]):
    CSS = "ContextScreen { align: center middle; }"

    def __init__(self, title: str, hint: str, value: int | None):
        super().__init__()
        self._args = (title, hint, value)

    def compose(self) -> ComposeResult:
        yield ContextBody(*self._args)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


def ask(app, backend: str, model_key: str, then: Callable[[int | None], None], *, label: str | None = None) -> None:
    """Ask for the context length of `model_key` on `backend`, then call `then(tokens or None)` exactly once.

    `then(None)` right away when there is nothing to ask: a backend with no context setting (hosted providers)
    or a headless session. Esc and "Keep current" also answer None.
    """
    if main_key(backend) is None or getattr(app, "_rpc", False):
        then(None)
        return
    title = f"Context length for {label or model_key} (tokens)"
    last = remembered(app.settings, backend, model_key)
    hint = ("Applies on the next /engine start (stop a running engine first)." if backend in APPLIES_ON_ENGINE_START
            else "The model is loaded at this size.")
    if last:
        hint += f" Last used for this model: {last:,}."
    value = suggestion(app.settings, backend, model_key)
    present_dialog(app, partial(ContextBody, title, hint, value), partial(ContextScreen, title, hint, value),
                   then, what="context length")
