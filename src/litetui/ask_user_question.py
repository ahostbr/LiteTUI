"""AskUserQuestion — the agent asks, Ryan answers in a Textual widget.

The model calls this tool with a list of questions. Each question carries
multiple-choice options, and the widget adds a free-text "Type something"
field to every question. It is MULTI-SELECT: any combination of options can
be correct — checkboxes, not radio. (Ryan's locked spec, 2026-08-20: the
example text in the reference screenshot reads single-answer, but the widget
semantics win — the owner's spec, not the example, defines behavior.)

What the human can do:
  * jump between questions freely (top-bar steps — NOT strictly sequential),
  * tick / untick options on the active question,
  * click a step's checkbox to CLEAR that question's answer (toggleable),
  * type a note on any question (the "Type something" field),
  * Next / Submit   — advance, then submit once every question is answered,
  * Chat about this — early exit: the full state of whatever has been
                      entered so far comes back WITHOUT every question
                      needing an answer, so the agent can discuss first,
  * Esc             — cancel: no answers at all.

The result is always a STRING — a role:"tool" message can only carry text —
serialized in a form a local model can act on.

THE SUSPENSION BRIDGE (the core wiring problem)
-----------------------------------------------
app.py dispatches every tool with `await asyncio.to_thread(fn, args)`, so
`run()` executes on a WORKER THREAD while the Textual event loop keeps
running on the main thread. `run()` therefore:

  1. pushes the modal screen ONTO the app's event loop, inside
     `app._context()` — the same idiom Textual 8.0.2 uses in its own
     `App.call_from_thread` and `Worker._run`. Both parts matter (both
     layers measured 2026-08-20):
       * called directly from a worker thread, `push_screen`'s
         `asyncio.Future()` fallback calls `get_event_loop()`, which on
         Python 3.11 RAISES in a thread without a loop (RuntimeError
         "There is no current event loop in thread");
       * even scheduled onto the loop, the push must run inside
         `app._context()`: without it the screen's own message-pump task
         (started during the push) inherits a context with no
         `active_app` ContextVar, and its `compose` dies with
         NoActiveAppError.
     The dismiss future is unawaited, so `set_result` on Submit/Chat/Esc
     is a no-op, not a hazard.
  2. blocks on a `threading.Event` that the widget sets the moment Ryan
     presses Submit, "Chat about this", or Esc.

No UI is built or driven from the worker thread; it only wakes and returns
the string the widget decided on. `done.wait()` is polled with a timeout so
a LiteTUI that exits mid-question cannot hang the thread forever
(`asyncio.to_thread` threads are not daemons).
"""

from __future__ import annotations

import asyncio
import threading
from uuid import uuid4
from dataclasses import dataclass, field
from typing import Any
from functools import partial
from litetui.side_panel import SwapButton, close_dialog, present_dialog
from litetui import tool_schemas

from rich.text import Text
from textual import events, on
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

MAX_QUESTIONS = 8
MAX_OPTIONS = 10

FOOTER_HINT = "Enter select · Tab/Arrows navigate · Esc cancel"

ASK_USER_QUESTION_TOOL_SPEC = tool_schemas.load("ask_user_question")


# ── State ──────────────────────────────────────────────────────────


