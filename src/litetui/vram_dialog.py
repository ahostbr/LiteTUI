"""The second-instance VRAM confirmation (T690).

Separate from `second_instance.py` on purpose: THE RULE is pure and its arms run
without a terminal, and this is the part that needs Textual. Splitting them is
what lets every combination of the rule be asserted cheaply while the drawing
keeps one arm.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Static

from litetui.side_panel import close_dialog

#: What `show_dialog` returns when the human agrees. Anything falsy — a cancel,
#: a dismissed screen, a closed sidebar — is a NO, which is the safe default for
#: a question about spending VRAM.
LOAD = "load"


class VramWarningBody(Widget):
    """Works in either host; no ModalScreen assumptions (the sidebar can host
    this too, like every other dialog in this app)."""

    DEFAULT_CSS = """
    VramWarningBody { height: auto; layout: vertical; }
    VramWarningBody #vram-title { text-style: bold; padding: 0 0 1 0; }
    VramWarningBody #vram-text { padding: 0 0 1 0; }
    VramWarningBody #vram-buttons { height: auto; align: center middle; }
    VramWarningBody #vram-buttons Button { margin: 0 1 0 0; }
    """

    def __init__(self, text: str, load_label: str = "Load") -> None:
        super().__init__()
        self._text = text
        self._load_label = load_label

    def compose(self) -> ComposeResult:
        yield Static("Another instance is running", id="vram-title")
        # `markup=False`: a model id can contain brackets, and Textual would
        # read them as markup — a slug is data, not a style.
        yield Static(self._text, markup=False, id="vram-text")
        with Horizontal(id="vram-buttons"):
            yield Button(self._load_label, variant="warning", id="vram-load")
            yield Button("Cancel", variant="primary", id="vram-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        close_dialog(self, LOAD if event.button.id == "vram-load" else None)

    def get_state(self) -> dict:
        """Nothing to carry across a host swap — the text is passed in and the
        buttons hold no state. Declared rather than omitted so a reader does not
        wonder whether it was forgotten."""
        return {}

    def set_state(self, state: dict) -> None:
        return None


class VramWarningScreen(ModalScreen):
    """The modal host, so the body keeps its own DEFAULT_CSS (see show_dialog:
    routing a styled body through `_ModalHost` renders it unstyled)."""

    def __init__(self, text: str, load_label: str = "Load") -> None:
        super().__init__()
        self._text = text
        self._load_label = load_label

    def compose(self) -> ComposeResult:
        yield VramWarningBody(self._text, self._load_label)
