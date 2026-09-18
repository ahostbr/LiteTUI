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

import re
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
    """The message box. Steals a few keys ONLY while the picker is open,
    and provides up/down history recall when it is closed.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._hist_idx: int = -1
        self._draft: str = ""

    def push_history(self, text: str) -> None:
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._hist_idx = -1
        self._draft = ""

    def on_key(self, event) -> None:
        ac = getattr(self.app, "_skill_ac", None)
        picker_open = ac is not None and ac.display

        if picker_open:
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
            return

        # T570 — THE FOOTER HAS THE KEYBOARD. Checked before history, because
        # while the footer is selected these keys mean something else entirely
        # and falling through would edit the draft the user cannot see.
        app = self.app
        if getattr(app, "_footer_nav", None) is not None:
            if event.key == "left":
                app.footer_nav_move(-1)
            elif event.key == "right":
                app.footer_nav_move(1)
            elif event.key == "enter":
                app.footer_nav_activate()
            elif event.key in ("escape", "up"):
                # Up leaves as well as Escape: the footer is BELOW the input, so
                # "back up to where I was typing" is the direction the hand
                # already means.
                app.footer_nav_leave()
            else:
                return
            event.prevent_default()
            event.stop()
            return

        # Down with nothing newer in history is the hook: it did nothing at all
        # before, so taking it costs no existing behaviour. With history open
        # (`_hist_idx != -1`) Down still walks FORWARD through it and only the
        # last press — the one that would restore the draft — is unchanged.
        if event.key == "down" and self._hist_idx == -1 and hasattr(app, "footer_nav_enter"):
            app.footer_nav_enter()
            if getattr(app, "_footer_nav", None) is not None:
                event.prevent_default()
                event.stop()
            return

        if not self._history:
            return

        if event.key == "up":
            if self._hist_idx == -1:
                self._draft = self.value
                self._hist_idx = len(self._history) - 1
            elif self._hist_idx > 0:
                self._hist_idx -= 1
            else:
                return
            self.value = self._history[self._hist_idx]
            self.cursor_position = len(self.value)
            event.prevent_default()
            event.stop()
        elif event.key == "down" and self._hist_idx != -1:
            if self._hist_idx < len(self._history) - 1:
                self._hist_idx += 1
                self.value = self._history[self._hist_idx]
            else:
                self._hist_idx = -1
                self.value = self._draft
            self.cursor_position = len(self.value)
            event.prevent_default()
            event.stop()


class ChatMessage(Static):
    """A single chat message bubble."""

    pass


class UserMessage(Vertical):
    """User prompt with a compact, manually foldable header; no model call."""

    def __init__(self, text: str, queued: bool = False) -> None:
        super().__init__(classes="user-msg")
        self.body = Static(Text(text))
        self.queued = queued
        preview = " ".join(text.split())
        self.preview = preview if len(preview) <= 80 else preview[:77].rstrip() + "..."
        self.refresh_header()

    def compose(self) -> ComposeResult:
        yield self.body

    @property
    def collapsed(self) -> bool:
        return self.has_class("collapsed")

    def refresh_header(self) -> None:
        marker = "▸" if self.collapsed else "▾"
        label = "You · queued" if self.queued else "You"
        preview = f" · {self.preview}" if self.collapsed and self.preview else ""
        self.border_title = f"{marker} {label}{preview}"

    def set_collapsed(self, value: bool) -> None:
        self.set_class(value, "collapsed")
        self.refresh_header()

    def mark_delivered(self) -> None:
        self.queued = False
        self.refresh_header()

    def on_click(self, event) -> None:
        if event.y == 0:
            event.stop()
            self.set_collapsed(not self.collapsed)


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

    def on_click(self, event) -> None:
        # STOP THE BUBBLE. Click bubbles, and this header now sits inside an
        # AssistantMessage that folds on its own click. Without stop(), one
        # click on "Thinking" would fold the trace AND the whole card - the
        # opposite of the independent inner collapse Ryan asked to preserve.
        event.stop()
        block = self.parent
        if isinstance(block, ThinkingBlock):
            block.set_expanded(not block.expanded)


_BOLD_SPAN = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def reasoning_text(buffer: str, caret: str = "") -> Text:
    """The reasoning trace as Rich Text, with `**heading**` actually bold.

    Codex summary parts are markdown: a bold heading, sometimes a body. The
    block used to render the raw string, so the asterisks were printed --
    "**Planning image tests**" with the stars visible. Styling the span and
    dropping the markers is the whole fix; it is deliberately NOT a markdown
    parser, because reasoning text is arbitrary and Text.from_markup would let
    a stray "[" in it become a Rich tag.

    A part still streaming has no closing `**` yet, so its markers show until
    the delta that closes them lands -- for one repaint, on text that is being
    typed out anyway.
    """
    out = Text()
    at = 0
    for match in _BOLD_SPAN.finditer(buffer):
        out.append(buffer[at : match.start()])
        out.append(match.group(1), style="bold")
        at = match.end()
    out.append(buffer[at:] + caret)
    return out


class ThinkingBlock(Vertical):
    """Collapsible model thinking/reasoning trace (click header to toggle)."""

    def __init__(self) -> None:
        super().__init__(classes="thinking-block expanded")
        self._buffer = ""
        # A ThinkingBlock is only ever constructed on the first
        # reasoning token, so construction IS the trace's start:
        # stamp it here rather than having the app reach in.
        self._t0: float | None = time.monotonic()
        self._toks = 0                 # appended deltas; see _count
        self._chars = 0                # a delta is not a token (spec decoding)
        self._settled: int | None = None   # the server's own count, once known
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
        self._set_header(text)

    def _count(self) -> int:
        """Tokens so far: the server's count once settled, else the delta
        count or the chars/4 estimate, whichever is larger (TpsState has the
        measurement - one delta carries several tokens under spec decoding)."""
        # getattr: the header tests drive a bare double built without
        # __init__, and this must not be the attribute that breaks them.
        settled = getattr(self, "_settled", None)
        if settled:
            return settled
        return max(self._toks, (getattr(self, "_chars", 0) + 3) // 4)

    def settle(self, tokens: int | None) -> None:
        """The server's own token count for this trace, from the round's usage.
        Arrives after freeze on a mixed round (a content token ended the
        thinking) and BEFORE it on a thinking-only round (the stream ends, then
        _thinking_done freezes) - so it is kept either way and the frozen
        readout is redone if it already exists."""
        if not tokens:
            return
        self._settled = int(tokens)
        if self._frozen is not None:
            elapsed = self._frozen[0]
            avg = (self._settled / elapsed) if elapsed > 0 else None
            self._frozen = (elapsed, self._settled, avg)
            self._set_header(self._frozen_text(self._marker))

    def append(self, token: str) -> None:
        self._buffer += token
        self._toks += 1
        self._chars += len(token)
        # This never scrolled the VerticalScroll it owns, so the trace grew
        # below the fold with the viewport pinned at the top. Measure BEFORE
        # the content grows: afterwards we are no longer at the bottom by
        # definition, so the test would always read "not following".
        follow = _at_bottom(self.scroll)
        # NOTE: use the `content` setter, not .update() — in textual 8.0.2
        # update() does not invalidate the content-size cache, so an
        # auto-height parent would freeze at the first (small) height.
        self.text.content = reasoning_text(self._buffer, " \u258c")
        if follow:
            # After the refresh: the scroll extent does not grow until the
            # new content has been re-measured.
            self.call_after_refresh(self.scroll.scroll_end, animate=False)

    def finalize(self) -> None:
        self.text.content = reasoning_text(self._buffer)

    @property
    def source(self) -> str:
        """The raw trace, for a header made from the model's own words."""
        return self._buffer

    # -- header timer ------------------------------------------------------
    #
    # The block stamps its own t0 at construction (see __init__), the
    # app feeds it one repaint tick (repaint_header, with the app's
    # own tps value) and freezes it (freeze_header, inside
    # _thinking_done). The strings are pure (thinking_header_text),
    # so they are testable without a live app; the tps number is the
    # app's single reactive, never a second one.

    def _set_header(self, text) -> None:
        """THE ONE PLACE THIS BLOCK WRITES ITS HEADER, AND SO THE ONE GUARD.

        A ThinkingBlock stamps its own `_t0` at CONSTRUCTION, but its
        children do not exist until Textual composes it -- so any writer
        reached in that gap raises NoMatches on `query_one`.

        🔴 TWO OF THE FOUR WRITERS HAD THE GUARD AND TWO DID NOT, AND ONE OF
        THE TWO RUNS FROM A TIMER. `reset_header` and `freeze_header` each
        carried their own copy with a comment naming the race;
        `repaint_header` -- six lines from one of them, called every 0.25 s
        from the app's `_elapsed_repaint` -- had none, and neither did
        `set_expanded`. Measured on the 35B (a reasoning model, so every
        turn builds one of these): 6 unretrieved
        `textual.css.query.NoMatches` tracebacks in a single 7-minute
        session, from app.py `_elapsed_repaint` -> `repaint_header`.

            A COMMENT THAT STATES A RULE DOES NOT TRANSFER TO THE SIBLING IT
            NEVER NAMES. The rule had to become a function to reach them.

        Deliberately still `except Exception`, not a narrowed QueryError:
        that is the tolerance the two existing guards already chose, and
        narrowing it here would turn something currently swallowed into a
        crash in the repaint timer -- the exact failure this removes.
        """
        try:
            self.query_one(ThinkingHeader).content = text
        except Exception:
            # Not composed yet. A true no-op: compose() renders the header
            # from the same state, and the next tick or toggle paints it.
            pass

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
        self._set_header(thinking_header_text(
            self._marker, self._t0, time.monotonic(), tps, tokens))

    def reset_header(self) -> None:
        """The trace stopped streaming: back to a plain header, no timer.
        The app calls it once, inside _thinking_done (idempotent there)."""
        self._t0 = None
        self._set_header(f"{self._marker} Thinking")

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
        tokens = self._count() or None
        avg = (tokens / elapsed) if (tokens and elapsed > 0) else None
        self._frozen = (elapsed, tokens, avg)
        self._set_header(self._frozen_text(self._marker))
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
        if isinstance(bubble, UserMessage):
            bubble.mark_delivered()
        else:
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
        # No event.stop() here, deliberately. A FoldBlock is only ever mounted
        # into #chat-log (tool cards, app.py:5785/6434, codex_tool_ui.py:63) or
        # into a CompactionCard - never inside an AssistantMessage, which is the
        # only parent that folds on a bubbled click. ThinkingHeader DOES need
        # stop(), because a ThinkingBlock is mounted into the card itself
        # (app.py:5729). Adding it here too was symmetry guarding a nesting that
        # does not exist, and it broke four call sites that invoke the handler
        # directly.
        block = self.parent
        if isinstance(block, FoldBlock):
            block.set_expanded(not block.expanded)


