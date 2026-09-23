"""The `/mcp` dialog — the surface over the lifecycle verbs.

Ryan, 2026-09-03: "theres no /mcp managment system yet ... there needs to be a
full cmd and UI for adding connecting disconnecting reconnecting and removing
mcps etc. full managment options suite."

🔴 EVERY BUTTON IS WIRED TO THE MANAGER, NOT TO ITSELF. The failure this file
has to avoid is a dialog whose rows look right and change nothing — the exact
defect the `/tools` list was rewritten to remove. So each action calls the same
`MCPManager` verb the command calls, then `app.rebuild_mcp_dispatch()`, then
re-renders from `describe()`. Nothing here caches a server's state: the rows
are rebuilt from the manager after every action, so the screen cannot drift
from what is actually running.

⚠️ ROWS ARE ADDRESSED BY INDEX, NOT BY NAME. Textual ids must be identifiers,
and an MCP server name is arbitrary text — `@scope/pkg` is a legal name and an
illegal id. Encoding the name into the id would work for every server anyone
happened to test and break on the first scoped one.
"""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Input, Static

from litetui.friendly_errors import present
from litetui.mcp_client import WRITE_CONFIG_NAME
from litetui.side_panel import SwapButton, close_dialog

#: What a state means, in the words a user needs rather than the enum's.
STATE_NOTE = {
    "connected": "serving tools to the model",
    "stopped": "declared, not running",
    "disabled": '"disabled": true in the config',
    "failed": "tried to start and could not",
    "orphan": "running, but no longer in the config — a restart will not bring it back",
}

#: Which actions make sense for a row in each state. A button that cannot work
#: is not rendered, rather than rendered and then refusing: the /tools doctrine
#: — a control that lies is worse than one that is visibly absent.
ACTIONS_FOR = {
    "connected": ("disconnect", "reconnect", "remove"),
    "stopped": ("connect", "remove"),
    "disabled": ("connect", "remove"),
    "failed": ("connect", "remove"),
    "orphan": ("disconnect",),
}


