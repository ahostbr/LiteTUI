"""/help — the command reference, and the screen that shows it.

HelpScreen moved here with its command: one owner for the help surface.
The app's CSS still styles #help-box and friends — Textual styles by
selector at runtime, so the class's home does not matter to the skin.
"""
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from litetui import paths
from litetui.settings import THINKING_LEVELS
from litetui.plugins import PluginManifest


class HelpScreen(ModalScreen[None]):
    """Scrollable, dismissable help. Same content as /help, readable."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def __init__(self, body: str):
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static("Commands & keys", id="help-title")
            with VerticalScroll(id="help-scroll"):
                yield Static(self._body, id="help-body")
            with Horizontal(id="help-buttons"):
                yield Button("Close", variant="primary", id="help-close")

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#help-close")
    def _close(self) -> None:
        self.dismiss(None)


def _cmd_help(app, name: str, arg: str) -> None:
    # Scrollable modal with a Close button; the text is unchanged.
    app.push_screen(HelpScreen(
        "/settings        open the settings panel (every knob, scrollable)\n"
        "/skills [name]   list discovered skills, or show one as the model sees it\n"
        "/new /clear      start a new conversation (new file on disk)\n"
        "/clear-screen    clear the DISPLAY only — conversation untouched\n"
        "/system <text>   set the system prompt\n"
        "/model [n]       show or switch model\n"
        "/think [level]   thinking level: "
        + ", ".join(THINKING_LEVELS)
        + ", unset\n"
        "/mark            drag a marker onto the screen; send ships the "
        "screenshot + coords here\n"
        "/cron            scheduled prompts: add/list/rm/on/off/run "
        "(fires while the app is open)\n"
        "/calendar /cal   the month; click a day to view, add or edit its jobs\n"
        "/compact [hint]  summarise older messages, keep the last "
        f"{app.settings.compact_keep_recent}\n"
        "/convos          list saved conversations\n"
        "/resume <n|id>   load a saved conversation (id = uuid prefix)\n"
        "/reconnect       reconnect   |   /quit  exit\n"
        "Esc     stop the current turn (asks first; Esc again = force)\n"
        "Ctrl+T  toggle agent tools (bash, read, write, web_fetch)\n"
        "drag    select text  |  Ctrl+Shift+C  copy the selection\n"
        "Shift+drag  select with the TERMINAL instead (system clipboard) —\n"
        "        the app captures the mouse, so a plain drag never reaches it\n"
        f"store: {paths.CONVO_DIR.name}/<uuid>/ holds convo.jsonl, memory.md,\n"
        f"       soul.md, handoff.md and {paths.MEMORIES_DIR}/ — the agent is told\n"
        "       its own path in the system prompt and manages them itself\n"
        "footer: live context usage — ctx used / window"
    ))


def _register(ctx) -> None:
    ctx.command(
        ("/help", "/?"), _cmd_help,
        palette="Help",
        help="Commands and keyboard shortcuts.",
        group="app",
        order=20,
    )


PLUGIN = PluginManifest(id="help", register=_register)