@dataclass
class QuestionState:
    """One question and the human's current answers to it."""

    label: str
    question: str
    options: list[dict]  # {"title": str, "description": str}
    selected: set[int] = field(default_factory=set)
    note: str = ""
    #: May Ryan tick more than one option? (T644)
    #:
    #: 🔴 DEFAULTS TO TRUE BECAUSE THAT IS WHAT THIS TOOL HAS ALWAYS DONE. The
    #: schema has said "Options are MULTI-SELECT: any number of them can be
    #: correct" since it was written, `selected` is a `set[int]`, and the widget
    #: ticks freely. The flag is not a new capability — it is this one finally
    #: being STATED, so a consumer that cannot see the widget knows what it is
    #: rendering. A model that wants one-of-N now says so.
    multi_select: bool = True
    secret: bool = False

    @property
    def answered(self) -> bool:
        return bool(self.selected) or bool(self.note.strip())

    def clear(self) -> None:
        """Untick everything on this question (the step-bar checkbox)."""
        self.selected.clear()
        self.note = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "question": self.question,
            "options": [dict(o) for o in self.options],
            "selected": sorted(self.selected),
            "note": self.note,
            "answered": self.answered,
            # 🔴 THE WIRE NAME IS camelCase BECAUSE THE CONSUMER IS TYPESCRIPT.
            # LiteSuite reads `question.multiSelect === true`
            # (session-logic.ts, T634) and every other key in this dict is
            # already the name it reads. A snake_case key here would be dropped
            # silently by a whitelist rebuild — which is exactly how T634's
            # flag went missing in the first place, one process further on.
            "multiSelect": self.multi_select,
            **({"isSecret": True} if self.secret else {}),
        }


def _parse_questions(args: dict) -> list[QuestionState]:
    """Validate the tool input. Raises ValueError with an actionable message.

    Deliberately lenient where a local model will drift (string options,
    missing descriptions) and strict where silence would be wrong (no
    question, no usable options) — a failed call that says WHY beats a
    widget that renders something half-empty.
    """
    raw = args.get("questions")
    if not isinstance(raw, list) or not raw:
        raise ValueError("`questions` must be a non-empty list of question objects")
    if len(raw) > MAX_QUESTIONS:
        raise ValueError(
            f"at most {MAX_QUESTIONS} questions per call — you gave {len(raw)}; "
            f"ask the first {MAX_QUESTIONS} now and the rest after"
        )
    states: list[QuestionState] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"questions[{i}] must be an object with label/question/options")
        label = str(item.get("label") or "").strip() or f"Question {i + 1}"
        question = str(item.get("question") or "").strip()
        if not question:
            raise ValueError(f"questions[{i}] is missing its `question` text")
        raw_opts = item.get("options")
        if not isinstance(raw_opts, list) or (not raw_opts and not item.get("allowFreeText")):
            raise ValueError(f"questions[{i}] needs a non-empty `options` list")
        if len(raw_opts) > MAX_OPTIONS:
            raise ValueError(f"questions[{i}] has {len(raw_opts)} options; max is {MAX_OPTIONS}")
        opts: list[dict] = []
        for o in raw_opts:
            if isinstance(o, str):
                title, desc = o.strip(), ""
            elif isinstance(o, dict):
                title = str(o.get("title") or "").strip()
                desc = str(o.get("description") or "").strip()
            else:
                continue
            if title:
                opts.append({"title": title, "description": desc})
        if not opts and not item.get("allowFreeText"):
            raise ValueError(f"questions[{i}] has no usable options (each needs a non-empty title)")
        states.append(
            QuestionState(
                label=label,
                question=question,
                options=opts,
                # Absent means MULTI, which is this tool's long-standing
                # behaviour; only an explicit `false` narrows it.
                multi_select=item.get("multiSelect") is not False,
                secret=bool(item.get("isSecret")),
            )
        )
    return states


