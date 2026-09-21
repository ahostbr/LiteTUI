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


def _verb_button(idx: int, verb: str, server: str) -> Button:
    """A row-action button carrying the EXACT server name it was rendered for.
    The Textual id stays index-based (a server name is not a legal id), but the
    action reads `_mcp_server`, so a describe() reorder between render and press
    can never retarget the wrong server."""
    btn = Button(verb.capitalize(), id=f"mcp-act-{idx}-{verb}")
    btn._mcp_server = server
    return btn


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
                yield Static(row["error"], classes="mcp-error", markup=False)
            # 🔴 CONSTRUCTED, NOT `with Horizontal(...)`. The context-manager
            # form appends to `app._compose_stacks`, which only exists while
            # compose() is running — and this generator runs AGAIN on every
            # re-render, where that stack is empty and the `with` raises
            # IndexError deep inside Textual. Passing children to the
            # constructor is the form that works in both callers.
            yield Horizontal(
                *(_verb_button(i, verb, row["name"])
                  for verb in ACTIONS_FOR.get(row["state"], ())),
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
            self.query_one("#mcp-status", Static).update(text)
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

    def _own_workers(self) -> tuple:
        """This dialog's OWN host worker OBJECT, excluded from its activity gate
        so the dialog does not count itself as busy. Excluded by `is` identity;
        any OTHER worker (including a different same-group one) still blocks."""
        host = getattr(self, "_dialog_host_worker", None)
        return (host,) if host is not None else ()

    def _own_modal_screen(self):
        """Our own modal screen, but ONLY when we are hosted in a real modal
        ABOVE the base that STILL contains this body — never the base/sidebar
        screen (docked), and never a foreign overlay. Returned for the activity
        gate to exclude; None when docked, so a foreign modal keeps the app busy
        and cannot be subtracted away by a containment mismatch."""
        try:
            screen = self.screen
            stack = self.app.screen_stack
        except Exception:
            return None
        if screen is None or not stack or screen is stack[0]:
            return None                       # docked/sidebar → base screen, not ours
        try:
            if any(n is self for n in screen.walk_children(with_self=True)):
                return screen                 # a modal that actually hosts THIS body
        except Exception:
            pass
        return None

    def _gate(self, app) -> str | None:
        """The SAME native/maintenance/idle gate the /mcp command runs, minus
        THIS dialog's own identity (its host worker + its own modal) so it does
        not refuse itself — a real turn/tool/child/busy-store or a SECOND modal
        still blocks."""
        from litetui.plugins.mcp_manage import _mutation_blocked_reason
        return _mutation_blocked_reason(
            app, ignore_workers=self._own_workers(), ignore_screen=self._own_modal_screen())

    def _is_active_body(self) -> bool:
        """True only when THIS body is still the controller's current view. A
        swap can leave the old body briefly mounted while no longer active; a
        completion must not re-render a superseded body."""
        if not self.is_mounted:
            return False
        ctrl = getattr(self, "_dialog_controller", None)
        return ctrl is None or getattr(ctrl, "_body", None) is self

    def _dispatch(self, op, describe, mcp0) -> None:
        """Claim maintenance SYNCHRONOUSLY (a second button then defers on the
        gate), capture convo/backend/manager identity, and run the op OFF the
        loop via run_guarded — which un-sticks maintenance if the worker is
        cancelled before its first step or fails to schedule."""
        import asyncio
        from litetui.agent_preparation import run_guarded
        app = self.app
        app._mcp_maintenance = True
        ev = asyncio.Event()
        app._mcp_maintenance_done = ev

        def _release():
            if not ev.is_set():
                app._mcp_maintenance = False
                ev.set()

        coro = self._run_action(app, op, describe, app.convo_id, app.backend, mcp0)
        run_guarded(app, coro, group="mcp", cleanup=_release,
                    report=lambda e: app.system_message("Could not start the MCP operation."))

    async def _run_action(self, app, op, describe, convo0, backend0, mcp0) -> None:
        # `app` is captured at SCHEDULING and passed in: reading self.app HERE
        # would raise NoActiveApp if this widget were removed before the coro's
        # first step, stranding maintenance before the try/finally below.
        from litetui.agent_preparation import await_preparation
        from litetui.mcp_client import MCPBusy
        from litetui.plugins.mcp_manage import _settle_maintenance
        msg = ""
        try:
            # Revalidate identity BEFORE the mutation. This is a LOOP-ADMISSION
            # check, not a thread-start atomic guarantee: await_preparation
            # schedules a thread, so a switch AFTER this check but before the
            # thread runs is not atomically prevented. The op is bound to mcp0,
            # so it can never RETARGET a different manager; but a captured old
            # operation may still finish AFTER an accepted context switch — that
            # completion is reported (below), never re-rendered onto the new
            # context, and never claimed as the current mutation's success.
            if (app.convo_id != convo0 or app.backend is not backend0
                    or app.mcp is not mcp0 or hasattr(app.backend, "app_server")):
                msg = "context changed before the MCP operation started — not applied."
            else:
                try:
                    msg = describe(await await_preparation(op))
                except MCPBusy:
                    msg = "MCP maintenance is in progress — try again in a moment."
                except Exception as e:  # noqa: BLE001 — bounded, never raw error text
                    msg = f"MCP operation failed ({type(e).__name__})."
        finally:
            note = _settle_maintenance(app)
        # Re-render ONLY if this body is still the ACTIVE view in the SAME
        # context — convo, backend AND manager. A manager switch means
        # _settle_maintenance rebuilt the CURRENT (new) manager's dispatch, not
        # the one this op targeted, so re-rendering here would paint an old-op
        # result onto the new manager; report to chat instead.
        if (self._is_active_body() and app.convo_id == convo0
                and app.backend is backend0 and app.mcp is mcp0):
            self._say(msg + note)
            await self._rerender()
        else:
            app.system_message(msg + note)

    @on(Button.Pressed, ".mcp-actions Button")
    async def _row_action(self, event: Button.Pressed) -> None:
        event.stop()
        parts = (event.button.id or "").split("-", 3)
        name = getattr(event.button, "_mcp_server", None)   # exact name bound at render
        if len(parts) != 4 or name is None:
            return
        verb = parts[3]
        app = self.app
        blocked = self._gate(app)
        if blocked:
            self._say(blocked)
            return
        mcp0 = app.mcp                                       # bind the manager; op targets THIS one
        if verb == "connect":
            op = lambda: mcp0.connect(name)
            describe = lambda err: f"could not connect {name}: {err}" if err else f"connected {name}"
        elif verb == "disconnect":
            op = lambda: mcp0.disconnect(name)
            describe = lambda was: f"disconnected {name}" if was else f"{name} was not running"
        elif verb == "reconnect":
            def op():
                mcp0.reload_configs()   # pick up config edits before reconnecting
                return mcp0.reconnect(name)
            describe = lambda err: f"could not reconnect {name}: {err}" if err else f"reconnected {name}"
        elif verb == "remove":
            op = lambda: mcp0.remove(name)
            describe = lambda err: f"could not remove {name}: {err}" if err else f"removed {name}"
        else:
            return
        self._dispatch(op, describe, mcp0)

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
        blocked = self._gate(app)
        if blocked:
            self._say(blocked)
            return
        # cfg is the parsed dict (command+args or {json}) — passed to the manager
        # as data, never a shell string. Clear the form now the add is accepted,
        # so an in-flight add cannot be re-submitted as a duplicate.
        self.query_one("#mcp-add-name", Input).value = ""
        self.query_one("#mcp-add-target", Input).value = ""
        mcp0 = app.mcp
        self._dispatch(
            lambda: mcp0.add(name, cfg),
            lambda err: (f"declared {name}, but it did not start: {err}" if err
                         else f"added {name}"),
            mcp0)

    @on(Button.Pressed, "#mcp-close")
    def _close(self) -> None:
        close_dialog(self, None)