class FoldBlock(Vertical):
    """One collapse mechanism for static payloads and live tool cards.

    It remains collapsed by default for compaction prompts. Subclasses may
    start expanded and override ``_header_content`` without reimplementing the
    marker, class, or body-visibility state machine.
    """

    def __init__(
        self,
        label: str,
        text: str,
        *,
        expanded: bool = False,
        classes: str = "thinking-block",
        body_classes: str = "thinking-body",
    ) -> None:
        if expanded:
            classes += " expanded"
        super().__init__(classes=classes)
        self._label = label
        self.header = _FoldHeader(label)
        self.body = Static(Text(text))
        self.scroll = VerticalScroll(self.body, classes=body_classes)

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.scroll

    @property
    def expanded(self) -> bool:
        return self.has_class("expanded")

    def _header_content(self, marker: str):
        return f"{marker} {self._label}"

    def _refresh_header(self) -> None:
        marker = "\u25be" if self.expanded else "\u25b8"
        self.header.content = self._header_content(marker)

    def set_expanded(self, value: bool) -> None:
        if value:
            self.add_class("expanded")
        else:
            self.remove_class("expanded")
        self._refresh_header()


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

    def record_round(self, timing: dict) -> None:
        """Retain each request/tool duration even after the final ledger replaces status."""
        first = timing["first_chunk_s"]
        first_text = f"{first:.2f}s" if first is not None else "unreported"
        self.mount(Static(
            f"Round {timing['round']}: model {timing['model_s']:.2f}s · "
            f"first chunk {first_text} · tools {timing['tools_s']:.2f}s",
            classes="compaction-round"), before=self.status)

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
    """Assistant bubble - optional thinking, answer, then terminal stop line.

    The WHOLE card folds, like a thinking block (Ryan, 2026-09-16). Two things
    make that unlike the ThinkingBlock next door:

    * The header is the widget's own ``border_title``, not a child row, so it
      survives when every child is hidden. A child header would disappear
      together with the body it exists to re-open.
    * Folding is driven by a POSITIVE ``collapsed`` class. Textual 8.0.2 has no
      ``:not()`` pseudo-class - it raises TokenError naming the nine it does
      accept - so the ``.thinking-block.expanded .thinking-body`` shape cannot
      simply be inverted here.

    ``_autocollapsed`` is a LATCH, not a state flag. Auto-collapse fires at most
    once per card, so a card the reader re-opens by hand is never folded shut
    behind them, and a height change can never feed back into a second collapse.
    """

    def __init__(self) -> None:
        super().__init__(classes="assistant-msg")
        self.thinking: ThinkingBlock | None = None
        self.body = AnswerBody("...", id="answer-body")
        self.stop_line = Static("", classes="turn-stop-line")
        self.stop_line.styles.display = "none"
        self.model_name: str = ""
        self.summary: str | None = None
        self.settled: bool = False        # the turn stopped, however it stopped
        self._autocollapsed: bool = False
        # The VISIBLE answer, kept so the one-line summary can be made from what
        # the reader can see. Deliberately not the reasoning trace: a summary of
        # hidden thinking would describe work the card does not show.
        self.answer_text: str = ""
        self.summary_done: bool = False   # asked once, whatever came back

    def compose(self) -> ComposeResult:
        yield self.body
        yield self.stop_line

    # -- header ------------------------------------------------------------
    #
    # While generating, the header is the MODEL NAME, so a reader can see which
    # model is answering without opening settings. Once a one-line summary
    # lands it becomes "<summary> - <model>". The model name is the fallback at
    # every other moment: summary pending, failed, empty, or cancelled.

    MARK_OPEN = "▾"
    MARK_SHUT = "▸"

    @property
    def collapsed(self) -> bool:
        return self.has_class("collapsed")

    def _header_text(self) -> str:
        marker = self.MARK_SHUT if self.collapsed else self.MARK_OPEN
        label = self.model_name or "AI"
        if self.summary:
            label = f"{self.summary} - {label}"
        return f"{marker} {label}"

    def refresh_header(self) -> None:
        self.border_title = self._header_text()

    def set_model_name(self, name: str | None) -> None:
        self.model_name = (name or "").strip()
        self.refresh_header()

    #: A border title cannot wrap, so an over-long summary would be clipped by
    #: the frame at whatever width the terminal happens to be. Cap it here
    #: instead, where the ellipsis is deliberate. Mirrors ToolMessage.
    MAX_SUMMARY_CHARS = 90

    def set_summary(self, text: str | None) -> None:
        """Bind a one-line summary. Blank leaves the model-name fallback standing.

        Whitespace is collapsed rather than trusted: the header is one line, and
        a model that answers with two would otherwise break the frame.
        """
        text = " ".join((text or "").split())
        if text.lower() in ("none", "none."):   # the prompt's "nothing to say"
            text = ""
        if len(text) > self.MAX_SUMMARY_CHARS:
            text = text[: self.MAX_SUMMARY_CHARS - 1].rstrip() + "…"
        self.summary = text or None
        self.refresh_header()

    def set_answer(self, text: str) -> None:
        """Render the finished answer and keep its source for summarising."""
        self.answer_text = text or ""
        try:
            self.body.set_markdown(text)
        except Exception:
            self.body.content = Text(text)

    def set_collapsed(self, value: bool) -> None:
        if value:
            self.add_class("collapsed")
        else:
            self.remove_class("collapsed")
        self.refresh_header()

    def autocollapse(self) -> bool:
        """Fold once, on the way off screen. True if this call is what folded it."""
        if self._autocollapsed or self.collapsed or not self.settled:
            return False
        self._autocollapsed = True
        self.set_collapsed(True)
        return True

    def on_click(self, event) -> None:
        # y == 0 is the top border row, i.e. the title bar. Anywhere else is
        # the card's content, where a click means "select text", not "fold".
        if event.y == 0:
            event.stop()
            self.set_collapsed(not self.collapsed)

    def set_stop_line(self, text: str | None) -> None:
        """Settle the bubble without putting display text in answer markdown."""
        if not text:
            return
        self.stop_line.content = text
        self.stop_line.styles.display = "block"


