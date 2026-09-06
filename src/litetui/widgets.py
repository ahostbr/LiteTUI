"""The chat-log widgets — every Textual widget the app mounts into #chat-log.

Lifted out of `app.py` under T070 step O0 (batch 2). They move as ONE module
because they reference each other: `ThinkingBlock` <-> `ThinkingHeader`,
`FoldBlock` <-> `_FoldHeader`, and `CompactionCard` composes `AnswerBody`,
`FoldBlock` and `ThinkingBlock`. Splitting them would have meant either a
circular import or a fake boundary.

🔴 `app.py` RE-EXPORTS ALL OF THESE. Every one is referenced inside `app.py`
itself, and most are reached from tests as `from litetui.app import ToolMessage`
or `app_mod.CompactionCard` — 16 outside references to `ToolMessage` alone, 13
to `ThinkingBlock`. The re-export is what makes this a move rather than a break.

⚠️ WHAT THIS BUYS, PRECISELY: `app.py` gets shorter. The API surface is
unchanged, `LiteTUI`'s method count is unchanged, and plugin reach-through is
unchanged. See PLAN.md §6 — a line count is one metric of three.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, fields as fields_of
from functools import partial
from litetui.fmt import fmt_dur
from litetui.textfmt import (  # noqa: F401  (re-exported for existing callers)
    TOOL_NAME_DEFAULT,
    _markdown_to_text,
    is_reliable_rate_sample,
    load_prompt,
    memory_prompt,
    midturn_action,
    render_progress,
    thinking_header_text,
    tool_display_parts,
    tps_text,
)
from litetui import plugins as plugins_mod
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
# Imported for the BODY's exits. side_panel imports nothing from widgets, so
# this does not close a cycle — checked rather than assumed.
from litetui.side_panel import SwapButton, close_dialog
from textual.widgets import (
    Button, Footer, Header, Input, OptionList, Static, )
from textual.widgets.option_list import Option
from textual import work, on
from rich.text import Text


@dataclass(frozen=True)
class Completion:
    """One row the slash picker can offer: an app COMMAND, or a skill.

    `sort` is precomputed and total, so refilter can order a MIXED list with a
    single comparison — commands in palette order, then skills alphabetically.
    Building it at construction is what lets two orderings that are not
    comparable to each other live in one list.
    """

    name: str
    description: str
    kind: str            # "command" | "skill"
    sort: tuple


class SkillAutocomplete(Vertical):
    """Slash names, rising from the message area as one is typed: the app's own
    COMMANDS and the skills library, in one list.

    The id and class name still say "skill" because the CSS and the tests bind
    to them; the contents are both kinds. That name is not cosmetic history —
    it is what the bug was. The widget did exactly what it was called, so
    /settings /convos /model and ~20 others were invisible to the one
    affordance built for finding them.

    Deliberately NOT a modal. A modal takes focus, which would stop the typing
    that is doing the filtering -- the whole interaction is "keep typing and
    watch the list narrow", so the Input must never lose focus. That is also
    why the keys are intercepted on the Input rather than bound here.
    """

    def __init__(self) -> None:
        super().__init__(id="skill-ac")
        self.options = OptionList(id="skill-ac-list")
        self._matches: list = []

    def compose(self) -> ComposeResult:
        yield self.options

    def refilter(self, candidates, fragment: str) -> bool:
        """Narrow to `fragment`. Returns whether anything survived.

        A PREFIX match sorts above a mere substring one: typing "ls-a" should
        offer ls-arch before something that merely contains the letters, and
        the first row is what Tab takes. Within equal prefix quality the
        candidate's own `sort` decides, which is what puts COMMANDS ahead of
        skills — they are the app's own surface and they always exist, while a
        skills library may be empty.
        """
        want = (fragment or "").lower()
        hits = [c for c in candidates if want in c.name.lower()]
        hits.sort(key=lambda c: (not c.name.lower().startswith(want), c.sort))
        self._matches = hits[:200]

        self.options.clear_options()
        for c in self._matches:
            desc = " ".join((c.description or "").split())
            # Labelled, because the user has to be able to tell a command from
            # a skill WITHOUT running it. Fixed-width so the column does not
            # ripple as the list narrows.
            tag = "[command]" if c.kind == "command" else "[skill]"
            self.options.add_option(Option(f"{c.name:<26}{tag:<11}{desc[:40]}"))
        if self._matches:
            self.options.highlighted = 0
        self.display = bool(self._matches)
        return bool(self._matches)

    def move(self, delta: int) -> None:
        if not self._matches:
            return
        cur = self.options.highlighted or 0
        self.options.highlighted = max(0, min(len(self._matches) - 1, cur + delta))

    def current(self) -> str | None:
        """The name Tab would take, or None when nothing is showing."""
        if not self._matches or not self.display:
            return None
        i = self.options.highlighted or 0
        if not (0 <= i < len(self._matches)):
            return None
        return self._matches[i].name

    def dismiss_list(self) -> None:
        self.display = False
        self._matches = []


class PromptInput(Input):
    """The message box. Steals a few keys ONLY while the picker is open.

    Tab, up and down all belong to Input normally, so they are intercepted
    here and released the moment the list is hidden -- a widget that keeps a
    key it does not need is how tab-to-focus silently disappears.
    """

    def on_key(self, event) -> None:
        ac = getattr(self.app, "_skill_ac", None)
        if ac is None or not ac.display:
            return
        if event.key == "tab":
            self.app.accept_skill_completion()
        elif event.key == "down":
            ac.move(1)
        elif event.key == "up":
            ac.move(-1)
        elif event.key == "escape":
            ac.dismiss_list()
        else:
            return
        event.prevent_default()
        event.stop()


class ChatMessage(Static):
    """A single chat message bubble."""

    pass


def _at_bottom(widget, slack: int = 2) -> bool:
    """Is this scrollable already parked at (or within `slack` lines of) the end?

    The whole point of autoscroll here is to FOLLOW a stream, and the whole
    danger is yanking a reader who has scrolled up to read something. Both call
    sites ask this first, so following happens only when the reader was already
    following.

    `slack` exists because scroll_y is a float and a stream lands fractions of a
    line at a time; requiring exact equality would drop out of follow mode after
    the first token and never return. Fails OPEN (returns True) if the widget
    does not expose scroll geometry -- an over-eager scroll is a visual nit, a
    silently dead autoscroll is the bug being fixed.
    """
    try:
        return widget.scroll_y >= widget.max_scroll_y - slack
    except Exception:
        return True


class CancelToolButton(Static):
    """Kill ONE running tool's bash TREE, keep the turn. Mounted beside that
    tool's own elapsed timer.

    It used to float on the header layer at the top-left, which said nothing
    about WHICH tool it would kill -- the control and the thing it acts on were
    at opposite ends of the screen. It is now mounted directly after the
    ToolMessage it belongs to, next to the timer that is counting that call up.

    One instance per in-flight tool, so it carries a CLASS, not an id: an id
    must be unique and there can be several tools running at once.

    Hidden unless a cancellable subprocess is actually running — a control
    that is visible and does nothing is a lie, and this repo has shipped that
    class of control before. Visibility is driven by the same repaint tick
    that animates the elapsed timers, so it needs no timer of its own.
    """

    def __init__(self) -> None:
        super().__init__(" ✕ cancel tool ", classes="cancel-tool")

    def on_click(self) -> None:
        self.app.action_cancel_tool()


class ThinkingHeader(Static):
    """Clickable header row that toggles the parent thinking block."""

    def __init__(self) -> None:
        super().__init__("\u25be Thinking", classes="thinking-header")

    def on_click(self) -> None:
        block = self.parent
        if isinstance(block, ThinkingBlock):
            block.set_expanded(not block.expanded)


class ThinkingBlock(Vertical):
    """Collapsible model thinking/reasoning trace (click header to toggle)."""

    def __init__(self) -> None:
        super().__init__(classes="thinking-block expanded")
        self._buffer = ""
        # A ThinkingBlock is only ever constructed on the first
        # reasoning token, so construction IS the trace's start:
        # stamp it here rather than having the app reach in.
        self._t0: float | None = time.monotonic()
        self._toks = 0                 # appended tokens; freeze_header's own count
        self._frozen: tuple | None = None   # (elapsed, tokens, avg) once done
        self._marker = "\u25be"      # expand glyph, kept in sync by set_expanded
        self.text = Static("", id="thinking-text")
        self.scroll = VerticalScroll(self.text, classes="thinking-body")

    def compose(self) -> ComposeResult:
        yield ThinkingHeader()
        yield self.scroll

    @property
    def expanded(self) -> bool:
        return self.has_class("expanded")

    def set_expanded(self, value: bool) -> None:
        if value:
            self.add_class("expanded")
        else:
            self.remove_class("expanded")
        marker = "\u25be" if value else "\u25b8"
        self._marker = marker
        # A finished block keeps its readout across toggles (Ryan): the frozen
        # stats re-render with the new marker instead of being wiped to a label.
        text = self._frozen_text(marker) or f"{marker} Thinking"
        self.query_one(ThinkingHeader).content = text

    def append(self, token: str) -> None:
        self._buffer += token
        self._toks += 1
        # This never scrolled the VerticalScroll it owns, so the trace grew
        # below the fold with the viewport pinned at the top. Measure BEFORE
        # the content grows: afterwards we are no longer at the bottom by
        # definition, so the test would always read "not following".
        follow = _at_bottom(self.scroll)
        # NOTE: use the `content` setter, not .update() — in textual 8.0.2
        # update() does not invalidate the content-size cache, so an
        # auto-height parent would freeze at the first (small) height.
        self.text.content = Text(self._buffer + " \u258c")
        if follow:
            # After the refresh: the scroll extent does not grow until the
            # new content has been re-measured.
            self.call_after_refresh(self.scroll.scroll_end, animate=False)

    def finalize(self) -> None:
        self.text.content = Text(self._buffer)

    # -- header timer ------------------------------------------------------
    #
    # The block stamps its own t0 at construction (see __init__), the
    # app feeds it one repaint tick (repaint_header, with the app's
    # own tps value) and freezes it (freeze_header, inside
    # _thinking_done). The strings are pure (thinking_header_text),
    # so they are testable without a live app; the tps number is the
    # app's single reactive, never a second one.

    def repaint_header(self, tps: float | None,
                       tokens: int | None = None) -> None:
        """One repaint tick from the app's _elapsed_repaint loop. tps and
        tokens arrive as ARGUMENTS - the block never reaches for the app
        itself, so this stays testable on a bare double.

        `tokens` is the reasoning-delta count for the turn (T079). Same
        discipline as tps: passed in, never fetched, and defaulted so every
        existing caller and double keeps working untouched."""
        if self._t0 is None:
            return
        self.query_one(ThinkingHeader).content = thinking_header_text(
            self._marker, self._t0, time.monotonic(), tps, tokens)

    def reset_header(self) -> None:
        """The trace stopped streaming: back to a plain header, no timer.
        The app calls it once, inside _thinking_done (idempotent there)."""
        self._t0 = None
        try:
            self.query_one(ThinkingHeader).content = f"{self._marker} Thinking"
        except Exception:
            # Not composed yet — a fast stream (compaction's tool-call chunk
            # right behind the first reasoning token) can end the trace before
            # the block's children exist. A pre-compose reset is a true no-op:
            # compose() renders the header with exactly this text anyway.
            pass

    def _frozen_text(self, marker: str) -> str | None:
        """The finished readout with `marker` applied, or None while live.
        Rendered through thinking_header_text on a synthetic (0.0, elapsed)
        pair — the function only ever uses now - t0, so the string is exactly
        the live one, just re-marked for the toggle."""
        if self._frozen is None:
            return None
        elapsed, tokens, avg = self._frozen
        return thinking_header_text(marker, 0.0, elapsed, avg, tokens)

    def freeze_header(self) -> None:
        """The trace stopped streaming: keep the readout instead of throwing it away.
        One final repaint with `now` pinned to this moment — elapsed is exactly
        the thinking duration, and the rate is that phase's own tokens / time
        (Ryan: total time, thinking tokens, avg tok/s). Same string as live, just
        frozen; stops the timer like reset_header. The app calls it once inside
        _thinking_done; a pre-compose freeze keeps its numbers for set_expanded."""
        if self._t0 is None:
            return
        elapsed = time.monotonic() - self._t0
        tokens = self._toks or None
        avg = (tokens / elapsed) if (tokens and elapsed > 0) else None
        self._frozen = (elapsed, tokens, avg)
        try:
            self.query_one(ThinkingHeader).content = self._frozen_text(self._marker)
        except Exception:
            # Not composed yet — same no-op class as reset_header's guard. The
            # numbers are kept either way; the next toggle paints them.
            pass
        self._t0 = None


def _mark_delivered(item: dict) -> None:
    """A queued bubble stops saying "queued" once the model can see it.

    The title was the only signal that a message was waiting, so leaving it
    after delivery is the same lie pointing the other way.

    Module-level, not a method: it touches only `item`, and as a method it
    forced every test double of the flush path to grow an unrelated attribute
    just to be called. A helper that constrains its callers' shape for no
    reason is a tax on every future test.
    """
    bubble = item.get("bubble")
    if bubble is None:
        return
    try:
        bubble.border_title = "You"
    except Exception:
        pass


class AnswerBody(Static):
    """The answer text. Rendered as Markdown, and still selectable.

    Textual extracts selected text via Widget.get_selection(), which returns
    None unless the rendered visual is a Text or Content. A Rich `Markdown`
    renderable is neither, so the finished answer could not be highlighted or
    copied — while the thinking block and tool output, both plain text, could.

    Rather than give up the Markdown rendering, extract from a PLAIN-TEXT render
    taken at the SAME WIDTH. Matching the width is not an optimisation: the
    selection offsets Textual hands us are positions in what is on SCREEN, so
    extracting from the raw markdown source would silently return text from a
    different place.
    """

    #: Remembered so a resize can re-render at the new width.
    _markdown_source: str = ""

    def set_markdown(self, src: str) -> None:
        """Show `src` as markdown, in a form Textual can select.

        Assigning `Markdown(src)` directly is what broke selection: Textual
        creates a selection only for a widget whose visual is a Text/Content,
        so the answer became inert the moment the turn finished.
        """
        self._markdown_source = src
        self.content = _markdown_to_text(src, self._render_width())

    def _render_width(self) -> int:
        return self.content_region.width or self.size.width or 80

    def on_resize(self, _event) -> None:
        # Re-wrap at the new width. Without this the answer keeps the column
        # count it was born with and looks broken after a pane resize.
        if self._markdown_source:
            self.content = _markdown_to_text(self._markdown_source, self._render_width())


class _FoldHeader(Static):
    """Clickable header for a FoldBlock."""

    def __init__(self, label: str) -> None:
        super().__init__(f"\u25b8 {label}", classes="thinking-header")
        self.label = label

    def on_click(self) -> None:
        block = self.parent
        if isinstance(block, FoldBlock):
            block.set_expanded(not block.expanded)


class FoldBlock(Vertical):
    """A collapsible static payload — ThinkingBlock's shape minus the timer.

    COLLAPSED by default, which is the difference in kind: a thinking trace
    is watched as it streams, while this holds content whose default view is
    the fold line itself (the compaction prompt: present for inspection,
    not for re-reading on every compact). Reuses the thinking-block CSS so
    the two fold identically.
    """

    def __init__(self, label: str, text: str) -> None:
        super().__init__(classes="thinking-block")     # no 'expanded' class
        self._label = label
        self.header = _FoldHeader(label)
        self.scroll = VerticalScroll(Static(Text(text)), classes="thinking-body")

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.scroll

    @property
    def expanded(self) -> bool:
        return self.has_class("expanded")

    def set_expanded(self, value: bool) -> None:
        if value:
            self.add_class("expanded")
        else:
            self.remove_class("expanded")
        marker = "\u25be" if value else "\u25b8"
        self.header.content = f"{marker} {self._label}"


class CompactionCard(Vertical):
    """The glass box. Every CLI treats compaction as a spinner and a prayer;
    this renders the whole act in the grammar the user already reads — the
    exact prompt (folded), the model's thinking, the summary STREAMING in,
    every store write as a real tool card, and a ledger at the end. Nothing
    about a compaction is secret; it was only ever undisplayed.
    """

    def __init__(self, plan: str, prompt_text: str, auto: bool = False) -> None:
        super().__init__(classes="compaction-card")
        self.auto = auto
        # Construction IS the start of the compaction, so stamp t0 here rather
        # than having the worker reach in -- same rule ThinkingBlock follows.
        self._t0 = time.monotonic()
        self._took: float | None = None
        self._title = Static(self._title_text(), classes="compaction-title")
        self._plan = Static(plan, classes="compaction-plan")
        self.prompt_fold = FoldBlock("Compaction prompt", prompt_text)
        self.body = AnswerBody("")
        self.status = Static("", classes="compaction-status")
        self.thinking: ThinkingBlock | None = None

    def _title_text(self) -> Text:
        """Title with the elapsed clock. Frozen once _took is set, so the card
        keeps reporting how long it actually took instead of resetting to zero."""
        elapsed = self._took if self._took is not None else (time.monotonic() - self._t0)
        stamp = fmt_dur(elapsed) if self._took is not None else f"{fmt_dur(elapsed)} \u2026"
        return Text.assemble(
            ("\U0001F5DC Compaction", "bold"),
            (f" \u00b7 {stamp}", "dim"),
            (" \u00b7 automatic", "dim") if self.auto else ("", ""),
        )

    def tick(self) -> None:
        """One repaint from the app's shared elapsed loop; no-op once settled."""
        self._title.content = self._title_text()

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._plan
        yield self.prompt_fold
        yield self.body
        yield self.status

    def think(self, token: str) -> None:
        if self.thinking is None:
            self.thinking = ThinkingBlock()
            self.mount(self.thinking, before=self.body)
            # THE SECOND MOUNT SITE. _scroll_down's docstring says a new thinking
            # block scrolls unconditionally, and the streaming path does exactly
            # that -- but a block created through here never scrolled at all, so
            # the fix was only ever wired at one of the two places that mount one.
            # Deferred for the same reason as the streaming site: measure, then scroll.
            self.app.call_after_refresh(self.app._scroll_down)
        self.thinking.append(token)

    def thinking_done(self) -> None:
        if self.thinking is not None:
            self.thinking.finalize()
            # freeze, don't reset — the card's path never ticks _tps, so the
            # block's own count is the only source for its stats.
            self.thinking.freeze_header()

    def add_tool(self, msg: "ToolMessage") -> None:
        self.mount(msg, before=self.status)

    def set_status(self, text: str) -> None:
        self.status.content = Text(text)

    def finish(self, ledger: str) -> None:
        self._took = time.monotonic() - self._t0
        self.tick()
        self.status.content = Text(ledger)
        self.add_class("done")

    def fail(self, reason: str) -> None:
        self._took = time.monotonic() - self._t0
        self.tick()
        self.status.content = Text(reason, style="bold red")
        self.add_class("failed")