class MCPListBody(Widget):
    """A dialog body that works in either host. No ModalScreen assumptions."""

    DEFAULT_CSS = """
    MCPListBody { height: auto; layout: vertical; }
    MCPListBody #mcp-title { text-style: bold; padding: 0 0 1 0; }
    MCPListBody #mcp-note { color: $text-muted; padding: 0 0 1 0; }
    MCPListBody #mcp-scroll { height: 16; padding: 0 1; }
    MCPListBody .mcp-name { text-style: bold; padding: 1 0 0 0; }
    MCPListBody .mcp-detail { color: $text-muted; padding: 0 0 0 2; }
    MCPListBody .mcp-error { color: $error; padding: 0 0 0 2; }
    MCPListBody .mcp-actions { height: auto; padding: 0 0 1 2; }
    MCPListBody .mcp-actions Button { margin: 0 1 0 0; min-width: 12; }
    MCPListBody #mcp-add { height: auto; padding: 1 0 0 0;
                           border-top: solid $foreground 20%; }
    MCPListBody #mcp-add Input { margin: 0 1 0 0; }
    MCPListBody #mcp-add-name { width: 24; }
    MCPListBody #mcp-buttons { height: auto; align: center middle; padding: 1 0 0 0; }
    MCPListBody #mcp-buttons Button { margin: 0 1 0 0; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict] = []
        self._scroll_y = 0

    # ── composition ──────────────────────────────────────────────────────
    def _row_widgets(self):
        """Yield the widgets for the current server list.

        Kept separate from compose() because it runs again after every action —
        re-rendering from describe() is what keeps the screen honest.
        """
        app = self.app
        self._rows = app.mcp.describe()
        if not self._rows:
            yield Static(
                "No MCP servers declared yet. Add one below — a URL for an HTTP "
                "endpoint, or a command to spawn.",
                classes="mcp-detail",
                markup=False,
            )
            return
        for i, row in enumerate(self._rows):
            head = f"{row['name']}  ·  {row['state']}"
            if row["state"] == "connected":
                head += f"  ·  {row['tools']} tools"
            yield Static(head, classes="mcp-name", markup=False)
            yield Static(
                f"{row['transport']}  {row['target']}".strip(),
                classes="mcp-detail",
                markup=False,
            )
            note = STATE_NOTE.get(row["state"], "")
            if note:
                yield Static(note, classes="mcp-detail", markup=False)
            if row["error"]:
                yield Static(present(row["error"], app.settings.error_message_style, surface=f"mcp:{row['name']}"),
                             classes="mcp-error", markup=False)
            # 🔴 CONSTRUCTED, NOT `with Horizontal(...)`. The context-manager
            # form appends to `app._compose_stacks`, which only exists while
            # compose() is running — and this generator runs AGAIN on every
            # re-render, where that stack is empty and the `with` raises
            # IndexError deep inside Textual. Passing children to the
            # constructor is the form that works in both callers.
            yield Horizontal(
                *(
                    Button(verb.capitalize(), id=f"mcp-act-{i}-{verb}")
                    for verb in ACTIONS_FOR.get(row["state"], ())
                ),
                classes="mcp-actions",
            )

    def compose(self) -> ComposeResult:
        yield Static("MCP servers", id="mcp-title")
        yield Static(
            "Tool servers the model can call. Changes apply at once — there is "
            f"no save. Adding and removing writes {WRITE_CONFIG_NAME}; "
            "disconnecting is for this session only.",
            id="mcp-note",
            markup=False,
        )
        with VerticalScroll(id="mcp-scroll"):
            yield from self._row_widgets()
        with Horizontal(id="mcp-add"):
            yield Input(placeholder="name", id="mcp-add-name")
            yield Input(placeholder="https://host/mcp   or   command arg arg", id="mcp-add-target")
            yield Button("Add", variant="primary", id="mcp-add-go")
        yield Static("", id="mcp-status", markup=False)
        with Horizontal(id="mcp-buttons"):
            yield Button("Close", variant="primary", id="mcp-close")
            yield SwapButton(classes="inline")

    # ── the swap contract ────────────────────────────────────────────────
    def get_state(self) -> dict:
        """Scroll position and whatever is half-typed in the add form.

        The add form is the one place this dialog holds state the manager does
        not, so a host swap has to carry it — losing a half-typed command line
        because the user pressed "Sidebar / popup" would be the kind of small
        betrayal that stops people using the swap at all.
        """
        state = {}
        try:
            state["scroll_y"] = self.query_one("#mcp-scroll", VerticalScroll).scroll_offset.y
            state["name"] = self.query_one("#mcp-add-name", Input).value
            state["target"] = self.query_one("#mcp-add-target", Input).value
        except Exception:
            pass
        return state

    def set_state(self, state: dict) -> None:
        self._scroll_y = int(state.get("scroll_y") or 0)
        self._pending = (state.get("name") or "", state.get("target") or "")

    def on_mount(self) -> None:
        name, target = getattr(self, "_pending", ("", ""))
        try:
            if name:
                self.query_one("#mcp-add-name", Input).value = name
            if target:
                self.query_one("#mcp-add-target", Input).value = target
            if self._scroll_y:
                self.query_one("#mcp-scroll", VerticalScroll).scroll_to(
                    y=self._scroll_y, animate=False
                )
        except Exception:
            pass

    # ── actions ──────────────────────────────────────────────────────────
    def _say(self, text: str) -> None:
        try:
            self.query_one("#mcp-status", Static).update(
                present(text, self.app.settings.error_message_style, surface="mcp"))
        except Exception:
            pass

    async def _rerender(self) -> None:
        """Rebuild the rows from the manager. The screen never caches state."""
        try:
            scroll = self.query_one("#mcp-scroll", VerticalScroll)
        except Exception:
            return
        await scroll.remove_children()
        await scroll.mount(*self._row_widgets())

    @on(Button.Pressed, ".mcp-actions Button")
    async def _row_action(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        try:
            _, _, idx, verb = bid.split("-", 3)
            row = self._rows[int(idx)]
        except (ValueError, IndexError):
            return
        name = row["name"]
        app = self.app
        if verb == "connect":
            err = app.mcp.connect(name)
            msg = f"could not connect {name}: {err}" if err else f"connected {name}"
        elif verb == "disconnect":
            was = app.mcp.disconnect(name)
            msg = f"disconnected {name}" if was else f"{name} was not running"
        elif verb == "reconnect":
            app.mcp.reload_configs()
            err = app.mcp.reconnect(name)
            msg = f"could not reconnect {name}: {err}" if err else f"reconnected {name}"
        elif verb == "remove":
            err = app.mcp.remove(name)
            msg = f"could not remove {name}: {err}" if err else f"removed {name}"
        else:
            return
        app.rebuild_mcp_dispatch()
        self._say(msg)
        await self._rerender()

    @on(Button.Pressed, "#mcp-add-go")
    async def _add(self, event: Button.Pressed) -> None:
        event.stop()
        from litetui.plugins.mcp_manage import _entry_from_words

        name = self.query_one("#mcp-add-name", Input).value.strip()
        target = self.query_one("#mcp-add-target", Input).value.strip()
        if not name:
            self._say("a server needs a name")
            return
        cfg, why = _entry_from_words(target.split())
        if cfg is None:
            self._say(why or "cannot read that entry")
            return
        app = self.app
        err = app.mcp.add(name, cfg)
        app.rebuild_mcp_dispatch()
        # A failed START still leaves the server declared, so the form is
        # cleared either way — leaving the text in place would invite a second
        # Add that refuses as a duplicate.
        self.query_one("#mcp-add-name", Input).value = ""
        self.query_one("#mcp-add-target", Input).value = ""
        self._say(f"declared {name}, but it did not start: {err}" if err else f"added {name}")
        await self._rerender()

    @on(Button.Pressed, "#mcp-close")
    def _close(self) -> None:
        close_dialog(self, None)
