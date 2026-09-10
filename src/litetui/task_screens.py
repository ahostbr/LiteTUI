"""The two footer panels: what is running without me. T570 piece 3.

Ryan (19:5x): "both backgroudn proccess and subagents will need a new modal for
them. backgroudn proccess just need to show them running with time display etc
... sub agents should show prompt sent > thinking > response ... clearing when
the agent is done."

🔴 ONE PREDICATE, AND IT IS `tasks.split_live`. The two chips (piece 1), their
counts, and both panels here read the SAME partition. Deriving "is this a
subagent" a second time is how a row eventually appears in both panels or in
neither, and neither mistake is visible in a number.

🔴 ONE RENDERER FOR THE PANEL AND FOR THE RPC TEXT. `bg_text` / `agents_text`
produce the rows, and the widgets display those same strings. A separate text
form for the headless transport would be a second description of the same rows;
the two drift the first time one of them learns a new field, and the drift shows
up as a model being told something the human is not.

🔴 HEADLESS `--rpc` GETS DATA, NEVER A SCREEN (T558-A). A dialog pushed there
waits on a keyboard that is not attached, and the caller — a model, not a
person — hangs holding a turn that can never finish. `/think`'s no-arg branch is
the precedent; `open_background` / `open_subagents` are the one door each, so a
future caller inherits the guard instead of having to remember it.

⚠️ WHAT A LIVE SUBAGENT CAN HONESTLY SHOW — MEASURED, NOT ASSUMED.
`subagent_plugin._make_runner` sends `"stream": False`: one blocking request.
`tasks.finish` writes the log only when that request returns, and
`tasks.tail_text` refuses a RUNNING task by design. So while an agent is in
flight there is NO partial text anywhere to display — not reasoning, not a
partial answer. The three stages are therefore rendered as what each one
actually is: PROMPT from `Task.prompt` (real), THINKING as the state plus the
live clock (the only true signal this transport carries), RESPONSE named as
pending. A thinking pane that stayed permanently blank would read as the model
being stuck, which is the failure worth avoiding here.
"""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Static

from litetui import tasks as tasks_mod
from litetui.fmt import fmt_dur
from litetui.side_panel import SwapButton, close_dialog, open_dialog

#: How often the clocks move. A whole recompose every second would fight the
#: scroll position and any focus in the panel, so the tick updates the row TEXT
#: and recomposes only when the set of live rows actually changes.
REFRESH_S = 1.0

PROMPT_LINES = 12


def _live(app) -> tuple[list, list]:
    """(subagents, background), from the app's own store. `getattr` for the same
    reason the footer uses it: these bodies are built against apps that are not
    a whole app, and a panel that CRASHES on a missing registry is worse than
    one that shows nothing."""
    return tasks_mod.split_live(getattr(app, "bg_tasks", {}).values())


def _clip(text: str, lines: int = PROMPT_LINES) -> str:
    rows = (text or "").splitlines()
    if len(rows) <= lines:
        return text or ""
    cut = len(rows) - lines
    return "\n".join(rows[:lines]) + f"\n[... {cut} more line{'s' if cut != 1 else ''} ...]"


# ── the rows, rendered once for both surfaces ────────────────────────────


def bg_row(task) -> str:
    """One background process: what it is, how long it has been at it."""
    label = task.label or "(no command)"
    return f"{task.id}  {task.tool}  ·  {fmt_dur(task.seconds)}  ·  {task.state}\n{label}"


def agent_block(task) -> str:
    """One subagent, as the three stages. See the module docstring for why the
    second and third are a state and not a transcript."""
    prompt = _clip((task.prompt or task.label or "").strip()) or "(no prompt recorded)"
    return (
        f"{task.id}  ·  {fmt_dur(task.seconds)}  ·  {task.state}\n"
        f"  prompt sent\n{prompt}\n"
        f"  thinking      working — {fmt_dur(task.seconds)} so far. This call does "
        f"not stream, so there is no partial text yet.\n"
        f"  response      arrives when it finishes; it is delivered into the "
        f"conversation, and this panel drops the agent."
    )


def bg_text(app) -> str:
    """The background half as plain text — what `--rpc` gets instead of a panel."""
    _, bg = _live(app)
    if not bg:
        return "no background processes running"
    return "\n\n".join(bg_row(t) for t in bg)


def agents_text(app) -> str:
    """The subagent half as plain text — what `--rpc` gets instead of a panel."""
    subs, _ = _live(app)
    if not subs:
        return "no subagents running"
    return "\n\n".join(agent_block(t) for t in subs)


# ── the panels ───────────────────────────────────────────────────────────