class AssistantMessage(Vertical):
    """Assistant bubble — optional thinking block above the answer body."""

    def __init__(self) -> None:
        super().__init__(classes="assistant-msg")
        self.thinking: ThinkingBlock | None = None
        self.body = AnswerBody("...", id="answer-body")

    def compose(self) -> ComposeResult:
        yield self.body


class ToolMessage(Static):
    """A single tool call (name + streamed args) and its display-truncated result."""

    MAX_DISPLAY_LINES = 12

    def __init__(self, name: str) -> None:
        super().__init__(Text.assemble((f"\U0001F527 {name}", "bold #e8a33d")), classes="tool-msg")
        # NOTE: Textual's Widget base class owns `name` (read-only property),
        # so the tool's name lives in `tool_name`.
        self.tool_name = name
        self._args = ""
        self._result: str | None = None
        self._ok = True
        # Elapsed timing: t0 at creation, _took settled in set_result.
        self._t0 = time.monotonic()
        self._took: float | None = None

    def set_args(self, args_json: str) -> None:
        self._args = args_json
        self._update_display()

    def set_result(self, result: str, ok: bool) -> None:
        self._result = result
        self._ok = ok
        self._took = time.monotonic() - self._t0
        self._update_display()

    def _tick(self) -> None:
        """Live elapsed repaint while the call is still running; no-op once the
        result has landed. Driven by the app's shared elapsed repaint task."""
        if self._result is None:
            self._update_display()

    # NOTE: must NOT be named `_render` — Textual's Widget._render() is an
    # internal method that must return a Visual; shadowing it breaks layout.
    def _tool_name_color(self) -> str:
        """The active theme's $tool-text, or the default when unthemed.

        Best-effort on purpose: a ToolMessage is constructed in tests with no
        app attached, and a colour lookup must never be the reason a tool card
        fails to render."""
        try:
            v = self.app.current_theme.variables.get("tool-text")
            return v or TOOL_NAME_DEFAULT
        except Exception:
            return TOOL_NAME_DEFAULT

    def _update_display(self) -> None:
        self.content = Text.assemble(
            *tool_display_parts(self, self.MAX_DISPLAY_LINES, self._tool_name_color())
        )


