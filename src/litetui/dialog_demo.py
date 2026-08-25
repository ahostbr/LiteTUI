"""THROWAWAY demo dialog for the T075 sidebar spike. Safe to delete with /test-sidebar.

This exists so Ryan judges the REAL thing rather than a placeholder. It carries
the shapes that decide whether a 60-column strip is usable:

  - a long UNWRAPPED MONOSPACE payload, because the tool approval preview is a
    JSON blob and that is the width-stress case
  - a TEXT INPUT, because the swap must not eat what you have already typed
  - three buttons returning distinct values, and Esc to cancel
  - the SWAP BUTTON, labelled for its DESTINATION rather than its current state

🔴 IT DOES NOT TOUCH `tool_approval.py`. The approval-shaped content below is a
COPY written for this demo. The real approval dialog is SilverBolt's file (T073)
and is explicitly out of scope for this spike.
"""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Input, Static

from litetui.side_panel import SwapButton, close_dialog, request_swap

# A realistic width-stress payload: unwrapped, monospace, and wider than 60
# columns on purpose. Whether this wraps, truncates or forces a horizontal
# scrollbar in the sidebar is one of the findings the spike exists to produce.
_DEMO_PAYLOAD = """{
  "tool": "write_file",
  "path": "C:/Projects/LiteTUI/src/litetui/plugins/scheduler_ui.py",
  "bytes": 34170,
  "sha256": "9f2c41ab77e0d3195c6ba8e40f7d21ce5813aa96b4d07ef2c1a5b98e6d3f04c7",
  "reason": "the model wants to rewrite the calendar screen in a single edit",
  "preview": "def open_day(self, day: date) -> None:  # one long unwrapped line to stress the panel width"
}"""


class DemoDialogBody(Widget):
    """A dialog body that works in EITHER host. No ModalScreen assumptions."""

    DEFAULT_CSS = """
    DemoDialogBody {
        height: auto;
        layout: vertical;
    }
    DemoDialogBody #demo-title { text-style: bold; padding: 0 0 1 0; }
    DemoDialogBody #demo-meta  { color: $text-muted; padding: 0 0 1 0; }
    DemoDialogBody #demo-scroll {
        height: 12;
        border: round $foreground 30%;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    DemoDialogBody #demo-note { margin: 0 0 1 0; }
    DemoDialogBody #demo-buttons { height: auto; align: center middle; }
    DemoDialogBody #demo-buttons Button { margin: 0 1 0 0; }
    DemoDialogBody #demo-swap { width: 100%; margin: 0 0 1 0; }
    """

    def compose(self) -> ComposeResult:
        yield Static("Run this tool?", id="demo-title")
        yield Static(
            "tool: write_file\nrisk: WORKSPACE_WRITE\nturn: 3 of 12",
            id="demo-meta",
        )
        with VerticalScroll(id="demo-scroll"):
            yield Static(_DEMO_PAYLOAD, markup=False, id="demo-payload")
        # The swap test types here. If a swap loses this, it would lose a
        # half-written reason on the real approval dialog too.
        yield Input(placeholder="note (type here, then swap)", id="demo-note")
        # THE SHARED CONTROL, keeping this id so the CSS above and the T075
        # relabel test keep binding to it. The label logic moved into
        # SwapButton — it is not duplicated here any more.
        yield SwapButton(id="demo-swap")
        with Horizontal(id="demo-buttons"):
            yield Button("Allow", variant="primary", id="demo-allow")
            yield Button("Always", variant="success", id="demo-always")
            yield Button("Deny", variant="error", id="demo-deny")

    def on_mount(self) -> None:
        # SwapButton labels itself on mount; this re-labels after a SWAP, when
        # the same body is re-created under the other host.
        self.query_one("#demo-swap", SwapButton).relabel()

    # ── state carry: data, not widgets ───────────────────────────────────────
    def get_state(self) -> dict:
        scroll = self.query_one("#demo-scroll", VerticalScroll)
        focused = self.screen.focused if self.screen is not None else None
        return {
            "note": self.query_one("#demo-note", Input).value,
            "scroll_y": scroll.scroll_offset.y,
            "focused_id": getattr(focused, "id", None),
        }

    def set_state(self, state: dict) -> None:
        note = self.query_one("#demo-note", Input)
        note.value = state.get("note", "")
        scroll = self.query_one("#demo-scroll", VerticalScroll)
        y = state.get("scroll_y", 0)
        if y:
            scroll.scroll_to(y=y, animate=False)
        fid = state.get("focused_id")
        if fid:
            try:
                self.query_one(f"#{fid}").focus()
            except Exception:
                # The control may not exist in the new host's tree. Losing the
                # focus target is survivable; losing the typed text is not, and
                # that is restored above regardless.
                pass

    # ── exits ────────────────────────────────────────────────────────────────
    @on(Button.Pressed, "#demo-swap")
    def _swap(self) -> None:
        request_swap(self)

    @on(Button.Pressed, "#demo-allow")
    def _allow(self) -> None:
        close_dialog(self, "allow")

    @on(Button.Pressed, "#demo-always")
    def _always(self) -> None:
        close_dialog(self, "always")

    @on(Button.Pressed, "#demo-deny")
    def _deny(self) -> None:
        close_dialog(self, "deny")
