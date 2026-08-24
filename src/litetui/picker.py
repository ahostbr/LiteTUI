"""The shared list-picker — a core widget, deliberately generic.

Shared by /model and /resume so the two never drift into different
interactions — the pattern is identical, only the rows differ. It lives
outside any plugin because two plugins consume it; a shared widget owned
by either would make the other import its peer.

T080: the content moved into `PickerBody` so it can be hosted by a sidebar as
well as by a modal. `PickerScreen` still EXISTS and is still a real, distinct
`ModalScreen` — `app.py`'s centering rule selects it by class name
(`ConfirmStop, PickerScreen, HelpScreen, SettingsScreen { ... }`, asserted by
test_modal_centering), so replacing it with a generic host would have changed
what the modal IS while claiming only to add a sidebar.
"""
from functools import partial

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from litetui.side_panel import close_dialog, present_dialog

DEFAULT_HINT = "↑↓ move · Enter or click to select · Esc to cancel"


class PickerBody(Widget):
    """The list content, host-agnostic. Exits through `close_dialog`."""

    DEFAULT_CSS = """
    PickerBody { height: auto; layout: vertical; }
    """

    def __init__(self, title: str, rows: list[tuple[str, str]],
                 current: str | None = None, hint: str = DEFAULT_HINT):
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

    # ── state carry across a live host swap ──────────────────────────────────
    def get_state(self) -> dict:
        """The HIGHLIGHTED ROW is the state that matters.

        A picker rebuilt at row 0 after a swap does not merely look wrong — on a
        long convo list it silently relocates the selection the user had already
        arrowed to, and Enter then picks something else. Losing it is worse than
        losing typed text, because nothing on screen says it happened.
        """
        ol = self.query_one(OptionList)
        return {"highlighted": ol.highlighted}

    def set_state(self, state: dict) -> None:
        hi = state.get("highlighted")
        if hi is None:
            return
        ol = self.query_one(OptionList)
        if 0 <= hi < ol.option_count:
            ol.highlighted = hi
            ol.focus()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        close_dialog(self, event.option.id)


class PickerScreen(ModalScreen[str | None]):
    """A clickable list modal. Returns the chosen option's id, or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, rows: list[tuple[str, str]], current: str | None = None,
                 hint: str = DEFAULT_HINT):
        super().__init__()
        self._title = title
        self._rows = rows
        self._current = current
        self._hint = hint

    def compose(self) -> ComposeResult:
        yield PickerBody(self._title, self._rows, self._current, self._hint)

    def action_cancel(self) -> None:
        self.dismiss(None)


def pick(app, title: str, rows: list[tuple[str, str]], callback,
         current: str | None = None, hint: str = DEFAULT_HINT) -> None:
    """Open the picker in whichever host the setting names. ONE call per site.

    Both factories are built from ONE set of arguments here rather than at each
    of the five call sites. Passing `title`/`rows`/`current` twice per site — once
    for the body and once for the modal — is five chances for the two to drift,
    and a picker whose sidebar and modal show different rows would be a very
    confusing bug to read.

    ⚠️ Esc AND a swap both produce "no answer yet", and they must not look alike:
    Esc resolves the dialog with None and the callback FIRES with None; a swap
    does not resolve at all and the callback is NOT called. That distinction is
    the controller's, not this function's, and there is a test for it.
    """
    present_dialog(
        app,
        partial(PickerBody, title, rows, current, hint),
        partial(PickerScreen, title, rows, current, hint),
        callback,
    )
