"""The `/tools` list: every tool the model can reach, with a checkbox each.

Ryan, 2026-08-24: "/tools is only a toggle for agent tools on and off. the only
way to list all the tools is to ask the model itself. there should be a full
list of tools available to the model with individual toggle checkmarks ...
there should also be one global toggle in the list that syncs with the agent
loop settings page."

WHAT A ROW SHOWS, AND WHY EACH PART IS THERE
  the checkbox      ticked = offered. Unticking withholds the schema AND
                    refuses the call at the authorization door.
  the AUTHORITY     from `tool_policy`, so "why is this one gated?" is
                    answerable without reading source.
  the description   the tool's own schema description, which is what the model
                    reads — not a second copy written here that could drift.

🔴 NOTHING IS SYNCHRONISED, BECAUSE THERE IS NOTHING TO SYNCHRONISE. The global
switch calls `app.action_toggle_tools()` — the SAME verb Ctrl+T and the settings
screen use — and the checkboxes write `settings.tools_disabled`, which is the
one field both the engine and the settings screen read. Two copies kept in
agreement is the drift class this codebase has been bitten by three times in a
day; this dialog deliberately owns no state of its own.

CHANGES APPLY IMMEDIATELY. There is no Save button: a checkbox that needs
confirming is a checkbox that lies about what it did. So a host swap loses
nothing except the scroll position, which is the only thing `get_state` carries.
"""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Label, Static, Switch

from litetui import settings as settings_mod
from litetui.side_panel import SwapButton, close_dialog


def tool_rows(app) -> list[dict]:
    """One dict per statically registered tool, in registration order.

    A FUNCTION over the live registry rather than a table: a tool added by a
    plugin appears here with no second list to update. That is the same reason
    the profile dropdown derives itself.
    """
    rows = []
    for entry in app.plugins.tools:
        policy = entry.policy
        caps = sorted(policy.capabilities) if policy else []
        if caps:
            authority = " · ".join(caps)
        elif policy is not None and policy.classify_args is not None:
            # `write` declares NOTHING statically — workspace_write vs
            # external_write is decided by the path at call time. Rendering an
            # empty string here would read as "this tool has no authority",
            # which is the opposite of true.
            authority = "set by the arguments"
        else:
            authority = "unknown"
        rows.append(
            {
                "name": entry.name,
                "authority": authority,
                "summary": (policy.summary if policy else "") or "",
                "description": (entry.spec.get("function", {}).get("description") or "").strip(),
            }
        )
    return rows


class ToolListBody(Widget):
    """A dialog body that works in either host. No ModalScreen assumptions."""

    DEFAULT_CSS = """
    ToolListBody { height: auto; layout: vertical; }
    ToolListBody #tl-title { text-style: bold; padding: 0 0 1 0; }
    ToolListBody #tl-global {
        height: auto;
        padding: 0 0 1 0;
        border-bottom: solid $foreground 20%;
    }
    ToolListBody #tl-global-label { width: 1fr; padding: 1 0 0 1; }
    ToolListBody #tl-note { color: $text-muted; padding: 1 0; }
    ToolListBody #tl-scroll { height: 18; padding: 0 1; }
    ToolListBody .tl-auth { color: $text-muted; padding: 0 0 1 3; }
    ToolListBody #tl-mcp { color: $text-muted; padding: 1 0 0 0; }
    ToolListBody #tl-buttons { height: auto; align: center middle; padding: 1 0 0 0; }
    ToolListBody #tl-buttons Button { margin: 0 1 0 0; }
    """

    def __init__(self) -> None:
        super().__init__()
        # Guards the programmatic write in set_state / the global switch, so
        # restoring a value cannot be mistaken for the user changing it.
        self._echo = False
        self._scroll_y = 0

    # ── composition ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        app = self.app
        off = set(getattr(app.settings, "tools_disabled", None) or ())
        yield Static("Tools", id="tl-title")
        with Horizontal(id="tl-global"):
            yield Switch(value=bool(app.tools_enabled), id="tl-global-switch")
            yield Label(
                "All tools — the same switch as Ctrl+T and Settings → Agent loop",
                id="tl-global-label",
            )
        yield Static(
            "Unticking a tool hides it from the model and refuses it if called "
            "anyway. Changes apply at once — there is no save.",
            id="tl-note",
        )
        with VerticalScroll(id="tl-scroll"):
            for row in tool_rows(app):
                yield Checkbox(
                    row["name"],
                    value=row["name"] not in off,
                    id=f"tl-tool-{row['name']}",
                )
                detail = row["authority"]
                text = row["description"] or row["summary"]
                if text:
                    detail = f"{detail} — {text}"
                yield Static(detail, classes="tl-auth", markup=False)
        mcp = len(app.plugins.dynamic)
        if mcp:
            yield Static(
                f"MCP servers ({mcp}) provide tools too. Those are switched off "
                "per SERVER in Settings → Capabilities, not here.",
                id="tl-mcp",
            )
        with Horizontal(id="tl-buttons"):
            yield Button("Close", variant="primary", id="tl-close")
            yield SwapButton(classes="inline")

    # ── the swap contract ────────────────────────────────────────────────

    def get_state(self) -> dict:
        """Only the scroll position. Every checkbox has ALREADY been written to
        settings, so a rebuilt body reads the same truth from the same place —
        there is no pending edit here to lose."""
        try:
            return {"scroll_y": self.query_one("#tl-scroll", VerticalScroll).scroll_offset.y}
        except Exception:
            return {}

    def set_state(self, state: dict) -> None:
        self._scroll_y = int(state.get("scroll_y") or 0)

    def on_mount(self) -> None:
        if self._scroll_y:
            try:
                self.query_one("#tl-scroll", VerticalScroll).scroll_to(
                    y=self._scroll_y, animate=False
                )
            except Exception:
                pass

    # ── the writes ───────────────────────────────────────────────────────

    @on(Switch.Changed, "#tl-global-switch")
    def _global(self, event: Switch.Changed) -> None:
        if self._echo:
            return
        if bool(event.value) == bool(self.app.tools_enabled):
            return  # already there; nothing to toggle
        # THE SAME VERB, not a copy of what it does. It rebuilds the system
        # prompt, updates the header, tells the user, and persists — a
        # hand-rolled `tools_enabled = x` here would silently skip all four.
        self.app.action_toggle_tools()

    @on(Checkbox.Changed)
    def _one_tool(self, event: Checkbox.Changed) -> None:
        if self._echo:
            return
        cid = event.checkbox.id or ""
        if not cid.startswith("tl-tool-"):
            return
        name = cid[len("tl-tool-"):]
        off = list(getattr(self.app.settings, "tools_disabled", None) or ())
        if event.value:
            off = [n for n in off if n != name]
        elif name not in off:
            off.append(name)
        self.app.settings.tools_disabled = off
        try:
            settings_mod.save(self.app.settings)
        except OSError:
            # Same shape as every other persistence failure in this app: the
            # change holds for the session and says so, rather than reporting
            # a success that will not survive a restart.
            self.app._system(
                f"could not save the tool list — {name} is off for this session only"
            )

    @on(Button.Pressed, "#tl-close")
    def _close(self) -> None:
        close_dialog(self, None)
