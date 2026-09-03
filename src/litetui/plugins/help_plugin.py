"""/help — the command reference, and the screen that shows it.

HelpScreen moved here with its command: one owner for the help surface.
The app's CSS still styles #help-box and friends — Textual styles by
selector at runtime, so the class's home does not matter to the skin.
"""
from functools import partial

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Static

from litetui import paths
from litetui.settings import THINKING_LEVELS
from litetui.plugins import PluginManifest
from litetui.side_panel import SwapButton, close_dialog, present_dialog


class HelpBody(Widget):
    """The help content, host-agnostic. Exits through `close_dialog`.

    THE PERCENTAGE MOVES UP A LEVEL; THE BOX DOES NOT KEEP IT. `#help-box` was
    a DIRECT child of `HelpScreen` at `height: 80%`, so its base was the screen.
    Inserting this body between them re-bases that 80% on whatever this widget
    is -- and at `height: auto` the base would be derived from the very box it
    constrains, which is the fixed-point-less shape PickerBody's comment
    measured (content settled at 10 rows for children needing 14).

    So the 80% lives HERE, taken off the screen exactly as before, and the box
    caps at 100% OF THIS. Same rendered height on the modal path; in a sidebar
    `SidePanel`'s `> *` rule overrides this to the panel's full height, which is
    what a strip wants.
    """

    DEFAULT_CSS = """
    HelpBody { width: auto; height: 80%; align: center middle; layout: vertical; }
    """

    BINDINGS = [
        # `q` closes. NOT `escape`: `SidePanel` already binds escape to cancel
        # the dialog, and a second binding for the same key one level down is a
        # coin toss over which fires. On the modal path the screen below carries
        # both, exactly as it always did.
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
                # `.inline` because it shares a row with Close -- SwapButton's
                # own `width: 100%` would take the whole row otherwise.
                yield SwapButton(classes="inline")

    # -- state carry across a live host swap --------------------------------
    def get_state(self) -> dict:
        """Carry the SCROLL POSITION across a swap.

        Help is one long scroll, and the reason to dock it is usually to read it
        beside the chat. Rebuilding at the top would send the reader back to
        line one at the exact moment they asked for a better view of line 200.
        """
        return {"scroll_y": self.query_one("#help-scroll", VerticalScroll).scroll_y}

    def set_state(self, state: dict) -> None:
        y = state.get("scroll_y")
        if y:
            self.query_one("#help-scroll", VerticalScroll).scroll_to(y=y, animate=False)

    def action_close(self) -> None:
        close_dialog(self, None)

    @on(Button.Pressed, "#help-close")
    def _close(self, event: Button.Pressed) -> None:
        event.stop()
        close_dialog(self, None)


class HelpScreen(ModalScreen[None]):
    """Scrollable, dismissable help. Same content as /help, readable.

    NOT replaced by `_ModalHost`: `app.py`'s centering rule names this class
    (`ConfirmStop, PickerScreen, HelpScreen, SettingsScreen, ...`), so routing
    the modal path through the generic host would change what the modal IS.
    Bindings stay here as well as on the body -- a ModalScreen is what has focus
    when the dialog opens as a modal.
    """

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def __init__(self, body: str):
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        yield HelpBody(self._body)

    def action_close(self) -> None:
        self.dismiss(None)


def _cmd_help(app, name: str, arg: str) -> None:
    # Scrollable dialog with a Close button; the text is unchanged. Both
    # factories are built from ONE string here rather than passed twice --
    # `picker.pick`'s reason: two factories built at two places are two chances
    # for the sidebar and the modal to show different help.
    text = (
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
        "/goal <objective> evidence-driven continuation; status/pause/resume/clear\n"
        "/loop            the loop panel; /loop <15m> <p> adds one\n"
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
    )
    present_dialog(app, partial(HelpBody, text), partial(HelpScreen, text))


def _register(ctx) -> None:
    ctx.command(
        ("/help", "/?"), _cmd_help,
        palette="Help",
        help="Commands and keyboard shortcuts.",
        group="app",
        order=20,
    )


PLUGIN = PluginManifest(id="help", register=_register)
