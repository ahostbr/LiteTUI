"""Modal/settings inputs reuse the app's hosted-seat idle-output policy."""
from __future__ import annotations

from textual.widgets import Input


class HostedInput(Input):
    """A stock Input with a steady cursor in hosted LiteTUI seats (T0081).

    Decide at mount, when the owning app is available. Keep Textual's events,
    validation, selection and focus behavior, and leave plain-terminal inputs
    untouched. In particular, this is not the chat prompt's history/picker UI.
    """

    def on_mount(self) -> None:
        hosted_seat = getattr(self.app, "_hosted_seat", None)
        if callable(hosted_seat) and hosted_seat():
            self.cursor_blink = False