class ToolMessage(FoldBlock):
    """A tool card: expanded while live, folded to its summary when complete."""

    MAX_SUMMARY_CHARS = 110

    def __init__(self, name: str) -> None:
        # NOTE: Textual's Widget base owns ``name`` (read-only), so the tool's
        # name lives in ``tool_name``.
        self.tool_name = name
        self._args = ""
        self._result: str | None = None
        self._progress = ""
        self._ok = True
        self._t0 = time.monotonic()
        self._took: float | None = None
        super().__init__(
            name,
            "",
            expanded=True,
            classes="tool-msg",
            body_classes="tool-body",
        )
        # Construction already seeds a plain header. Rich styling arrives on
        # mount's first elapsed tick; assigning Rich Text before mount asks
        # Textual for an app console that does not exist yet.

    @staticmethod
    def _one_line(value: str) -> str:
        return " ".join(value.split())

    def _arg_summary(self) -> str:
        summary = self._one_line(self._args)
        if len(summary) > self.MAX_SUMMARY_CHARS:
            summary = summary[: self.MAX_SUMMARY_CHARS - 3] + "..."
        return summary

    def _header_content(self, marker: str) -> Text:
        color = self._tool_name_color()
        parts = [(f"{marker} \U0001F527 {self.tool_name}", f"bold {color}")]
        summary = self._arg_summary()
        if summary:
            parts.append((f" · {summary}", "#8b95a7"))
        if self._result is None:
            parts.append((f" · {fmt_dur(time.monotonic() - self._t0)} …", "#8b95a7"))
        else:
            status = "done" if self._ok else "error"
            duration = fmt_dur(self._took) if self._took is not None else "duration unknown"
            parts.append((f" · {status} · {duration}", "#5c6470"))
            parts.append((f" · {len(self._result)} chars", "#5c6470"))
        return Text.assemble(*parts)

    def _body_content(self) -> Text:
        out = Text()
        if self._args:
            out.append("Arguments\n", style="bold #8b95a7")
            out.append(self._args)
        if self._result is not None:
            if self._args:
                out.append("\n\n")
            out.append("Result\n", style="bold #8b95a7")
            out.append(self._result, style="bold #e5534b" if not self._ok else "#7d8799")
        elif self._progress:
            out.append("\n\nOutput (running)\n", style="bold #8b95a7")
            out.append(self._progress)
        return out

    def set_progress(self, text: str) -> None:
        if self._result is None:
            self._progress = text
            self._update_display()

    def set_args(self, args_json: str) -> None:
        self._args = args_json
        self._update_display()

    def set_result(self, result: str, ok: bool, *, elapsed: float | None = None,
                   duration_unknown: bool = False) -> None:
        self._result = result
        self._ok = ok
        self._took = None if duration_unknown else time.monotonic() - self._t0 if elapsed is None else elapsed
        self.set_expanded(False)
        self._update_display()

    def _tick(self) -> None:
        """Refresh live elapsed without changing the user's fold state."""
        if self._result is None:
            self._update_display()

    def _tool_name_color(self) -> str:
        """The active theme's $tool-text, or the default when unthemed."""
        try:
            value = self.app.current_theme.variables.get("tool-text")
            return value or TOOL_NAME_DEFAULT
        except Exception:
            return TOOL_NAME_DEFAULT

    def set_expanded(self, value: bool) -> None:
        # A running call stays open so its arguments and live state cannot be
        # hidden. Once a result lands it follows FoldBlock's shared toggle.
        if self._result is None and not value:
            return
        super().set_expanded(value)

    def _update_display(self) -> None:
        self._refresh_header()
        self.body.content = self._body_content()


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