def _serialize(payload: dict) -> str:
    """The full answer state as a string — what the model reads back."""
    from litetui.question_result import publish
    publish(payload)
    action = payload["action"]
    qs: list[dict] = payload["questions"]
    answered = sum(1 for q in qs if q["answered"])

    blocks = []
    for q in qs:
        lines = [f"  {q['label']} — {'ANSWERED' if q['answered'] else 'not answered'}"]
        for i, opt in enumerate(q["options"]):
            mark = "x" if i in q["selected"] else " "
            line = f"    [{mark}] {opt['title']}"
            if opt.get("description"):
                line += f"  ({opt['description']})"
            lines.append(line)
        note = (q.get("note") or "").strip()
        lines.append(f"    note: {note if note else '(none)'}")
        blocks.append("\n".join(lines))
    body = "\n".join(blocks)

    if action == "submit":
        head = f"[ask_user_question] SUBMITTED — {answered} of {len(qs)} answered"
        tail = ("These selections and notes are the user's answers — proceed on that basis.")
    elif action == "chat":
        head = (
            f"[ask_user_question] CHAT ABOUT THIS — {answered} of {len(qs)} "
            f"answered so far (PARTIAL — they are not done; do not treat it as final)"
        )
        tail = (
            "The user pressed 'Chat about this': discuss the question(s) with them in "
            "plain chat. You may ask again later with a refined question list."
        )
    elif action == "aborted":
        # T632. NOT the `cancel` text: that one names Ryan pressing Esc, and a
        # turn the HOST stopped is a different event with a different actor. A
        # model told the wrong cause reasons from it — the same defect class
        # T558's "stale request" message was.
        return (
            "[ask_user_question] ABORTED — the turn was stopped while this "
            "question was still open, so it was never put to the user. No answers "
            "were given; do not assume any option and do not re-ask unless you "
            "are asked to continue."
        )
    else:
        return (
            "[ask_user_question] CANCELLED — the user pressed Esc without answering. "
            "No answers were given; do not assume any option. Ask in plain chat "
            "or re-issue the tool."
        )
    return f"{head}\n\n{body}\n\n{tail}"


# ── Widget ─────────────────────────────────────────────────────────


class _OptionBody(Vertical):
    """A focusable container of option rows.

    Focusable so the SCREEN's bindings receive the arrow/Enter/Tab keys:
    Textual routes keys to the focused widget and its ancestors, and a plain
    Vertical cannot hold focus. Textual 8.0.2 has no Focusable mixin —
    `can_focus` is a plain class attribute on Widget.
    """

    can_focus = True


class AskUserQuestionScreen(ModalScreen[None]):
    """The modal host. Kept as a distinct screen, same as the other three.

    Its own `align: center middle` is what centres the dialog; the content
    styling lives on the body, because Textual SCOPES `DEFAULT_CSS`/`CSS` to the
    DECLARING class and a body lifted out of here would otherwise render
    COMPLETELY UNSTYLED in a sidebar — no crash, no failing test.

    📌 DEPTH IS UNCHANGED BY THE SPLIT. This screen used to compose
    `Vertical(id="auq-box")`; it now composes a body that IS that box. That is
    deliberate: one EXTRA level swallowed every mouse click on the tool-approval
    dialog (T081), and this one has three `@on(events.Click)` handlers.
    """

    DEFAULT_CSS = """
    AskUserQuestionScreen {
        align: center middle;
    }
    """

    def __init__(self, states: list[QuestionState], done: threading.Event,
                 result_box: list[dict]) -> None:
        super().__init__()
        # The SAME objects the body gets, not copies — `states` is a list of
        # mutable QuestionState and both names refer to one list. Kept here
        # because the existing suite reads `screen._states` to assert what the
        # human's clicks did, and that assertion is about the STATE, not about
        # which widget happens to hold the reference.
        self._states = states
        self._done = done
        self._result_box = result_box

    def compose(self) -> ComposeResult:
        yield AskUserQuestionBody(self._states, self._done, self._result_box)