class _LiveTaskBody(Widget):
    """Shared shell: a live list that keeps its own clock.

    Both panels are the same widget with a different half of `split_live` and a
    different row renderer, so the refresh rule — the one thing here with a bug
    in it worth having — is written once.
    """

    #: Set by the subclasses.
    TITLE = ""
    EMPTY = ""

    DEFAULT_CSS = """
    _LiveTaskBody { height: auto; layout: vertical; }
    _LiveTaskBody .lt-title { text-style: bold; padding: 0 0 1 0; }
    _LiveTaskBody .lt-note { color: $text-muted; padding: 0 0 1 0; }
    _LiveTaskBody .lt-scroll { height: 16; padding: 0 1; }
    _LiveTaskBody .lt-row { padding: 0 0 1 0; }
    _LiveTaskBody .lt-buttons { height: auto; align: center middle; padding: 1 0 0 0; }
    _LiveTaskBody .lt-buttons Button { margin: 0 1 0 0; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._ids: list[str] = []
        self._scroll_y = 0

    # The two halves. Subclasses fill these in.
    def rows(self) -> list:
        raise NotImplementedError

    def render_row(self, task) -> str:
        raise NotImplementedError

    def compose(self) -> ComposeResult:
        rows = self.rows()
        self._ids = [t.id for t in rows]
        yield Static(f"{self.TITLE} ({len(rows)})", classes="lt-title")
        if not rows:
            yield Static(self.EMPTY, classes="lt-note")
        with VerticalScroll(classes="lt-scroll"):
            for task in rows:
                yield Static(
                    self.render_row(task),
                    id=f"lt-{task.id}",
                    classes="lt-row",
                    markup=False,
                )
        with Horizontal(classes="lt-buttons"):
            yield Button("Close", variant="primary", classes="lt-close")
            yield SwapButton(classes="inline")

    def on_mount(self) -> None:
        if self._scroll_y:
            try:
                self.query_one(VerticalScroll).scroll_to(y=self._scroll_y, animate=False)
            except Exception:
                pass
        self.set_interval(REFRESH_S, self.sync)

    def sync(self) -> bool:
        """Move the clocks. Returns True when the panel was rebuilt.

        🔴 THE MEMBERSHIP CHECK IS WHAT MAKES THIS CLEAR ITSELF. A task that
        finished leaves `split_live` on its own, with no key pressed — so the
        id list changing IS the completion signal, and rebuilding on it is what
        Ryan's "clearing when the agent is done" asks for. Updating only the
        text would leave a finished agent on screen with a frozen clock, which
        looks like a hang rather than a finish.
        """
        rows = self.rows()
        ids = [t.id for t in rows]
        if ids != self._ids:
            self._ids = ids
            self.refresh(recompose=True)
            return True
        for task in rows:
            try:
                self.query_one(f"#lt-{task.id}", Static).update(self.render_row(task))
            except Exception:
                pass
        return False

    # ── the swap contract ────────────────────────────────────────────────

    def get_state(self) -> dict:
        """Only the scroll position: every row is read from the live store, so a
        rebuilt body reads the same truth from the same place."""
        try:
            return {"scroll_y": self.query_one(VerticalScroll).scroll_offset.y}
        except Exception:
            return {}

    def set_state(self, state: dict) -> None:
        self._scroll_y = int(state.get("scroll_y") or 0)

    @on(Button.Pressed, ".lt-close")
    def _close(self) -> None:
        close_dialog(self, None)


class BackgroundProcessesBody(_LiveTaskBody):
    """Named `*Body`, not `*Screen`, because it is host-agnostic like every other
    dialog here (`LoopListBody`, `MCPListBody`): `side_panel` decides whether it
    lands in a modal or the sidebar."""

    TITLE = "Background processes"
    EMPTY = (
        "Nothing running in the background. A tool call becomes one when it is "
        "asked to (background=true) or when it outlives "
        "settings.tool_auto_background_s."
    )

    def rows(self) -> list:
        return _live(self.app)[1]

    def render_row(self, task) -> str:
        return bg_row(task)


class SubagentsBody(_LiveTaskBody):
    TITLE = "Subagents"
    EMPTY = "No subagents running."

    def rows(self) -> list:
        return _live(self.app)[0]

    def render_row(self, task) -> str:
        return agent_block(task)


# ── the doors ────────────────────────────────────────────────────────────


def open_background(app) -> None:
    if getattr(app, "_rpc", False):
        app.system_message(bg_text(app))  # NO SCREEN OVER RPC — see the docstring
        return
    open_dialog(app, BackgroundProcessesBody)


def open_subagents(app) -> None:
    if getattr(app, "_rpc", False):
        app.system_message(agents_text(app))
        return
    open_dialog(app, SubagentsBody)
