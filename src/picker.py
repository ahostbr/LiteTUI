"""The shared list-picker modal — a core widget, deliberately generic.

Shared by /model and /resume so the two never drift into different
interactions — the pattern is identical, only the rows differ. It lives
outside any plugin because two plugins consume it; a shared widget owned
by either would make the other import its peer.
"""
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class PickerScreen(ModalScreen[str | None]):
    """A clickable list modal. Returns the chosen option's id, or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, rows: list[tuple[str, str]], current: str | None = None,
                 hint: str = "↑↓ move · Enter or click to select · Esc to cancel"):
        super().__init__()
        self._title = title
        self._rows = rows          # (id, label)
        self._current = current
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static(self._title, id="picker-title")
            yield OptionList(
                *[Option(label, id=oid) for oid, label in self._rows], id="picker-list"
            )
            yield Static(self._hint, id="picker-hint")

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        if self._current is not None:
            ids = [oid for oid, _ in self._rows]
            if self._current in ids:
                ol.highlighted = ids.index(self._current)
        ol.focus()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