class ConfirmStopBody(Widget):
    """The Yes/No content, host-agnostic so it works in a modal OR a sidebar.

    🔴 THE SCREEN BELOW IS NOT REPLACED, AND THAT IS THE WHOLE DESIGN. Two things
    in this codebase bind to `ConfirmStop` being a real, distinct `ModalScreen`:

        app.py CSS      `ConfirmStop, PickerScreen, HelpScreen, SettingsScreen {`
                        — the centering rule, asserted by test_modal_centering
        test_modals.py  `assert isinstance(e.screen, m.ConfirmStop)`

    Swapping the modal path onto the generic `_ModalHost` would have changed what
    the modal IS while the commit message said it only ADDED a sidebar. So the
    content moved down here, `ConfirmStop` composes it, and the modal path is
    byte-identical to what it always was.

    Exits go through `close_dialog`, which resolves the sidebar controller when
    there is one and falls back to `screen.dismiss` when the host is this
    screen — so one body, both hosts, no branch in the handlers.
    """

    # `width: auto` so the screen's `align: center middle` centres the BOX and
    # not a full-width wrapper. See PickerBody for the full account; this body
    # has the same screen-centres-its-child shape and the same defect.
    DEFAULT_CSS = """
    ConfirmStopBody { width: auto; height: auto; layout: vertical; }
    """

    BINDINGS = [
        Binding("escape", "answer_no", "No", show=False),
        Binding("n", "answer_no", "No", show=False),
        Binding("y", "answer_yes", "Yes", show=False),
        Binding("enter", "answer_yes", "Yes", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static("Stop the agent's turn?", id="confirm-title")
            yield Static(
                "Whatever it has already written is kept.\n"
                "Y / Enter = stop      N / Esc = keep going",
                id="confirm-sub",
            )
            yield SwapButton()
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes, stop", variant="error", id="yes")
                yield Button("No, keep going", variant="primary", id="no")

    # ── state carry across a live host swap ──────────────────────────────────
    def get_state(self) -> dict:
        """Almost nothing to carry — but which button was focused is real, and
        losing it would move focus onto 'Yes, stop' mid-decision."""
        focused = self.screen.focused if self.screen is not None else None
        return {"focused_id": getattr(focused, "id", None)}

    def set_state(self, state: dict) -> None:
        fid = state.get("focused_id")
        if fid:
            try:
                self.query_one(f"#{fid}").focus()
            except Exception:
                pass

    def action_answer_yes(self) -> None:
        close_dialog(self, True)

    def action_answer_no(self) -> None:
        close_dialog(self, False)

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        close_dialog(self, True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        close_dialog(self, False)


class ConfirmStop(ModalScreen[bool]):
    """Yes/No before interrupting a running turn.

    pi binds escape straight to `app.interrupt` and aborts with no confirmation
    (keybindings.ts:78, "Cancel or abort"), showing only an `esc to interrupt`
    hint in the spinner. Ryan asked for a confirmation here instead: a local 27B
    turn is slow and expensive enough that losing one to a stray escape costs
    more than the extra keypress. Escape inside the dialog answers No, so the
    accidental-escape case is a no-op rather than a lost turn.
    """

    # The content lives in ConfirmStopBody so the sidebar host can mount the
    # SAME widget. Bindings stay here too: a ModalScreen is what has focus when
    # the dialog opens as a modal, so `y`/`n`/Esc must resolve at this level.
    BINDINGS = [
        Binding("escape", "answer_no", "No", show=False),
        Binding("n", "answer_no", "No", show=False),
        Binding("y", "answer_yes", "Yes", show=False),
        Binding("enter", "answer_yes", "Yes", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield ConfirmStopBody()

    def action_answer_yes(self) -> None:
        self.dismiss(True)

    def action_answer_no(self) -> None:
        self.dismiss(False)


class ContextFooter(Footer):
    """Textual's Footer plus a live context-window readout on the right."""

    def compose(self) -> ComposeResult:
        yield from super().compose()
        # CLASS, not id. Footer recomposes (Textual removes its children and
        # re-runs compose), and a fixed `id` on a recomposed child raises
        # DuplicateIds the moment the removal has not landed before the mount.
        # That crashed the whole app; duplicate CLASSES are legal, so the worst
        # case degrades to a stale label instead of a traceback.
        label = Static("", classes="ctx-label")
        app = self.app
        if hasattr(app, "ctx_label_text"):
            label.content = app.ctx_label_text
        yield label


class LiteTUICommands(Provider):
    """LiteTUI's features in the command palette.

    The stock palette knows five Textual commands and nothing about this
    app — 90% of what LiteTUI does was undiscoverable from ctrl+p. Each row
    here carries the SAME command string the dispatcher handles, invoked
    through the same `_handle_command` the keyboard uses, so the palette can
    never grow behaviour of its own. A drift test walks this table against
    the dispatcher source; a renamed command breaks the test, not the row.
    """

    def _commands(self):
        app = self.app

        def cmd(command: str):
            return partial(app._handle_command, command)

        # DERIVED from the registry — the one place commands are declared.
        # A hand-authored second table is the drift class this replaced.
        rows = []
        seen: set[int] = set()
        for entry in app.plugins.commands.values():
            if entry.palette is None or id(entry) in seen:
                continue
            seen.add(id(entry))
            rows.append((
                plugins_mod.palette_sort_key(entry.group, entry.order, entry.palette),
                entry.group,
                entry.palette,
                # The slash command is DERIVED, never written into the help
                # string. It used to be typed inline as "(/compact)", which put
                # a machine token in the middle of a sentence a newcomer was
                # trying to read -- and gave the text a second chance to drift
                # from the command it names.
                self._describe(entry.help, entry.tokens[0]),
                cmd(entry.tokens[0]),
            ))
        for r in app.plugins.palette_rows:
            rows.append((
                plugins_mod.palette_sort_key(r.group, r.order, r.title),
                r.group,
                r.title,
                self._describe(r.help, r.tag or None),
                r.run,
            ))
        rows.sort(key=lambda row: row[0])

        # The group name leads the title. Textual's palette has no section
        # headers, so this is what makes the grouping visible -- and it makes
        # search BETTER rather than worse: typing "backend" now surfaces the
        # whole family together instead of one row that happens to say it.
        labels = plugins_mod.PALETTE_GROUP_LABELS
        return [
            (f"{labels.get(group, group.title())}  \u203a  {title}", help_text, run)
            for _key, group, title, help_text, run in rows
        ]

    @staticmethod
    def _describe(help_text: str, token: str | None) -> str:
        """Plain sentence first, the slash command as a trailing tag."""
        text = (help_text or "").strip()
        if not token:
            return text
        return f"{text}   {token}" if text else token

    async def discover(self) -> Hits:
        """The list shown before any query is typed — full feature roll."""
        for title, help_text, run in self._commands():
            yield DiscoveryHit(title, run, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for title, help_text, run in self._commands():
            score = matcher.match(title)
            if score > 0:
                yield Hit(score, matcher.highlight(title), run, help=help_text)