class AskUserQuestionBody(Vertical):
    """The AskUserQuestion widget. See the module docstring for the contract."""

    DEFAULT_CSS = """
    AskUserQuestionBody {
        width: 92;
        max-width: 96%;
        height: auto;
        max-height: 92%;
        padding: 1 2;
        background: $surface;
        border: thick $warning;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }

    .auq-step {
        margin: 0 1 0 0;
    }

    #auq-steps, #auq-bodies, .auq-qbody {
        height: auto;
    }

    #auq-progress {
        height: auto;
        text-align: center;
        color: $warning;
        padding: 1 0;
    }

    .auq-checkbox, .auq-step-label {
        width: auto;
        padding: 0 1;
        background: $surface-darken-2;
        color: $text-muted;
    }

    .auq-checkbox.answered {
        color: $warning;
        text-style: bold;
    }

    .auq-step-label.active {
        color: $warning;
        text-style: bold;
        background: $warning-darken-3;
    }

    #auq-question {
        text-style: bold;
        padding: 1 0;
    }

    .auq-qbody {
        display: none;
    }

    .auq-qbody.active {
        display: block;
    }

    .auq-row {
        padding: 0 1;
        background: $surface-darken-1;
    }

    .auq-row.selected {
        border-left: thick $warning;
        background: $warning-darken-3;
    }

    .auq-row.cursor {
        background: $surface-darken-3;
    }

    .auq-row.selected.cursor {
        background: $warning-darken-2;
    }

    #auq-note-input {
        margin: 0 0 1 0;
    }

    #auq-actions {
        height: auto;
        align: center middle;
    }

    #auq-actions Button {
        margin: 0 1;
    }

    #auq-hint {
        color: $text-muted;
        text-align: center;
        padding-top: 1;
    }
    """

    BINDINGS = [
        # Screen-level: the focused child (an _OptionBody) claims none of
        # these, so they land here. The app's own Esc->stop_turn binding is
        # shadowed the same way PickerScreen/ConfirmStop already shadow it.
        Binding("left", "prev_question", "Prev question", show=False),
        Binding("right", "next_question", "Next question", show=False),
        Binding("tab", "next_question", "Next question", show=False),
        Binding("shift+tab", "prev_question", "Prev question", show=False),
        Binding("up", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, states: list[QuestionState], done: threading.Event,
                 result_box: list[dict]) -> None:
        super().__init__()
        self._states = states
        self._done = done
        self._result_box = result_box
        self._active = 0       # which question is on screen
        self._cursor = 0       # which body row is highlighted (last = note row)
        self._finished = False

    # ── compose ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Horizontal(id="auq-steps"):
            for i, q in enumerate(self._states):
                # textual 8.0.2 removed widget.dataset: the id already
                # carries the index (auq-box-<i> / auq-lab-<i>).
                box = Static("☐", id=f"auq-box-{i}", classes="auq-checkbox")
                # 🔴 active must be true at COMPOSE, not just in on_mount:
                # _refresh_steps() runs in the async on_mount, and a caller
                # that checks the step bar the moment the screen is pushed
                # (compose done, on_mount not yet) saw the label WITHOUT the
                # active class — a window where the bar paints with no active
                # step. The body (auq-qbody) already sets it here; the label
                # now matches, so the invariant holds from first paint and
                # _refresh_steps() only maintains it on later jumps.
                lab = Static(
                    q.label,
                    markup=False,
                    id=f"auq-lab-{i}",
                    classes="auq-step-label active" if i == self._active
                    else "auq-step-label",
                )
                yield box
                yield lab
        yield Static(self._states[0].question, markup=False, id="auq-question")
        with Vertical(id="auq-bodies"):
            for i, q in enumerate(self._states):
                classes = "auq-qbody active" if i == self._active else "auq-qbody"
                with _OptionBody(id=f"auq-qbody-{i}", classes=classes):
                    for j, opt in enumerate(q.options):
                        # id = auq-opt-<qi>-<j> (dataset is gone in 8.x)
                        row = Static(
                            self._row_text(opt, False),
                            markup=False,
                            id=f"auq-opt-{i}-{j}",
                            classes="auq-row",
                        )
                        yield row
                    # The auto "Type something" option: a row that hands
                    # focus to the note field, per the locked spec.
                    yield Static(
                        "✎  Type something…",
                        id=f"auq-note-{i}",
                        classes="auq-row auq-note-row",
                    )
        yield Input(placeholder="Type something…", id="auq-note-input", password=self._states[0].secret)
        yield SwapButton()
        yield Static("", markup=False, id="auq-progress")
        with Horizontal(id="auq-actions"):
            yield Button("Chat about this", id="auq-chat")
            yield Button("Next" if len(self._states) > 1 else "Submit",
                         variant="primary", id="auq-submit")
        yield Static(FOOTER_HINT, markup=False, id="auq-hint")

    async def on_mount(self) -> None:
        self._refresh_steps()
        self._set_cursor()
        self._focus_body()

    # ── rendering helpers ──────────────────────────────────────────

    @staticmethod
    def _row_text(opt: dict, selected: bool) -> Text:
        gold = "#e8a33d"
        if selected:
            parts: list[tuple[str, str]] = [
                ("☑ ", f"bold {gold}"),
                (opt["title"], f"bold {gold}"),
            ]
        else:
            parts = [
                ("☐ ", "bold #7d8799"),
                (opt["title"], "bold"),
            ]
        if opt.get("description"):
            parts.append(("  " + opt["description"], "#7d8799"))
        return Text.assemble(*parts)

    def _active_body(self) -> _OptionBody:
        return self.query_one(f"#auq-qbody-{self._active}")

    def _focus_body(self) -> None:
        self._active_body().focus()

    def _refresh_steps(self) -> None:
        for i, q in enumerate(self._states):
            box = self.query_one(f"#auq-box-{i}", Static)
            lab = self.query_one(f"#auq-lab-{i}", Static)
            box.content = "☑" if q.answered else "☐"
            box.set_class(q.answered, "answered")
            lab.set_class(i == self._active, "active")
        answered = sum(q.answered for q in self._states)
        total = len(self._states)
        self.query_one("#auq-progress", Static).update(
            f"Question {self._active + 1} of {total} · {answered} of {total} answered"
        )
        self.query_one("#auq-submit", Button).label = (
            "Next" if self._active < total - 1 else "Submit"
        )

    def _refresh_row(self, i: int) -> None:
        q = self._states[self._active]
        row = list(self._active_body().query(".auq-row"))[i]
        row.content = self._row_text(q.options[i], i in q.selected)
        row.set_class(i in q.selected, "selected")

    def _refresh_all_rows(self, qi: int) -> None:
        q = self._states[qi]
        body = self.query_one(f"#auq-qbody-{qi}")
        for j, row in enumerate(list(body.query(".auq-row"))[:-1]):  # all but the note row
            row.content = self._row_text(q.options[j], j in q.selected)
            row.set_class(j in q.selected, "selected")

    def _set_cursor(self) -> None:
        rows = list(self._active_body().query(".auq-row"))  # options + note row
        for j, row in enumerate(rows):
            row.set_class(j == self._cursor, "cursor")

    # ── navigation ─────────────────────────────────────────────────

    def action_prev_question(self) -> None:
        self._jump(self._active - 1)

    def action_next_question(self) -> None:
        self._jump(self._active + 1)

    def _jump(self, i: int) -> None:
        self._active = i % len(self._states)
        self._cursor = 0
        self.query_one("#auq-question", Static).content = self._states[self._active].question
        for j, body in enumerate(self.query(".auq-qbody")):
            body.set_class(j == self._active, "active")
        self.query_one("#auq-note-input", Input).value = self._states[self._active].note
        self.query_one("#auq-note-input", Input).password = self._states[self._active].secret
        self._refresh_steps()
        self._set_cursor()
        self._focus_body()

    def action_move_up(self) -> None:
        if self.app.focused is not None and self.app.focused.id == "auq-note-input":
            self._focus_body()
        self._cursor = max(0, self._cursor - 1)
        self._set_cursor()

    def action_move_down(self) -> None:
        if self.app.focused is not None and self.app.focused.id == "auq-note-input":
            self._focus_body()
        last = len(self._states[self._active].options)  # the note row's index
        self._cursor = min(last, self._cursor + 1)
        self._set_cursor()

    def action_select(self) -> None:
        q = self._states[self._active]
        if self._cursor == len(q.options):
            # the "Type something" row: hand focus to the note field
            self.query_one("#auq-note-input", Input).focus()
        else:
            self._toggle(self._cursor)

    def _toggle(self, i: int) -> None:
        q = self._states[self._active]
        if i in q.selected:
            q.selected.discard(i)
        else:
            if not q.multi_select:
                q.selected.clear()
            q.selected.add(i)
        self._refresh_all_rows(self._active)
        self._refresh_steps()

    # ── note field ─────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if getattr(event.input, "id", "") != "auq-note-input":
            return
        self._states[self._active].note = event.value
        self._refresh_steps()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter inside the note field is not "select a row" — stay put.
        event.input.focus()

    # ── mouse ──────────────────────────────────────────────────────

    @on(events.Click, ".auq-row")
    def _row_clicked(self, event: events.Click) -> None:
        w = event.widget
        if w is None:
            return
        if not w.id.startswith("auq-opt-"):
            # the "Type something" row: hand focus to the note field
            self._focus_body()
            self.query_one("#auq-note-input", Input).focus()
        else:
            self._cursor = int(w.id.rsplit("-", 1)[1])
            self._toggle(self._cursor)
            self._set_cursor()

    @on(events.Click, ".auq-checkbox")
    def _step_box_clicked(self, event: events.Click) -> None:
        """Untick the step's checkbox: clear that question's answer, jump to it."""
        i = int(event.widget.id.rsplit("-", 1)[1])
        self._states[i].clear()
        self._refresh_all_rows(i)
        self._jump(i)

    @on(events.Click, ".auq-step-label")
    def _step_label_clicked(self, event: events.Click) -> None:
        self._jump(int(event.widget.id.rsplit("-", 1)[1]))

    # ── exit ───────────────────────────────────────────────────────

    def _finish(self, action: str) -> None:
        """Close the widget and wake run() with the full state. Runs once."""
        if self._finished:
            return
        if action == "submit":
            missing = next((i for i, q in enumerate(self._states) if not q.answered), None)
            if missing is not None:
                self._jump(missing)
                self.query_one("#auq-progress", Static).update(
                    f"Question {missing + 1} of {len(self._states)} needs an answer. "
                    "Choose an option or add a note; use Chat about this to discuss."
                )
                return
        self._finished = True
        self._result_box.append({
            "action": action,
            "questions": [q.to_dict() for q in self._states],
        })
        self._done.set()
        close_dialog(self, None)

    def action_cancel(self) -> None:
        self._finish("cancelled")

    @on(Button.Pressed, "#auq-submit")
    def _submit(self) -> None:
        if self._active < len(self._states) - 1:
            self._jump(self._active + 1)
            return
        self._finish("submit")

    @on(Button.Pressed, "#auq-chat")
    def _chat(self) -> None:
        self._finish("chat")


# ── Tool entry point ───────────────────────────────────────────────

#: What the tool returns when nobody ever answered. EXPLICIT, and never an empty
#: or partial answer set: the model must be able to tell "asked and unanswered"
#: from "asked and told nothing", and a caller that cannot tell those apart will
#: proceed as though silence were consent.
UNANSWERED_EXITED = (
    "[ask_user_question] UNANSWERED - LiteTUI exited before the question was "
    "answered. No answers were given; do not assume any option."
)
UNANSWERED_NO_HOST = (
    "[ask_user_question] UNANSWERED - the host closed the question without an "
    "answer. No answers were given; do not assume any option."
)


def _ask_registry(app: App) -> dict:
    """Pending over-the-wire asks for THIS app, keyed by ask id.

    On the app INSTANCE, not a module global. This module's own history is why:
    the old `set_app()` module-global was the one impure hook in the tool set,
    and two apps in one process (the test suite's normal state) crossed wires
    through it. A shared registry would have exactly that hazard again.
    """
    reg = getattr(app, "_rpc_pending_asks", None)
    if reg is None:
        reg = {}
        app._rpc_pending_asks = reg
    return reg


def resolve_over_rpc(app: App, ask_id: str, action: str, answers: object) -> bool:
    """Answer a pending wire ask. False when the id is unknown or already done.

    Called from the rpc dispatch thread; the tool call is parked on the Event
    this sets.
    """
    entry = _ask_registry(app).get(ask_id)
    if entry is None:
        return False
    done, result_box, states = entry
    if done.is_set():
        # A SECOND answer is refused rather than applied. The tool call has
        # already moved on, so a late write into result_box reaches nobody while
        # reporting success to whoever sent it.
        return False

    # A short or malformed answers list is not a reason to fail the call: those
    # questions simply read as unanswered, which _serialize already states per
    # question. Failing here would strand the tool call instead.
    if isinstance(answers, list):
        for i, state in enumerate(states):
            if i >= len(answers):
                break
            a = answers[i]
            if not isinstance(a, dict):
                continue
            picked = a.get("selected")
            if isinstance(picked, list):
                state.selected = {
                    j for j in picked
                    if isinstance(j, int) and 0 <= j < len(state.options)
                }
                if not state.multi_select and len(state.selected) > 1:
                    state.selected = {next(j for j in picked if j in state.selected)}
            note = a.get("note")
            if isinstance(note, str):
                state.note = note

    act = action if action in ("submit", "chat", "cancel") else "submit"
    result_box.append({"action": act, "questions": [s.to_dict() for s in states]})
    done.set()
    return True


def cancel_pending_asks(app: App) -> list[str]:
    """Release every wire ask parked on this app. Returns the ids released.

    🔴 THE PARKED THREAD HAS EXACTLY ONE EXIT AND `abort` WAS NOT IT (T632).
    `_run_over_rpc` blocks on a `threading.Event`; the only things that ever set
    it are `resolve_over_rpc` (an `answer` command) and the app shutting down.
    A host `abort` reached neither, so the tool thread stayed parked, the turn
    never ended, and the host's interrupt could not complete — while the `abort`
    reply said `stopped: true`.

    ⚠️ AND CANCELLING THE WORKER WOULD NOT HAVE DONE IT EITHER. The tool runs
    under `asyncio.to_thread`, whose threads cannot be cancelled from outside;
    the block has to be released at the Event or not at all. That is why this is
    a resolve and not a kill.

    Idempotent by the `is_set` check, so a second abort is a no-op rather than a
    second write into a box nobody is reading.
    """
    released: list[str] = []
    for ask_id, (done, result_box, states) in list(_ask_registry(app).items()):
        if done.is_set():
            continue
        result_box.append(
            {"action": "aborted", "questions": [s.to_dict() for s in states]}
        )
        done.set()
        released.append(ask_id)
    return released


def _run_over_rpc(states: list[QuestionState], app: App) -> str:
    """The headless path: put the question ON THE WIRE and wait for an answer.

    THIS EXISTS BECAUSE THE WIDGET PATH DEADLOCKS HERE. Under `--rpc` the app is
    headless, so pushing a screen puts the question where nobody can answer it
    and the tool call waits out the whole session. Reproduced in
    tests/test_ask_over_rpc_hangs.py BEFORE this was written.

    The polling loop is the same shape as the widget path below, for the same
    reason: `done` is never set when nobody answers, so the `is_running` check is
    the only thing that ever releases this thread.
    """
    from litetui.question_result import current_lifetime, question_origin_fields

    lifetime = current_lifetime()
    ask_id = "ask-" + uuid4().hex[:12]
    done = threading.Event()
    result_box: list[dict] = []
    _ask_registry(app)[ask_id] = (done, result_box, states)
    try:
        app._rpc_emit({
            "type": "user_input_requested",
            "id": ask_id,
            "questions": [s.to_dict() for s in states],
            **question_origin_fields(),
        })
        while not done.wait(timeout=0.1 if lifetime is not None else 5):
            if lifetime is not None and lifetime.is_set():
                if done.is_set():
                    break
                result_box.append({"action": "aborted", "questions": [s.to_dict() for s in states]})
                done.set()
                app._rpc_emit({"type": "user_input_resolved", "id": ask_id, "cancelled": True})
                break
            if not getattr(app, "is_running", True):
                return UNANSWERED_EXITED
        if not result_box:
            return UNANSWERED_NO_HOST
        return _serialize(result_box[0])
    finally:
        _ask_registry(app).pop(ask_id, None)


def run(args: dict, app: App | None) -> str:
    """Dispatch the `ask_user_question` tool. Never raises — every path returns text.

    Runs on a worker thread (app.py dispatches with `asyncio.to_thread`), so
    blocking here is expected, and the Textual loop keeps painting on the
    main thread while Ryan answers. See the module docstring for the bridge.
    """
    try:
        states = _parse_questions(args)
    except ValueError as e:
        return f"[error] ask_user_question: {e}"

    if app is None:
        return (
            "[error] ask_user_question: no running LiteTUI app to render the "
            "question widget"
        )

    # Headless: the widget has nobody to render to. Route to the wire rather
    # than pushing a screen into the dark and waiting out the session.
    if getattr(app, "_rpc", False):
        return _run_over_rpc(states, app)

    from litetui.question_result import current_lifetime

    lifetime = current_lifetime()
    done = threading.Event()
    result_box: list[dict] = []
    loop = getattr(app, "_loop", None)

    # 🔴 THE ANSWER DOES NOT COME BACK THROUGH THE HOST'S FUTURE, AND MUST NOT.
    # This dialog is driven from a WORKER THREAD: the body appends to
    # `result_box` and sets `done`, and the polling loop below is what wakes the
    # tool call. `close_dialog` only tears the view down. Two consequences:
    #
    #   SWAP         -> the view is rebuilt, `done` is NEVER set, the thread
    #                   keeps waiting. Correct: nobody answered.
    #   APP TEARDOWN -> `done` is never set either, so the loop's `is_running`
    #                   check is the ONLY thing that releases the thread.
    #
    # Those two look identical from the host's side and are opposite, which is
    # why the polling loop is carried across UNCHANGED rather than collapsed
    # into an await on the dialog's result.

    def _open() -> None:
        if loop is None:
            # App not running: the direct call fails and becomes the
            # same error string.
            app.push_screen(AskUserQuestionScreen(states, done, result_box))
            return

        async def _push() -> None:
            # app._context() sets the active_app / active_message_pump
            # ContextVars for THIS task (the exact idiom of textual 8.0.2
            # App.call_from_thread and Worker._run). push_screen starts
            # the screen's own message-pump task during the push; that
            # task copies THIS task's context, so it must carry
            # active_app or the screen's compose raises NoActiveAppError.
            #
            # THE SAME CONTEXT REQUIREMENT APPLIES TO THE SIDEBAR PATH: mounting
            # also starts the widget's message pump, so the mount has to happen
            # inside `app._context()` for exactly the same reason.
            with app._context():
                # T222: this used to branch on `sidebar` itself and push the
                # bare screen on the modal side — which is the branch
                # `present_dialog` IS, minus the swap. Opened as a modal there
                # was no way back to the sidebar: the screen carried no
                # controller and nothing watched its exit. Routing it here gives
                # the control both directions without changing either host.
                present_dialog(
                    app,
                    partial(AskUserQuestionBody, states, done, result_box),
                    partial(AskUserQuestionScreen, states, done, result_box),
                )

        # .result() returns once the push is processed on the loop. It bounds
        # THE PUSH, not the answer — the answer is bounded by `done` below.
        asyncio.run_coroutine_threadsafe(_push(), loop).result(timeout=10)

    try:
        _open()
    except Exception as e:
        return (
            f"[error] ask_user_question: could not open the widget: "
            f"{type(e).__name__}: {e}"
        )

    # Block until Submit / Chat / Esc. Poll so a LiteTUI that exits
    # mid-question cannot hang this thread forever.
    while not done.wait(timeout=0.1 if lifetime is not None else 5):
        if lifetime is not None and lifetime.is_set():
            def cancel_owned_question():
                # Match the shared Event, never dismiss an unrelated dialog.
                for screen in app.screen_stack:
                    for body in screen.query(AskUserQuestionBody):
                        if body._done is done:
                            body._finish("aborted")
                            return
            try:
                app.call_from_thread(cancel_owned_question)
            except Exception:  # noqa: BLE001 - teardown must still release the tool thread
                if not done.is_set():
                    result_box.append({"action": "aborted", "questions": [s.to_dict() for s in states]})
                    done.set()
            if not done.is_set():
                result_box.append({"action": "aborted", "questions": [s.to_dict() for s in states]})
                done.set()
            break
        if not getattr(app, "is_running", True):
            return (
                "[error] ask_user_question: LiteTUI exited before the question "
                "was answered — no answers were given"
            )

    return _serialize(result_box[0])
