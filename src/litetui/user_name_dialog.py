"""One-time prompt for the name used in the model's system prompt."""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class UserNameScreen(ModalScreen[str | None]):
    """Ask once; Submit with an empty field deliberately chooses no name."""

    CSS = """
    UserNameScreen { align: center middle; }
    #user-name-box { width: 60; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #user-name-actions { height: auto; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="user-name-box"):
            yield Label("What should LiteTUI call you?")
            yield Static("This is used only in the assistant's system prompt. Leave it blank for no name.")
            yield Input(placeholder="Your name (optional)", id="user-name-input")
            yield Button("Save", variant="primary", id="user-name-save", classes="user-name-actions")

    def on_mount(self) -> None:
        self.query_one("#user-name-input", Input).focus()

    @on(Button.Pressed, "#user-name-save")
    def save_name(self) -> None:
        value = self.query_one("#user-name-input", Input).value.strip()
        if value.startswith("/"):
            self.query_one("#user-name-input", Input).value = ""
            return
        self.dismiss(value)

    @on(Input.Submitted, "#user-name-input")
    def submit_name(self) -> None:
        self.save_name()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