class PaletteButton(Static):
    """The command palette's ONLY route, and the reason it is a mouse target.

    Ryan, liteask a-5d6c1ca0 (2026-09-10 21:3x): "Keep plan on Ctrl+P, move the
    palette -- palette via click only".

    🔴 BEFORE THIS THE PALETTE HAD NO ROUTE AT ALL (T573). Textual opens it
    from COMMAND_PALETTE_BINDING and nothing else -- nothing in this tree calls
    `action_command_palette` or pushes the screen -- so when 9660da1 bound
    ctrl+p to plan mode with priority=True, the binding did not override the
    palette, it DELETED it. Fourteen of the fifteen arms in
    tests/test_command_palette.py kept passing throughout, because they call the
    provider rows directly; only the one that drives the real UI noticed.
    A feature whose every route runs through one keybinding has no route at all
    the day something else claims that key.
    """

    def on_click(self) -> None:
        self.app.action_command_palette()


class ContextFooter(Footer):
    """Textual's Footer plus a live context-window readout on the right."""

    def on_resize(self, _event) -> None:
        """Re-fit after this footer has received its new layout width."""
        palette_width = 12
        buttons = list(self.query(".palette-button"))
        button_width = max((button.size.width for button in buttons), default=0)
        if button_width:
            palette_width = button_width
        self.app._footer_available_width = max(0, self.size.width - palette_width - 1)
        self.call_after_refresh(self.app._refresh_ctx_label)

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
        # AFTER the label, deliberately: two widgets docked to the same
        # edge stack in compose order, so the one yielded LAST sits
        # innermost -- and the label is the one that must keep the far
        # right, where the context readout has always been.
        yield PaletteButton("☰ commands", classes="palette-button")


class LiteTUICommands(Provider):
    """LiteTUI's features in the command palette.

    The stock palette knows five Textual commands and nothing about this
    app — 90% of what LiteTUI does was undiscoverable from the palette before
    these rows existed. (It USED to open on ctrl+p; that key is plan mode since
    T558, and the palette opens from the footer's "commands" button — Ryan,
    liteask a-5d6c1ca0.) Each row
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
