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

from litetui.side_panel import SwapButton, close_dialog, present_dialog

DEFAULT_HINT = "↑↓ move · Enter or click to select · Esc to cancel"


class PickerBody(Widget):
    """The list content, host-agnostic. Exits through `close_dialog`."""

    # 🔴 THIS BODY IS THE ONE NODE WITH A DEFINITE HEIGHT, AND EVERYTHING ELSE
    # IS RELATIVE TO IT. THAT IS THE WHOLE FIX.
    #
    # `#picker-box` used to be a DIRECT child of PickerScreen, so its
    # `max-height: 80%` resolved against the screen and the screen's
    # `align: center middle` centred it. The conversion inserted this body
    # between them and BOTH of those broke:
    #
    #   centring -> the screen now centres THIS body, not the box
    #   the cap  -> 80% now resolved against a parent that is `height: auto`,
    #               i.e. sized BY the very box it was meant to constrain
    #
    # A percentage whose base is derived from the child it constrains has no
    # fixed point. Textual settled on a content height of 10 for children
    # needing 14 and cut the overflow: SilverBolt's swap button lost its label
    # row and bottom border, while the identical widget painted correctly in the
    # sidebar — where the panel forces this body to a definite 100%.
    #
    # ⚠️ MOVING THE CAP BETWEEN LEVELS DOES NOT FIX IT. Measured, three ways:
    # on the box, the modal clips; on this body at `height: auto`, the body caps
    # itself and the BOX overflows it (fails on a 24-row screen); at `height:
    # 100%`, the sidebar clips instead. While every level is `auto` the cap only
    # relocates the clip.
    #
    # So: ONE definite height here, taken off the SCREEN, and the box caps at
    # 100% OF THIS. The box stays `height: auto`, so a three-row picker is still
    # a small dialog — making it fill would have been a visible UX change
    # smuggled in as a bug fix — and `align` does the centring that `width:
    # auto` had been standing in for.
    #
    # In a sidebar, SidePanel's DEFAULT_CSS overrides this to 100%, so the box
    # caps at the panel height instead.
    #
    # ⚠️ VERIFIED AT 100x32 ONLY — AND THAT SCOPE IS THE POINT, NOT A FOOTNOTE.
    # Measured on SilverBolt's tree with the swap button present: shortfall 0 in
    # both hosts. It does NOT hold at 24 rows, where this dialog still clips —
    # see the strict xfails in tests/test_dialog_geometry.py. That failure is a
    # DIFFERENT problem (nothing in the column shrinks or scrolls when it
    # exceeds the box) and it predates both this fix and the swap button.
    #
    # The earlier version of this comment said "verified in BOTH hosts" with no
    # viewport attached, which read as unconditional to anyone opening the file.
    # A code comment carries no timestamp and no instrument, so an unscoped
    # verification claim here is worse than the same claim in a message: the
    # message dies with its context, this reads as current forever.
    DEFAULT_CSS = """
    PickerBody { width: 100%; height: 80%; align: center middle; layout: vertical; }
    """

    def __init__(self, title: str, rows: list[tuple[str, str]],
                 current: str | None = None, hint: str = DEFAULT_HINT,
                 extra_factory=None, on_pick=None):
        super().__init__()
        self._title = title
        self._rows = rows          # (id, label)
        self._current = current
        self._hint = hint
        # Opt-in extra controls (e.g. the /load context-length row). `None` keeps
        # every other picker BYTE-IDENTICAL: nothing extra is yielded, `_selected`
        # takes the same one-line path. `extra_factory()` yields widgets built at
        # the CALL SITE, so this generic widget imports no load-specific widgets;
        # `on_pick(self)` runs at selection so the site can read those widgets'
        # state before the dialog closes.
        self._extra_factory = extra_factory
        self._on_pick = on_pick

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static(self._title, id="picker-title")
            yield OptionList(
                *[Option(label, id=oid) for oid, label in self._rows], id="picker-list"
            )
            if self._extra_factory is not None:
                yield from self._extra_factory()
            yield Static(self._hint, id="picker-hint")
            # Swap host without answering. Styles itself (SwapButton owns
            # its own DEFAULT_CSS), so this needs no rule in picker CSS.
            yield SwapButton()

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
        # Read any extra controls (their widgets vanish with the dialog) BEFORE
        # closing, so the call site captures their state for the chosen row.
        if self._on_pick is not None:
            self._on_pick(self)
        close_dialog(self, event.option.id)


class PickerScreen(ModalScreen[str | None]):
    """A clickable list modal. Returns the chosen option's id, or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, rows: list[tuple[str, str]], current: str | None = None,
                 hint: str = DEFAULT_HINT, extra_factory=None, on_pick=None):
        super().__init__()
        self._title = title
        self._rows = rows
        self._current = current
        self._hint = hint
        self._extra_factory = extra_factory
        self._on_pick = on_pick

    def compose(self) -> ComposeResult:
        yield PickerBody(self._title, self._rows, self._current, self._hint,
                         self._extra_factory, self._on_pick)

    def action_cancel(self) -> None:
        self.dismiss(None)


def pick(app, title: str, rows: list[tuple[str, str]], callback,
         current: str | None = None, hint: str = DEFAULT_HINT,
         extra_factory=None, on_pick=None) -> None:
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
        partial(PickerBody, title, rows, current, hint, extra_factory, on_pick),
        partial(PickerScreen, title, rows, current, hint, extra_factory, on_pick),
        callback,
        # The title is the only HUMAN name a picker has — `_dialog_name` would
        # answer "PickerScreen" for all of them, and a headless refusal that
        # cannot say WHICH picker it refused is barely better than silence.
        what=title,
    )
