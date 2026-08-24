"""LiteTUI — a terminal chat client and agent harness for a local LM Studio model."""

import asyncio
import base64
import io
import json
import os
import re
import subprocess
import tempfile
import statistics
import time
import uuid
from datetime import datetime
from pathlib import Path

from litetui import harness as harness_mod
from dataclasses import dataclass, fields as fields_of
from functools import partial

from litetui import settings as settings_mod
from litetui.settings import Settings

from litetui import llm_backend
from litetui import paths
from litetui import prompt_compiler
from litetui.conversation import (
    CONVO_SEED_FILES,
    TRANSCRIPT_NAME,
    ConversationRepository,
)
from litetui import ttyguard
from litetui import mcp_client
from litetui import sanitize
from litetui.fmt import fmt_dur
from litetui import scheduler as sched_mod
from litetui import tool_context
from litetui import tool_policy
from litetui.turn_engine import TurnEngine
from litetui import themes as themes_mod
from litetui.colorpicker import ColorPickerScreen  # noqa: F401 — CSS binds by class name
from litetui.tool_approval import ToolApprovalScreen
from litetui import skills as skills_mod
from litetui import plugins as plugins_mod

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Footer, Header, Input, OptionList, Static, )
from textual.widgets.option_list import Option
from textual.worker import WorkerState
from textual import work, on
from openai import AsyncOpenAI
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_DIM = 1536
# The path anchors live in paths.py — one owner; test_paths.py proves the
# resolution. MARK_SCRIPT stays here: it is /mark's fact, not a store's.
MARK_SCRIPT = paths.ROOT / "tools" / "pccontrol" / "marker_overlay.ps1"


def load_prompt(name: str, **variables: object) -> str:
    """A prompt from prompts/<name>.md, with {name} placeholders substituted.

    Replacement, not str.format(): these files are meant to be EDITED, and a
    stray brace in hand-edited prose must not crash the app — only the
    placeholders that are actually passed get touched.

    A missing file raises FileNotFoundError naming the path, at import time
    for the module-level prompts — a prompt that silently loads empty would
    be a model quietly running without its instructions, which is worse than
    not booting.
    """
    text = (paths.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    for key, value in variables.items():
        text = text.replace("{" + key + "}", str(value))
    return text

#: Marks an already-injected store block inside the system message. Detection
#: by MARKER rather than a flag is what makes /resume correct: a flag lives in
#: memory and dies with the process; the marker is persisted with the message.
STORE_HEADER = "## Your store, loaded once at the start of this conversation"


# LM Studio's accepted set, read out of its own 400 body rather than the docs
# (which omit reasoning_effort entirely):
#   Invalid 'reasoning_effort' value: 'x'. Supported values: none, minimal,
#   low, medium, high, xhigh.

# 🔴 A LEVEL THIS SERVER ACCEPTS IS NOT A LEVEL THE LOADED MODEL ACCEPTS, AND THE
# DIFFERENCE IS SILENT. The set above came from LM Studio's generic 400 body. A
# model loaded as a VIRTUAL MODEL carries its OWN narrower set, and a value
# outside it is DROPPED with a 200 rather than refused with a 400. Measured
# 2026-08-19 in LM Studio's own log while this app sent reasoning_effort=none:
#
#   Reasoning setting 'off' is not a valid option for reasoning level field
#   'ext.virtualModel.customField.qwen.qwen3.827b.reasoningEffort'.
#   Valid options are: xhigh, medium, low. Skipping this field.
#
# "Skipping this field" is the whole problem: the request succeeds, so `/think
# off` reports success, and the model then reasons at the SERVER DEFAULT (xhigh)
# -- the most expensive setting there is, on the box with the least context to
# spare, precisely when the user asked for the least.
#
# /api/v0/models does not expose the field, so this cannot be validated up
# front. It IS detectable in band, which is what _warn_reasoning_ignored does:
# if the user asked for `off` and a reasoning trace arrives anyway, the field
# was dropped. Detect the MECHANISM (a trace exists), never the string.
LEAST_THINKING_FALLBACK = "low"

# How many trailing messages /compact keeps verbatim after the summary.
COMPACT_KEEP_RECENT = 4

# How many tool round-trips /compact may take while persisting to the store.
COMPACT_MAX_TOOL_ITERS = 8

# How long a COMPACTION waits for a model that is still loading before giving
# up. The backend's own ceiling is llm_backend.LOAD_TIMEOUT_S = 300s, which is
# right for a turn someone is watching and wrong here: a compaction holds the
# exclusive chat group, so the user's next message queues behind it for as long
# as it waits. A real model load takes far longer than this bound, so the short
# value is deliberate — "not now, try again after the next turn", not a race we
# are hoping to win. The turn path keeps the full 300s on purpose.
COMPACT_READY_TIMEOUT_S = 10

# How often a CONTINUOUS glass-box channel may fire. thinking and output arrive
# once per token and their intensity is tok/s, which cannot meaningfully change
# between two tokens — so emitting per token floods an observer to tell it the
# same thing hundreds of times. Discrete channels (a tool call, a store write, a
# ledger) are never throttled: dropping one loses the event itself.
GLASSBOX_MIN_INTERVAL_S = 0.2

# Loaded from prompts/compact.md — edit the FILE; it is read at import.
COMPACT_PROMPT = load_prompt("compact")

# The post-compaction ping. User role on purpose: it is the nudge that says
# "keep going", and the standing-by exit is what keeps the model from
# inventing a task to resume when there was none - a ping without an exit
# would cost a full turn every time someone compacted just to free context.
WAKE_AFTER_COMPACT = load_prompt("wake-after-compact")

def memory_prompt(convo_id: str, folder: Path) -> str:
    """The block appended to the system prompt so the agent can find its own
    store. Body lives in prompts/conversation-store.md — read PER CALL, so
    edits take effect on the next conversation without a restart."""
    return load_prompt(
        "conversation-store",
        convo_id=convo_id,
        store_path=str(folder).replace("\\", "/"),
    )


# ════════════════════════════════════════════════════════════════
# Agent tools (pi-style): bash, read, write, web_fetch
# Limits mirror pi's defaults (2000 lines / 50KB).
# ════════════════════════════════════════════════════════════════

# Fallback only. The live value is self.settings.tool_iterations, editable in
# /settings; env LM_TOOL_ITERS still wins over both (see settings.ENV_OVERRIDES).
# This constant remains so module-level users and tests keep a sane number.
TOOL_MAX_ITERATIONS = int(os.environ.get("LM_TOOL_ITERS", "48"))


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
        self.query_one(ThinkingHeader).content = f"{marker} Thinking"

    def append(self, token: str) -> None:
        self._buffer += token
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
    # own tps value) and stops it (reset_header, inside
    # _thinking_done). The strings are pure (thinking_header_text),
    # so they are testable without a live app; the tps number is the
    # app's single reactive, never a second one.

    def repaint_header(self, tps: float | None) -> None:
        """One repaint tick from the app's _elapsed_repaint loop. tps
        arrives as an argument - the block never reaches for the app
        itself, so this stays testable on a bare double."""
        if self._t0 is None:
            return
        self.query_one(ThinkingHeader).content = thinking_header_text(
            self._marker, self._t0, time.monotonic(), tps)

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


def _markdown_to_text(src: str, width: int) -> Text:
    """Markdown rendered to a styled Text.

    Rich renders the Markdown to SEGMENTS; rebuilding those as a Text keeps
    every style (bold, bullets, code colour) while giving Textual a visual type
    it can select from. Falls back to the raw source if rendering ever fails —
    unstyled but readable and selectable beats an exception mid-turn.
    """
    try:
        console = Console(width=max(1, width), highlight=False)
        out = Text()
        for seg in console.render(Markdown(src), console.options.update(width=max(1, width))):
            if seg.text:
                out.append(seg.text, seg.style)
        return out
    except Exception:
        return Text(src)


def midturn_action(enter_interrupts: bool, alt_chord: bool) -> str:
    """Pure: what a mid-turn submission does — "queue" or "interrupt".

    ONE mapping with two ends and a boolean that swaps them (Ryan, 2026-08-21:
    "swapping the default behavior between those two in the settings page").
    NOT a queue path plus a hardcoded interrupt chord — that shape, under the
    swapped setting, leaves the user with no way to queue at all.

        enter_interrupts=False:  Enter -> queue      chord -> interrupt
        enter_interrupts=True:   Enter -> interrupt  chord -> queue
    """
    return "interrupt" if (enter_interrupts != alt_chord) else "queue"


def render_progress(t0: float, now: float, prompt_tokens=None, learned_rate=None) -> str:
    """Pure: the in-flight bubble text while no answer token has arrived yet.
    Elapsed since t0, always, with a trailing '…' to signal still working.
    An ETA is appended ONLY when there is both a learned prompt-eval rate and a
    prompt token count to apply it to; a None or zero rate (or a missing token
    count) yields elapsed-only and never divides by zero. `prompt_tokens` is an
    ESTIMATE (the previous turn's count), so the ETA is a projection, not a
    measurement of this turn. Inputs are optional so the pre-ETA callers
    (tool_display_parts, the pre-token answer bubble) keep working unchanged."""
    base = f"{fmt_dur(now - t0)} …"
    if (learned_rate is not None and learned_rate > 0
            and prompt_tokens is not None and prompt_tokens > 0):
        eta_s = prompt_tokens / learned_rate
        return f"{base} · est ~{fmt_dur(eta_s)}"
    return base


def is_reliable_rate_sample(prompt_tokens, first_token_s, floor: float = 0.25) -> bool:
    """Pure: the KV-cache gate. A rate sample is only trustworthy when the turn
    demonstrably REPROCESSED the prompt, which surfaces as a first-token latency
    at or above `floor` (default 0.25s). A cache-hit turn returns well under the
    floor and its prompt_tokens/latency is a misleading rate (fixed overhead
    dominates when few tokens are reprocessed), so admitting it into the median
    would make a large post-compact turn predict minutes. Requires BOTH a positive
    token count AND a first-token latency at the floor — either missing/zero
    means the sample is not usable."""
    return (prompt_tokens is not None and prompt_tokens > 0
            and first_token_s is not None and first_token_s >= floor)


def tps_text(tps: float) -> str:
    """Pure: the tok/s field, one format for every surface (the footer,
    the thinking header). The caller decides whether to show it at all:
    a None reactive means "no number yet" and renders as absence, never
    a rendered 0.0, which would be a lie about a number that does not
    exist."""
    return f"{tps:.1f} tok/s"


def thinking_header_text(marker: str, t0: float, now: float,
                         tps: float | None) -> str:
    """Pure: the thinking block header while the trace is streaming.
    '<marker> Thinking · 12.3s ... · 24.1 tok/s' — the elapsed part is
    render_progress (no ETA: a reasoning trace has no token count until
    it ends, and a confidently wrong number is worse than none), the
    tok/s part is tps_text (the footer's own format, one source). tps
    None -> elapsed only; the field is never rendered as 0.0 tok/s.
    `marker` is the expand glyph, so a collapsed block keeps its own
    state in the same string."""
    text = f"{marker} Thinking · {render_progress(t0, now)}"
    if tps is not None:
        text += f" · {tps_text(tps)}"
    return text


# The launch wordmark. Module-level so a test can assert on it without a running
# app, and so the art is not buried inside a method body.
LITETUI_SPLASH = (
    "\n"
    "  \u2588\u2588     \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588  \u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588\n"
    "  \u2588\u2588       \u2588\u2588     \u2588\u2588   \u2588\u2588       \u2588\u2588   \u2588\u2588  \u2588\u2588   \u2588\u2588  \n"
    "  \u2588\u2588       \u2588\u2588     \u2588\u2588   \u2588\u2588\u2588\u2588\u2588    \u2588\u2588   \u2588\u2588  \u2588\u2588   \u2588\u2588  \n"
    "  \u2588\u2588       \u2588\u2588     \u2588\u2588   \u2588\u2588       \u2588\u2588   \u2588\u2588  \u2588\u2588   \u2588\u2588  \n"
    "  \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588   \u2588\u2588   \u2588\u2588\u2588\u2588\u2588\u2588   \u2588\u2588   \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588\n"
    "      local-first chat \u00b7 LM Studio + llama.cpp \u00b7 /help\n"
)


#: The tool card's colours when no theme is reachable -- the values that were
#: hardcoded in this function before `tool-text` became a theme token. Kept as
#: the default so the pure function stays callable with no app and no theme.
TOOL_NAME_DEFAULT = "#e8a33d"


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


def tool_display_parts(tool, max_lines: int = 12, name_color: str | None = None) -> list:
    """Pure: the (text, style) parts for a ToolMessage's display from its state.
    Testable without a Textual app (no widget.content / console involved).
    `tool` needs: tool_name, _args, _result, _ok, _t0, _took.

    `name_color` is the theme's $tool-text. Optional, and defaulted, so this
    stays a pure function that a test can call with nothing but a stub tool."""
    arg_line = tool._args.replace("\n", " ")
    if len(arg_line) > 110:
        arg_line = arg_line[:107] + "..."
    parts = [(f"\U0001F527 {tool.tool_name}", f"bold {name_color or TOOL_NAME_DEFAULT}")]
    if arg_line:
        parts.append(("  " + arg_line, "#8b95a7"))
    if tool._result is not None:
        lines = tool._result.split("\n")
        shown = "\n".join(lines[:max_lines])
        if len(lines) > max_lines:
            shown += f"\n\u2026 ({len(lines) - max_lines} more lines, {len(tool._result)} chars total)"
        style = "bold #e5534b" if not tool._ok else "#7d8799"
        parts.append(("\n" + shown, style))
    if tool._result is None:
        parts.append(("\n  \u23f1 " + render_progress(tool._t0, time.monotonic()), "#8b95a7"))
    else:
        parts.append(("\n  ⏱ " + fmt_dur(tool._took or 0.0), "#5c6470"))
    return parts


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
            self.thinking.reset_header()

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


class ConfirmStop(ModalScreen[bool]):
    """Yes/No before interrupting a running turn.

    pi binds escape straight to `app.interrupt` and aborts with no confirmation
    (keybindings.ts:78, "Cancel or abort"), showing only an `esc to interrupt`
    hint in the spinner. Ryan asked for a confirmation here instead: a local 27B
    turn is slow and expensive enough that losing one to a stray escape costs
    more than the extra keypress. Escape inside the dialog answers No, so the
    accidental-escape case is a no-op rather than a lost turn.
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
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes, stop", variant="error", id="yes")
                yield Button("No, keep going", variant="primary", id="no")

    def action_answer_yes(self) -> None:
        self.dismiss(True)

    def action_answer_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
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


class LiteTUI(App):
    """TUI chat client for LM Studio."""

    TITLE = "LiteTUI"
    SUB_TITLE = "Connecting..."

    # The stock providers (theme, keys, quit...) plus ours.
    COMMANDS = App.COMMANDS | {LiteTUICommands}

    def get_system_commands(self, screen):
        """Keep Textual's Theme picker. Everything else is ours, and grouped.

        The stock provider also offers Keys, Maximize, Screenshot, Quit and
        Bell. All of those now exist as real commands in our own palette, with
        a group, a plain-English description and a slash command -- yielding
        them here as well would show each of them twice, in two different
        vocabularies, which is worse than the ungrouped list this replaces.

        Theme is the exception and is deliberately kept: it opens Textual's own
        picker, we do not reimplement it, and /settings promises in writing
        that "ctrl+p still has a quick-select". Dropping the stock provider
        wholesale would have quietly broken that promise -- the reason this is
        a filter rather than a deletion.
        """
        for command in super().get_system_commands(screen):
            if command.title == "Theme":
                yield command

    CSS = """
    Screen {
        background: $surface-darken-1;
        layers: base overlay;
    }

    .cancel-tool {
        /* In the flow, directly under its tool. .tool-msg is `margin: 0 2;
           padding: 0 2;` and the elapsed line is indented two more, so 4
           lands the control under the timer rather than under the margin. */
        margin: 0 0 0 4;
        width: auto;
        height: 1;
        display: none;
        background: $error 40%;
        color: $text;
    }

    .cancel-tool:hover {
        background: $error 70%;
    }

    .cancel-tool.visible {
        display: block;
    }

    #skill-ac {
        display: none;
        height: auto;
        max-height: 12;
        margin: 0 2;
        border: round $primary;
        background: $surface;
    }

    #skill-ac-list {
        height: auto;
        max-height: 10;
        scrollbar-size: 1 1;
        background: $surface;
    }

    #chat-log {
        height: 1fr;
        padding: 1 1;
        scrollbar-size: 1 1;
    }

    .user-msg {
        background: $primary-darken-3;
        color: $text;
        margin: 1 2 0 8;
        padding: 1 2;
        border: round $primary;
        border-title-color: $primary-lighten-2;
        border-title-align: left;
    }

    .assistant-msg {
        height: auto;
        background: $surface;
        color: $text;
        margin: 1 8 0 2;
        padding: 1 2;
        border: round $success-darken-2;
        border-title-color: $success;
        border-title-align: left;
    }

    .system-msg {
        color: $text-muted;
        text-style: italic;
        text-align: center;
        margin: 0 4;
    }

    .thinking-block {
        height: auto;
        margin-bottom: 1;
        border: dashed $thinking-box;
    }

    .thinking-header {
        /* The LABEL is text, so it follows $thinking-text, not the frame.
           It shipped following $thinking-box for one turn on the theory that
           header+border are one "frame" identity -- but a row named
           thinking-text that leaves the visible word "Thinking" untouched
           reads as a control that does nothing, and that is the exact class
           of defect this repo keeps re-finding. $thinking-box now means the
           border, and only the border. */
        color: $thinking-text;
        text-style: bold;
        padding: 0 1;
    }

    .thinking-header:hover {
        background: $warning-darken-3;
    }

    .thinking-body {
        display: none;
        height: auto;
        max-height: 10;
        scrollbar-size: 1 1;
        padding: 0 1;
        color: $thinking-text;
    }

    .thinking-block.expanded .thinking-body {
        display: block;
    }

    .tool-msg {
        margin: 0 2;
        padding: 0 2;
        background: $surface-darken-2;
    }

    #image-indicator {
        height: auto;
        display: none;
        padding: 0 2;
        background: $warning-darken-3;
        color: $warning;
    }

    #image-indicator.visible {
        display: block;
    }

    #message-input {
        margin: 0 1 1 1;
    }

    .ctx-label {
        dock: right;
        padding-right: 1;
        background: $footer-background;
    }

    /* Every modal centres in the window. SettingsScreen was missing from this
       list and rendered docked to the TOP-LEFT — the rule existed, the new
       screen simply was not in it. */
    ColorPickerScreen {
        align: center middle;
    }

    #cp-box {
        width: 54;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    #cp-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }

    #cp-hue {
        margin-top: 1;
    }

    #cp-presets {
        margin-top: 1;
    }

    #cp-row {
        height: 3;
        margin-top: 1;
    }

    #cp-swatch {
        width: 8;
        margin-right: 1;
    }

    #cp-hex {
        width: 12;
    }

    #cp-hint {
        color: $text-muted;
        margin-left: 1;
    }

    ConfirmStop, PickerScreen, HelpScreen, SettingsScreen,
    CalendarScreen, DayScreen, JobScreen {
        align: center middle;
    }

    #picker-box {
        width: 78;
        max-height: 80%;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #picker-title, #help-title {
        text-style: bold;
        color: $primary-lighten-2;
        padding-bottom: 1;
    }

    #picker-list {
        height: auto;
        max-height: 20;
        background: $surface;
        border: none;
    }

    #picker-hint, #confirm-sub {
        color: $text-muted;
        padding-top: 1;
    }

    /* ── Settings ─────────────────────────────────────────── */

    #set-box {
        width: 92;
        height: 88%;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #set-title {
        text-style: bold;
        color: $primary-lighten-2;
    }

    #set-sub {
        color: $text-muted;
        padding-bottom: 1;
    }

    /* The whole point: the body scrolls, so a knob added last is still
       reachable. A settings panel that outgrows the terminal and cannot
       scroll hides exactly the options nobody has tried yet. */
    /* One scroll per TAB PANE now, not one for the whole panel. Each section
       is its own short surface, so nothing pushes another off the bottom. */
    #set-tabs {
        height: 1fr;
    }

    .set-scroll {
        height: 1fr;
        scrollbar-size: 1 1;
        padding-right: 1;
    }

    #set-tabs Tabs {
        background: $surface;
    }


    .set-row {
        height: auto;
        margin-bottom: 1;
    }

    .set-indent {
        padding-left: 4;
        margin-bottom: 0;
    }

    .set-label {
        color: $text;
        text-style: bold;
    }

    .set-label-inline {
        color: $text;
        text-style: bold;
        padding-left: 1;
    }

    .set-switchline {
        height: auto;
        align-vertical: middle;
    }

    .set-subhead {
        color: $text-muted;
        text-style: bold;
        margin: 1 0 0 0;
    }

    .set-help {
        color: $text-muted;
        padding-left: 1;
    }

    .set-input {
        width: 100%;
    }

    /* A disabled control must LOOK disabled: it is env-locked, and a field
       that silently ignores typing reads as a broken app. */
    .set-input:disabled, Select:disabled {
        opacity: 0.5;
    }

    #set-error {
        color: $error;
        height: auto;
        padding: 0 1;
    }

    #set-buttons {
        height: auto;
        padding-top: 1;
        align-horizontal: right;
    }

    #set-buttons Button {
        margin-left: 2;
    }

    /* Compaction, marked as itself: the warning hue is the 'this is the
       app doing maintenance' colour, distinct from any conversation card. */
    .compaction-card {
        /* height: auto is LOAD-BEARING. A Vertical defaults to height: 1fr, and
           inside #chat-log (a VerticalScroll) that collapses the card to a
           minimal box: only the FIRST child paints. The whole card -- plan,
           folded prompt, thinking block, tool cards, the summary streaming in --
           rendered into children with no room to exist, so compaction looked
           like a yellow title bar and nothing else on EVERY run, while the
           compaction itself worked perfectly. .assistant-msg, the bubble mounted
           beside it in the same log, has carried height: auto all along. */
        height: auto;
        margin: 1 2 0 2;
        padding: 0 1;
        border-left: thick $warning;
    }

    .compaction-title {
        color: $warning;
    }

    .compaction-plan {
        color: $text-muted;
    }

    .compaction-status {
        color: $text-muted;
        margin-top: 1;
    }

    /* The calendar. Wide and airy on purpose: calcure's month breathes, and
       a cramped grid is just a list with extra steps. The side pane is a
       fixed column so the seven day-cells divide a stable width. */
    #cal-box {
        width: 96%;
        height: 92%;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #cal-body {
        height: 1fr;
    }

    #cal-grid {
        width: 1fr;
        height: 1fr;
    }

    /* border-left IS the separator -- calcure draws a literal `|` column, but
       a real border cannot drift out of alignment with the text beside it. */
    #cal-side {
        width: 34;
        height: 1fr;
        padding: 0 0 0 2;
        border-left: solid $primary;
    }

    #cal-hints {
        height: 1;
        padding: 0 1;
    }

    #day-box {
        width: 82;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #day-title {
        text-style: bold;
        color: $error;          /* the calcure title treatment, as the grid */
    }

    #day-list {
        height: auto;
        max-height: 18;
        margin: 1 0;
        background: $surface;
    }

    #day-hint {
        color: $text-muted;
    }

    #job-box {
        width: 82;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
        /* The builder made the form taller; on a short terminal the Save
           button must scroll into reach rather than clip out of existence. */
        overflow-y: auto;
    }

    #job-title {
        text-style: bold;
        color: $error;
    }

    .job-cap {
        color: $text-muted;
        margin-top: 1;
    }

    #job-preset {
        width: 36;
    }

    #job-params {
        height: auto;
        margin-top: 1;
    }

    #job-weekday, #job-month {
        width: 18;
    }

    .job-param-label {
        color: $text-muted;
        margin: 1 1 0 1;
        width: auto;
    }

    #job-switches {
        height: auto;
        margin-top: 1;
    }

    .job-switchcap {
        color: $text-muted;
        margin: 1 3 0 1;
        width: auto;
    }

    #job-status {
        margin-top: 1;
        height: 2;
    }

    #job-buttons {
        height: auto;
        margin-top: 1;
    }

    #job-buttons Button {
        margin-right: 2;
    }

    #help-box {
        width: 88;
        height: 80%;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #help-scroll {
        height: 1fr;
        scrollbar-size: 1 1;
    }

    #help-buttons {
        height: auto;
        align: center middle;
        padding-top: 1;
    }

    #confirm-box {
        width: 56;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $error;
    }

    #confirm-title {
        text-style: bold;
        color: $text;
        padding-bottom: 1;
    }

    #confirm-sub {
        color: $text-muted;
        padding-bottom: 1;
    }

    #confirm-buttons {
        height: auto;
        align: center middle;
    }

    #confirm-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        # NOT priority=True: a priority app binding wins over the focused
        # SCREEN, so Esc inside a modal fired stop_turn instead of the
        # modal's own cancel and the picker could not be dismissed.
        Binding("escape", "stop_turn", "Stop turn"),
        # priority=True so the focused Input never swallows it. Whether the
        # terminal DELIVERS a distinct ctrl+shift+enter is a property of the
        # terminal + kitty keyboard protocol (Textual's Windows driver enables
        # it); prove it on a real boot before trusting the chord end.
        Binding("ctrl+shift+enter", "submit_alt", "Send (swapped)", priority=True, show=False),
        # MEASURED 2026-08-21 (keyprobe, this box, Windows Terminal): the
        # terminal does not honour the kitty keyboard protocol, so BOTH
        # ctrl+enter and ctrl+shift+enter arrive as 'ctrl+j' (legacy LF) and
        # the binding above can never fire here. Physically pressing
        # ctrl+shift+enter lands as ctrl+j — so this alias is what makes the
        # chord real on this terminal. Cost: a bare ctrl+j also triggers it.
        # Both names stay: a kitty-capable terminal delivers the real chord.
        Binding("ctrl+j", "submit_alt", "Send (swapped)", priority=True, show=False),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+o", "paste_image", "Paste Image"),
        # Ctrl+V: Input._on_paste already handles BRACKETED paste, but a
        # terminal that does not send it (plain conhost) delivers nothing at
        # all — which is why images worked (Ctrl+O reads the clipboard
        # directly) while text silently did not. This reads the OS clipboard
        # by the same logic, so both paths work regardless of the terminal.
        # priority=True is REQUIRED, and it is the whole bug: Input binds
        # ctrl+v to its OWN action_paste, which reads TEXTUAL's internal
        # clipboard -- empty unless something inside the app copied there --
        # so Ctrl+V was handled, did nothing, and reported nothing. The
        # focused widget beats a non-priority App binding. Safe here because
        # action_paste_text returns early on a ModalScreen (the inverse of
        # the escape bug above, where priority was WRONG).
        Binding("ctrl+v", "paste_text", "Paste", priority=True),
        # Textual ships the selection machinery and binds NO key to it:
        # App.BINDINGS is only ctrl+q/ctrl+c, so a drag selected text that
        # nothing could copy. ctrl+shift+c, because ctrl+c is help_quit.
        Binding("ctrl+shift+c", "screen.copy_text", "Copy selection"),
        # Same collision as ctrl+v: Input binds ctrl+x to cut.
        Binding("ctrl+x", "clear_image", "Clear Image", priority=True),
        Binding("ctrl+l", "clear_chat", "Clear Chat"),
        Binding("ctrl+t", "toggle_tools", "Tools on/off"),
    ]

    pending_image: reactive[str | None] = reactive(None)
    ctx_used: reactive[int | None] = reactive(None)
    # Generation speed of the most recent turn. Survives the turn so the footer
    # keeps showing the last measurement while idle rather than blanking.
    tps: reactive[float | None] = reactive(None)

    def __init__(self):
        super().__init__()
        # Every knob, loaded once: defaults < settings.json < environment.
        self.settings: Settings = settings_mod.load()
        self.conversation: list[dict] = []
        self.model_id: str = ""
        self.available_models: list[str] = []
        self.tools_enabled = self.settings.tools_enabled
        self.ctx_max: int | None = None  # effective context window (tokens), from LM Studio
        #: Is ctx_max the LOADED window, or merely the model's ceiling? Anything
        #: that divides by ctx_max must check this first.
        self.ctx_loaded: bool = False
        # None = send no reasoning_effort at all, which is what this app did
        # before /think existed. LM Studio's own default for an ABSENT value is
        # xhigh, so "unset" is not "off" — /think off is a different thing and
        # sends "none" explicitly.
        # From settings, not hardcoded. This was `None`, so a SAVED thinking
        # level was ignored on every launch — and it looked like it worked
        # because saving it in the same session did apply it.
        self.thinking_level: str | None = self.settings.thinking_level
        # One warning per session: this is a config truth, not a per-turn event.
        self._reasoning_ignored_warned = False
        # Images staged by view_image during a tool round, drained into a
        # role:"user" turn once the round's tool results are appended.
        self._pending_tool_images: list[tuple[str, str]] = []
        # "vlm" | "llm" | None, learned from the same request as the ctx window.
        self.model_type: str | None = None
        # tok/s accounting, reset per turn by _tps_start.
        self._tps_t0: float | None = None
        self._tps_n = 0
        self._tps_painted = 0.0
        # The STORE owns where this conversation lives and how it is written.
        # The properties below keep `self.convo_id` and friends resolving, so
        # nothing that reads them had to change.
        self.store = ConversationRepository(on_error=self._report_persist_error)
        # Staged-but-not-created. See _new_convo / _materialise_convo.
        self._stop_requested = False  # Esc-to-stop, checked inside the stream loop
        # Did the LAST turn end because the user killed it, or because it
        # finished? Only the post-compaction wake ping cares: "resume the
        # in-flight task" is the wrong thing to say about a task the user
        # deliberately stopped. Distinct from _stop_requested, which is
        # cleared at the START of the next turn and is about the turn in
        # flight; this outlives the turn so the ping can read it.
        self._turn_abandoned = False
        # Elapsed-time display while a turn is in flight (pre-token) and while
        # tool calls run. The repaint is a task on the worker's own event loop
        # (interleaves with the stream); the display string is render_progress.
        self._elapsed_task = None
        self._elapsed_body = None            # AnswerBody in its pre-token phase
        self._elapsed_body_t0: float = 0.0
        self._inflight_tools: list = []      # ToolMessages awaiting their result
        self._cancel_buttons: dict = {}      # ToolMessage -> its CancelToolButton
        # The last scroll position this app SET. Following is judged against it,
        # never against max_scroll_y -- see _scroll_down.
        self._follow_anchor: float | None = None
        self._compact_card = None            # the live CompactionCard, if any
        #: Messages held while a turn runs (FIFO). Each {"content": ..., "text": ...}.
        #: Flushed one per turn end — consecutive role:"user" messages are a
        #: chat-template gamble some models refuse, so each gets its own turn.
        self._pending_input: list = []
        #: Host authority for the turn currently consuming tools. Human turns
        #: start from settings; cron/inbox turns explicitly replace it with a
        #: narrower profile. The model never writes this field.
        self._active_tool_profile = self.settings.tool_policy_profile
        #: Cron jobs, loaded once at construction. A scheduled prompt is an
        #: INPUT nobody typed, so it rides the same held/flushed path as inbox
        #: mail rather than growing a second delivery route.
        self._jobs: list = sched_mod.load(paths.ROOT)
        # ETA: a rolling median of prompt-eval tokens/sec, learned ONLY from
        # turns that demonstrably reprocessed the prompt (the KV-cache gate in
        # is_reliable_rate_sample). _eta_last_prompt_tokens is the previous
        # turn's count, used as the ESTIMATE this turn's ETA is projected from;
        # _eta_first_delta is this turn's first-delta time, for first-token
        # latency. All optional/None until a reliable turn has happened.
        self._eta_samples: list[float] = []
        self._eta_last_prompt_tokens: int | None = None
        # Last emit time per glass-box channel, for the continuous ones only.
        self._gb_last: dict[str, float] = {}
        self._eta_first_delta: float | None = None
        # The thinking block currently streaming (its header timer): set
        # at block creation, cleared by _thinking_done when the trace
        # ends (first content token, first tool call, or turn end).
        self._thinking_live: ThinkingBlock | None = None
        self._store_injected = False  # see STORE_HEADER; once per conversation
        # ONE seam for both engines (LM Studio / our llama-server). The chat
        # client is rebuilt in _connect after ensure_running(), because the
        # llama backend may ATTACH to a different host than it was configured
        # with — building here alone would pin the pre-attach URL.
        self.backend = llm_backend.make_backend(self.settings)
        #: key -> ModelRow for the connected backend; the /model picker reads
        #: source tags and load state from here.
        self.model_rows: dict[str, llm_backend.ModelRow] = {}
        self.client = AsyncOpenAI(
            base_url=self.backend.base_url(),
            api_key="litetui",
        )
        # Discovered ONCE, before the first system prompt is built -- the skill
        # index rides in that prompt, so discovering later would ship a prompt
        # that omits every skill for the first turn.
        # Honour the setting at DISCOVERY, not at use: an empty index means the
        # skill tool is never offered and the index block never enters the system
        # prompt, which is what "off" has to mean for a context-costing feature.
        # `[]`, not `{}` — discover() returns a LIST, and load() iterates its
        # argument expecting Skill objects. A dict would yield keys.
        self.skills, self.skills_cached_at = self._load_skills()
        self.mcp = mcp_client.MCPManager(paths.ROOT)
        if self.settings.mcp_enabled:
            self.mcp.load()
            # Per-server opt-out. Stopping AFTER load rather than filtering the
            # config keeps mcp.json the single source of what EXISTS, so a
            # disabled server still appears in /settings to be re-enabled.
            for name in list(self.settings.mcp_disabled_servers):
                srv = self.mcp.servers.pop(name, None)
                if srv is not None:
                    try:
                        srv.stop()
                    except Exception:
                        pass
        self._mcp_dispatch = self.mcp.dispatch()
        # A seat in the fleet, like any other agent. Registration is
        # deferred to the first poll tick so the roster shows the real
        # model rather than the empty string it holds before _connect.
        self.seat = harness_mod.Seat(
            agent_id=harness_mod.new_agent_id(),
            name=(self.settings.seat_name or "").strip() or "LiteTUI",
            model="",
        )
        self._seat_started = False
        # The plugin substrate. Per INSTANCE, never module-level — the suite
        # builds many apps in one process, and a shared registry would leak
        # skills/mcp/seat state between them. Skills discovery and mcp.load()
        # above stay host-owned lines: inside a plugin's register() their
        # failure would be swallowed by per-plugin isolation, turning a
        # broken checkout into a silent half-boot.
        self.plugins = plugins_mod.PluginRegistry()
        # The host's own prompt sections, registered BEFORE plugins so slot
        # collisions land on the intruder, not the floor. Renders read self.*
        # LIVE — a /reconnect or convo switch changes the next composition.
        _ord = plugins_mod.PROMPT_ORDER
        self.plugins.add_prompt_section(
            "host", _ord["BASE"],
            lambda: prompt_compiler.compile_prompt_file(
                paths.SYSTEM_PROMPT_FILE,
                root=paths.ROOT,
            ).strip(),
            enabled=lambda: paths.SYSTEM_PROMPT_FILE.exists(),
        )
        self.plugins.add_prompt_section(
            "host", _ord["MEMORY"],
            lambda: memory_prompt(self.convo_id, self.convo_dir),
            enabled=lambda: self.convo_dir is not None,
        )
        self.plugins.add_prompt_section(
            "host", _ord["TOOLS"],
            # The newlines are LOAD-BEARING. compose_prompt glues sections
            # with a bare `base + render()`, and the constant this replaced was
            # "\nYou have four tools...\n" -- carrying its own separators. A
            # plain .strip() of the file drops them and welds the memory block
            # straight onto the first word of this one. Caught by
            # test_prompt_compose's reference fold, which is exactly what that
            # test exists to notice.
            lambda: "\n" + paths.TOOLS_PROMPT_FILE.read_text(encoding="utf-8").strip() + "\n",
            # Gated on BOTH the toggle and the file. A missing file must not
            # render as an empty section: the tools would still be OFFERED to
            # the model with no instructions on how to use them, which is a
            # worse state than not offering them, and a silent one. The boot
            # check below is what makes the absence audible.
            enabled=lambda: self.tools_enabled and paths.TOOLS_PROMPT_FILE.exists(),
        )
        # The substrate's own status readout — host-registered so it can
        # never be disabled away with a plugin.
        self.plugins.add_command(
            "host", ("/plugins",), plugins_mod.status_command,
            palette="Plugins",
            help="Add-ons that extend the app, and whether each one is working.",
            group="tools",
            order=30,
        )
        self._plugin_manifests = plugins_mod.register_plugins(
            self, self.plugins,
            disabled=frozenset(self.settings.plugins_disabled or ()),
        )
        self._new_convo()
        self._load_system_prompt()

    def compose(self) -> ComposeResult:
        yield Header()
        # No cancel control here any more: it is mounted next to the tool it
        # kills, by _tool_begin. See CancelToolButton.
        yield VerticalScroll(id="chat-log")
        yield Static(
            "  Image attached — Ctrl+X to remove", id="image-indicator"
        )
        # Above the input, so it grows UPWARD out of the message area rather
        # than pushing the box down as it filters.
        self._skill_ac = SkillAutocomplete()
        yield self._skill_ac
        yield PromptInput(
            placeholder="Message... (Ctrl+V paste | Ctrl+O image | /help)",
            id="message-input",
        )
        yield ContextFooter()

    def _splash(self) -> None:
        """The launch wordmark. First thing drawn, before the backend answers.

        Deliberately six lines and no colour codes: it is seen on EVERY launch,
        so it has to earn its height once and then get out of the way. Box-drawing
        glyphs only -- no square brackets anywhere in the art, because Textual
        parses markup out of a raw str content and a stray "[" would be eaten as
        a tag rather than drawn.
        """
        self._system(LITETUI_SPLASH)

    def on_mount(self) -> None:
        self.query_one("#message-input", Input).focus()
        self._splash()
        # An authored prompt file that has gone missing is invisible everywhere
        # else: the section simply does not render, and the model is handed
        # tools with no instructions. Say it once, at the only moment anyone is
        # looking at a fresh screen.
        for label, f in (
            ("system prompt", paths.SYSTEM_PROMPT_FILE),
            ("tools prompt", paths.TOOLS_PROMPT_FILE),
        ):
            if not f.exists():
                self._system(f"[!] {label} missing: {f}\n    That section is absent from the model's context.")
        self._connect()
        # Plugin activate() hooks — the side-effecting half of the lifecycle,
        # run where the monitors it will absorb have always started.
        plugins_mod.activate_plugins(self, self.plugins, self._plugin_manifests)

    @work(exclusive=True, group="inbox")
    async def _inbox_monitor(self) -> None:
        """Register the seat, then wake this agent when its own mail lands.

        Deliberately NOT `liteharness.hooks watch`: that is a second consumer
        on a shared mailbox (the defect the ls-liteharness fix retracted) and
        it writes to stdout, which paints over a Textual screen. harness.poll
        claims ONLY messages addressed to this seat.
        """
        await asyncio.sleep(2)  # let _connect settle so the model is known
        self.seat.model = self.model_id or "unknown"
        ok = await asyncio.to_thread(self.seat.register)
        self._seat_started = True
        if ok:
            self._system(
                f"harness seat online · {self.seat.name} · {self.seat.agent_id[:8]}"
            )
            # The harness tool just joined the offer — repaint the
            # derived tool count. (The one late tool; MCP loads in __init__.)
            self._update_header()
            # The model cannot see the UI line above, and the system prompt was
            # built before registration finished. Without this it holds fleet
            # tools it has no idea it is entitled to use.
            # 🔴 MERGED INTO THE FIRST SYSTEM MESSAGE, NOT APPENDED AS A SECOND ONE.
            # A second role:"system" turn is rejected outright by some chat
            # templates: qwen/qwen3.8-27b returns HTTP 500 "Jinja Exception:
            # System message must be at the beginning" for it, while the
            # @iq2_xxs build of the same model accepts it. So this failed only
            # after a /model switch, which reads as the new model being broken
            # rather than as our message shape being wrong.
            # Same approach as _inject_store_once: extend conversation[0].
            # Replace a stale line from a previous process before appending a
            # second one — a resumed conversation already carries one.
            if self._sync_fleet_identity():
                return
            self._append_to_system(
                self._fleet_identity_sentence()
                + load_prompt("harness-capabilities")
            )
        else:
            # Say so once. A seat nobody can reach that reports nothing is
            # indistinguishable from one that is simply idle.
            #
            # ...unless the operator switched it off, which is neither broken
            # nor idle, and so is not news. LITETUI_NO_HARNESS already stops
            # `register` from touching the live fleet -- but the gate covered
            # the REGISTRY side effect and not this UI one, and a remedy that
            # covers only part of what it was written for reads as fully
            # applied. Under the suite every app instance therefore mounted a
            # chat line and scrolled the log from a background worker, at a
            # moment set by how long `register` took to refuse. Landing after a
            # test's own content, that scroll is indistinguishable from the app
            # autoscrolling on its own: it is what made the three thinking-block
            # autoscroll tests fail ~20% of the time, at a rate that rose with
            # how long the process had been alive (0/20 alone, 4-5/20 with
            # siblings) because a longer process gives the worker more chances
            # to land late.
            if harness_mod.harness_disabled():
                return
            self._system(f"harness seat OFFLINE ({self.seat.error or 'unknown'})")
            return
        try:
            # A beat every HEARTBEAT_EVERY polls, not every poll: refreshing
            # presence costs a subprocess, and `discover` only cares on the
            # order of minutes. Without ANY beat the seat decays to [ghost]
            # while the app is plainly running -- `last_seen` is written once,
            # at registration, and never again.
            beat = 0
            while True:
                await asyncio.sleep(harness_mod.POLL_SECONDS)
                msgs = await asyncio.to_thread(self.seat.poll)
                for m in msgs:
                    self._deliver_inbox(m)

                beat += 1
                if beat >= harness_mod.HEARTBEAT_EVERY:
                    beat = 0
                    # Silent on failure by design: a missed beat is not news,
                    # and reporting one would paint the transcript every minute
                    # that liteharness happened to be busy.
                    await asyncio.to_thread(self.seat.heartbeat)
        except asyncio.CancelledError:
            raise

    def _deliver_inbox(self, msg: dict) -> None:
        """Show the message, then WAKE the agent with it as a user turn.

        Queued rather than dropped when a turn is already running: mail that
        arrives mid-turn is exactly the mail worth not losing.
        """
        text = harness_mod.format_message(msg)
        profile = tool_policy.SCHEDULED
        if self._chat_running():
            # HELD, never appended: an appended mid-turn message lands where
            # nothing announces it and the model trusts its inbox tool over its
            # own context, so it goes unread. Inbox mail always QUEUES -- it must
            # never cancel work in flight.
            # Why: Docs/adr/0001-mid-turn-mail-is-held-not-appended.md
            self._user_bubble(text, False, queued=True)
            self._pending_input.append(
                {"content": text, "text": text, "tool_profile": profile}
            )
            return
        self._user_bubble(text, False)
        self._append({"role": "user", "content": text})
        self._active_tool_profile = profile
        self._stream()

    @work(exclusive=True, group="cron")
    async def _cron_monitor(self) -> None:
        """Fire scheduled prompts while the app is up.

        Its own worker group, deliberately. Sharing "chat" with _stream would
        make every tick cancel the turn in flight -- the exact trap autocompact
        fell into, where a checker started work from inside the work it was
        checking.
        """
        while True:
            await asyncio.sleep(sched_mod.TICK_SECONDS)
            try:
                ready = sched_mod.due(self._jobs, datetime.now())
            except Exception:
                continue  # a scheduling bug must never take the chat down
            for job in ready:
                self._fire_job(job)

    def _fire_job(self, job) -> None:
        """Deliver a job as a real user turn, holding if one is running.

        The slot is stamped and PERSISTED BEFORE delivery, not after. If the
        stamp came after, a crash mid-turn would leave the job looking unfired
        and it would run again on the next tick inside the same minute -- and
        the failure that produces duplicates is exactly the one you cannot see
        in a log that only records successes.
        """
        now = datetime.now()
        blocked = sched_mod.prepare_fire(job, getattr(self, "convo_id", ""), now)
        if blocked:
            try:
                sched_mod.save(self._jobs, paths.ROOT)
            except OSError:
                pass
            self._system(blocked)
            return
        job.last_fired_slot = sched_mod.slot_of(now)
        job.run_count += 1
        try:
            sched_mod.save(self._jobs, paths.ROOT)
        except OSError:
            pass  # an unwritable store must not stop the job from running

        label = job.label or job.id
        text = job.prompt
        profile = getattr(job, "tool_profile", tool_policy.SCHEDULED)
        source = "loop" if getattr(job, "kind", "cron") == "loop" else "cron"
        banner = f"[{source} {label} \u00b7 {job.schedule}]\n{text}"

        if job.new_conversation and not self._chat_running():
            self._handle_command("/new")

        if self._chat_running():
            # QUEUED, never interrupting. A scheduled prompt is the LEAST
            # urgent kind of input there is -- nobody is waiting on it, so it
            # has no business cancelling something a human asked for.
            self._user_bubble(banner, False, queued=True)
            self._pending_input.append(
                {"content": text, "text": banner, "tool_profile": profile}
            )
            return
        self._user_bubble(banner, False)
        self._append({"role": "user", "content": text})
        self._active_tool_profile = profile
        self._stream()

    def _cron_command(self, arg: str) -> None:
        """/cron — add, list, remove, enable, disable, or fire a job now."""
        arg = arg.strip()
        if not arg or arg.lower() in ("list", "ls"):
            self._cron_list()
            return

        verb, _, rest = arg.partition(" ")
        verb = verb.lower()
        rest = rest.strip()

        if verb == "add":
            self._cron_add(rest)
            return

        if verb in ("rm", "del", "remove"):
            job = self._cron_find(rest)
            if not job:
                return
            self._jobs.remove(job)
            sched_mod.save(self._jobs, paths.ROOT)
            self._system(f"/cron: removed {job.id} ({job.label or job.prompt[:40]})")
            return

        if verb in ("on", "off", "enable", "disable"):
            job = self._cron_find(rest)
            if not job:
                return
            job.enabled = verb in ("on", "enable")
            sched_mod.save(self._jobs, paths.ROOT)
            self._system(f"/cron: {job.id} is now {'ON' if job.enabled else 'OFF'}")
            return

        if verb == "run":
            job = self._cron_find(rest)
            if not job:
                return
            # Fires REGARDLESS of schedule and of enabled — "run" is the human
            # asking for it now, which is a different act from the schedule
            # coming round. It still stamps the slot, so an actual due-time
            # inside this same minute will not double up.
            self._fire_job(job)
            return

        self._system(
            "/cron add <schedule> <prompt>   e.g. /cron add @daily summarise my inbox\n"
            "/cron list | rm <id> | on <id> | off <id> | run <id>\n"
            "schedule: 5-field cron (min hour day month weekday) or "
            "@hourly @daily @weekly @monthly"
        )

    def _cron_find(self, token: str):
        """Resolve an id prefix or a label to exactly one job, or say why not.

        Ambiguity is reported rather than resolved to the first match: picking
        one silently is how the wrong job gets deleted.
        """
        token = token.strip()
        if not token:
            self._system("/cron: which job? Use /cron list to see ids.")
            return None
        hits = [j for j in self._jobs
                if j.id.startswith(token) or (j.label and j.label == token)]
        if not hits:
            self._system(f"/cron: no job matches {token!r}. /cron list shows them.")
            return None
        if len(hits) > 1:
            ids = ", ".join(j.id for j in hits)
            self._system(f"/cron: {token!r} matches {len(hits)} jobs ({ids}) — be more specific.")
            return None
        return hits[0]

    def _cron_add(self, rest: str) -> None:
        if not rest:
            self._system("/cron add <schedule> <prompt>")
            return

        tokens = rest.split()
        if tokens[0].startswith("@"):
            schedule, prompt = tokens[0], " ".join(tokens[1:])
        elif len(tokens) > 5:
            schedule, prompt = " ".join(tokens[:5]), " ".join(tokens[5:])
        else:
            self._system(
                "/cron add: a schedule is 5 fields (min hour day month weekday) "
                "or an @alias, followed by the prompt.\n"
                "  /cron add 0 9 * * 1-5 what is on for today?"
            )
            return

        if not prompt:
            self._system("/cron add: that schedule parsed, but there is no prompt after it.")
            return

        try:
            cron = sched_mod.Cron.parse(schedule)
        except sched_mod.CronError as e:
            # The error names the FIELD. "invalid cron expression" would leave
            # the person guessing which of five to fix.
            self._system(f"/cron add: {e}")
            return

        job = sched_mod.Job(prompt=prompt, schedule=schedule)
        self._jobs.append(job)
        sched_mod.save(self._jobs, paths.ROOT)

        nxt = cron.next_after(datetime.now())
        when = nxt.strftime("%a %d %b %H:%M") if nxt else "never (no matching date)"
        self._system(f"/cron: added {job.id} — next fire {when}\n  {prompt}")

    def _cron_list(self) -> None:
        if not self._jobs:
            self._system(
                "/cron: no jobs.\n"
                "  /cron add @daily summarise what I did yesterday\n"
                "  /cron add */30 * * * * check the build"
            )
            return

        now = datetime.now()
        lines = [f"{len(self._jobs)} job(s) — jobs fire only while LiteTUI is open"]
        lines.append("")
        for job in self._jobs:
            try:
                nxt = job.next_after(now)
                when = nxt.strftime("%a %d %b %H:%M") if nxt else "never"
            except sched_mod.CronError as e:
                # A job that can never fire must SAY so here. Silently listing
                # it beside working jobs is how it sits dead for weeks.
                when = f"BROKEN — {e}"
            state = "on " if job.enabled else "off"
            head = f"  {job.id}  {state}  {job.schedule:<16} next {when}"
            lines.append(head)
            lines.append(f"        x{job.run_count}  {job.prompt}")
        self._system("\n".join(lines))

    async def _contextualise_tool_result(self, name: str, raw: str) -> str:
        """What this tool result contributes to the CONVERSATION (tool_context).

        The tool bubble always shows the raw result — this changes only what
        the model re-reads on every subsequent turn. The raw is never
        destroyed: both processing modes park it in a sidecar file under the
        conversation's own directory and the placeholder carries the path, so
        the model can `read` it back on demand. Every early return here is the
        raw itself — the fail-safe direction is the one that keeps everything.
        """
        plan = tool_context.plan_tool_result(
            self.settings.tool_context_mode,
            name,
            raw,
            self.settings.tool_context_threshold_chars,
        )
        if plan.route == tool_context.VERBATIM:
            return raw
        if self.convo_dir is None:
            # Nowhere durable to park the raw, so replacing it would be
            # destructive. Should not happen (a tool result implies a turn,
            # which implies a materialised conversation) — but "should not"
            # is not a guard.
            return raw
        sidecar = self.convo_dir / "tool-raw"
        path = sidecar / f"{len(self.conversation):05d}-{name}.txt"

        def _park() -> None:
            sidecar.mkdir(parents=True, exist_ok=True)
            path.write_text(raw, encoding="utf-8")

        try:
            await asyncio.to_thread(_park)
        except OSError:
            return raw  # could not store the raw -> do not replace it
        pointer = str(path)
        if plan.route == tool_context.ROUTE_MASK:
            return tool_context.render_mask(name, raw, pointer)

        # ROUTE_SUMMARISE: one throwaway side call. Its context is never
        # persisted anywhere — the main conversation never holds the raw.
        task = ""
        for m in reversed(self.conversation):
            if m.get("role") == "user":
                task = self._flatten(m.get("content"))
                break
        summary = ""
        try:
            resp = await self.client.chat.completions.create(
                model=self.model_id or "local-model",
                messages=[{
                    "role": "user",
                    "content": tool_context.summarise_prompt(task, name, raw),
                }],
                # Reuses the compact budget knob rather than minting a third
                # literal: it is already tuned for the local-model failure
                # where reasoning eats a small budget before any output.
                max_tokens=self.settings.compact_max_tokens,
                extra_body={"reasoning_effort": "low"},
            )
            summary = (resp.choices[0].message.content or "").strip()
            why_no_summary = "side call returned an empty summary"
        except Exception as e:
            summary = ""
            why_no_summary = f"side call failed — {type(e).__name__}: {e}"
        if not summary:
            # Side call failed or produced nothing: fall back to the MASK.
            # Still non-lossy (the pointer survives), still cheap — and more
            # honest than a fabricated one-line "summary".
            #
            # The fallback is right; falling back SILENTLY is not. Without a
            # recorded reason, a backend that can never summarise degrades
            # every big result to a mask and nothing anywhere says why.
            line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {path.name}: {why_no_summary}\n"

            def _record() -> None:
                with (sidecar / "summarise-failures.log").open(
                        "a", encoding="utf-8") as f:
                    f.write(line)

            try:
                await asyncio.to_thread(_record)
            except OSError:
                pass  # the record must never take the fallback down with it
            return tool_context.render_mask(name, raw, pointer)
        return tool_context.render_summary(name, raw, pointer, summary)

    def _all_tools(self) -> list[dict]:
        """Static tools + the `skill` tool + every MCP tool, as OpenAI specs."""
        return self.plugins.tool_specs()

    def _dispatch_for(self, name: str):
        """Resolve a tool name across all three sources, static first."""
        return self.plugins.dispatch_for(name)

    async def _execute_tool(self, name: str, args: dict) -> tuple[str, bool]:
        """The one host authorization door before any tool side effect.

        Metadata lives in the registry; the active turn profile lives in the
        host. A denial returns an ordinary tool result so the model can adapt
        without an exception tearing the tool-call pairing apart.
        """
        fn = self._dispatch_for(name)
        if fn is None:
            return f"[error] unknown tool: {name}", False
        policy = self.plugins.policy_for(name)
        if policy is None:
            return f"[policy denied] {name}: no capability metadata", False
        decision = tool_policy.evaluate(
            getattr(self, "_active_tool_profile", tool_policy.INTERACTIVE),
            policy,
            args,
            paths.ROOT,
        )
        if decision.action == tool_policy.DENY:
            return f"[policy denied] {name}: {decision.reason}", False
        if decision.action == tool_policy.CONFIRM:
            approved = await self.push_screen_wait(
                ToolApprovalScreen(name, args, decision)
            )
            if not approved:
                return f"[policy denied by user] {name}", False
        try:
            return str(await asyncio.to_thread(fn, args)), True
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}", False

    def _system_prompt_text(self) -> str:
        """systemprompt.md + the store block + the tools block, in that order.

        Single builder so the tools toggle cannot silently drop the store block
        — rebuilding it in two places is how one of them goes stale. The fold
        itself lives in the registry (PROMPT_ORDER slots); host sections are
        registered in __init__, the skills index by the skills plugin.
        """
        return self.plugins.compose_prompt()

    def _load_system_prompt(self) -> None:
        base = self._system_prompt_text()
        if base:
            self._append({"role": "system", "content": base})

    # ── Store injection ──────────────────────────────────────────
    #
    # memory.md / soul.md / handoff.md are read fresh and merged into the
    # system message AT REQUEST TIME, not baked into self.conversation.
    #
    # Two reasons for that. A memory the agent writes on turn N is visible on
    # turn N+1 rather than after a restart — telling a model "your notes are
    # in that file" is an instruction, injecting them is a mechanism. And the
    # stored transcript keeps ONE stable system message instead of a new
    # snapshot every turn, so convo.jsonl does not grow a copy of the store
    # per exchange.

    def _read_store_file(self, name: str, cap: int) -> str:
        # A staged conversation has no directory yet, so there is nothing to
        # read. Deliberately does NOT materialise: reading must not create.
        if self.convo_dir is None or getattr(self, "_convo_pending", False):
            return ""
        p = self.convo_dir / name
        try:
            # errors="replace" ON PURPOSE: a store file that carries a stray
            # non-UTF-8 byte (e.g. a cp1252 em dash, 0x97, written by an
            # editor that never declared its encoding) must not be able to
            # raise UnicodeDecodeError out of the worker and kill _compact.
            # A bad byte degrades to U+FFFD instead of crashing the app.
            text = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        if len(text) > cap:
            text = (
                text[:cap]
                + f"\n\n[... truncated at {cap} chars — {name} is too long to inject "
                f"in full. Move detail into {paths.MEMORIES_DIR}/ and leave pointers here.]"
            )
        return text

    def _store_block(self, live: bool = False) -> str:
        # memory.md is capped hardest ON PURPOSE: it is an index, and an index
        # that needs more than this has stopped being one.
        parts = []
        for name, cap in (("memory.md", 6000), ("soul.md", 8000), ("handoff.md", 8000)):
            body = self._read_store_file(name, cap)
            if body:
                parts.append(f"### {name} (current contents)\n\n{body}")
        if not parts:
            return ""
        if live:
            return (
                "\n\n## Your store, as it stands right now\n\n"
                "Re-read from disk just now.\n\n" + "\n\n".join(parts) + "\n"
            )
        # RULING (Ryan, 2026-08-19): injected ONCE, not per turn. Re-sending
        # three files every turn is affordable at 1M context and is NOT on a
        # local 27B, where it crowds out the conversation itself. The text
        # below must not promise a per-turn refresh -- an instruction that
        # quietly stopped being true is worse than no instruction at all.
        return (
            "\n\n" + STORE_HEADER + "\n\n"
            "This is a SNAPSHOT taken at the start of the conversation, not a "
            "live view, and it is NOT re-sent each turn. If you have written to "
            "these files since, or need their current contents, read them with "
            "the `read` tool.\n\n" + "\n\n".join(parts) + "\n"
        )

    def _inject_store_once(self) -> None:
        """Merge the store into the system message, exactly once per conversation.

        Written INTO self.conversation and persisted with an `edit` record, not
        merged into the outgoing request only: a block that exists solely in the
        request must be rebuilt every turn, which is the cost being removed.
        """
        if self._store_injected:
            return
        if not self.conversation or self.conversation[0].get("role") != "system":
            return  # no system message yet; try again next turn
        current = self.conversation[0].get("content", "")
        if not isinstance(current, str):
            return
        if STORE_HEADER in current:
            self._store_injected = True  # resumed a convo that already has it
            return
        block = self._store_block()
        if not block:
            return  # empty store: nothing to inject, and no marker to leave
        self.conversation[0] = {**self.conversation[0], "content": current + block}
        self._store_injected = True
        if not self._convo_loading:
            self._edit(0, "store injected once")

    def _request_messages(self) -> list[dict]:
        """The conversation as sent. The store rides in message 0, injected once."""
        self._inject_store_once()
        return list(self.conversation)

    # \u2500\u2500 Persistence (.convos/<uuid>/convo.jsonl) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    #
    # Append-only JSONL. NEVER rewritten, never backed up and replaced: a torn
    # append costs one line (which the reader tolerates), whereas a failed
    # rewrite costs the whole conversation. The raw history stays on disk
    # behind every compaction, because the transcript is the archive and the
    # context is not.
    #
    # Record types, applied in file order:
    #   meta      once, first line: id / created / model
    #   msg       one appended message, verbatim
    #   edit      replace ONE message in place (index + new message)
    #   truncate  drop msgs[:keep_from], optionally keep msg 0 if it is the
    #             system prompt, then splice `prepend` in front. This is how
    #             /compact is recorded: it stores the two summary messages
    #             rather than a fresh copy of the whole conversation.
    #   snapshot  the entire message list. Still read for older files, but no
    #             longer written \u2014 it was the reason a long conversation grew
    #             by a full copy on every compaction and every Ctrl+T.

    def _new_convo(self) -> None:
        """STAGE a conversation: pick its id and paths, touch no disk.

        Nothing is created until the user actually says something. The id and
        paths are assigned here anyway, so the footer can name the conversation
        and /clear can report where it will live.
        """
        self.store.stage(str(uuid.uuid4()))
        self._refresh_ctx_label()   # the footer names the conversation
        self._sync_seat_identity()

    def _sync_seat_identity(self) -> None:
        """Point the seat at the CURRENT conversation.

        Called from the only two places convo_id changes. The seat id used to
        be a fresh uuid4 per PROCESS, so resuming a conversation joined the
        fleet as a stranger and left the previous id behind, still heartbeating
        at nothing. One conversation minted three ids in an evening and a task
        dispatched to the id last seen was never delivered — `send` exits 0
        either way, so the misdelivery is silent.

        🔴 THIS DOCSTRING USED TO CLAIM the seat is not re-registered here
        because "heartbeat() sends the same argv as register(), so the next tick
        registers the new id by itself". THAT SENTENCE WAS FALSE, and it sat
        three lines above the `registered = False` that defeated it:
        `heartbeat()` returns immediately unless `registered`. Nothing re-armed
        the seat, so after /new or /resume this app was invisible to `discover`,
        its id named no registry row, and the footer read "unregistered".
        Probed 2026-08-23: registered False, heartbeat False, transport 0 calls.

        `Seat.rebind()` is now the single transition, and it reports its own
        failure rather than leaving a seat that quietly stopped existing.
        """
        seat = getattr(self, "seat", None)
        if seat is None or not self.convo_id:
            return
        want = harness_mod.agent_id_for_convo(self.convo_id)
        if want == seat.agent_id:
            return

        # Whether a presence was actually held decides whether a False return
        # is news: at boot there is nothing to rebind yet (startup owns the
        # first registration, once the model id is known), and reporting that
        # as a failure would train the reader to ignore this line.
        was_registered = getattr(seat, "registered", False)
        try:
            ok = seat.rebind(want)
        except Exception as e:                     # never fatal — the app runs unharnessed
            seat.agent_id, ok = want, False
            seat.error = f"{type(e).__name__}: {e}"
        if was_registered and not ok:
            # Loud, once. An unregistered seat that says nothing is precisely
            # the state that hid this defect for its whole life.
            self._system(f"harness seat could not rebind ({seat.error or 'unknown'})")

    def _materialise_convo(self) -> None:
        """Create the staged conversation on disk. Idempotent.

        Writes meta, then a SNAPSHOT of whatever is already in memory — the
        system prompt is appended at boot, long before this runs, and the
        snapshot is what carries it into the file. ConversationRepository.read
        already handles `type: "snapshot"` by replacing the message list, so a
        conversation born here reads back identically to one written record by
        record.
        """
        if not self.store.pending:
            return
        if self.store.convo_dir is None or self.store.convo_path is None:
            return
        self.store.pending = False   # cleared FIRST: write_record below would
                                     # otherwise see pending and skip its writes
        self.store.seed()
        seat = getattr(self, "seat", None)
        self.store.record_meta(
            self.model_id,
            seat_name=seat.name if seat is not None else None,
            seat_id=seat.agent_id if seat is not None else None,
        )
        # Everything said before the first user message (the system prompt) was
        # held in memory only. This is where it reaches disk.
        if self.conversation:
            self._snapshot("materialised on first message")

    def _report_persist_error(self, msg: str) -> None:
        """How the store speaks. It owns no widgets, so the app lends it one.

        Called at most once per conversation — ConversationRepository.note_error
        returns the text only for the FIRST failure. A store that has gone away
        fails on every subsequent write, and repeating it would bury the
        conversation the user is still trying to have.
        """
        try:
            self._system(
                f"[save failed — this conversation is memory-only]\n{msg}"
            )
        except Exception:
            pass  # not mounted yet; /convos re-surfaces it

    def _note_persist_error(self, e: Exception) -> None:
        # Same route the store's own writers take — one path, so a failure
        # reads identically whoever noticed it first.
        self.store._raise_to_app(e)

    def _write_record(self, rec: dict) -> None:
        self.store.write_record(rec)

    def _append(self, msg: dict) -> None:
        """Append to the live conversation AND to disk. Single choke point."""
        self.conversation.append(msg)
        self.store.record_msg(msg)

    def _snapshot(self, reason: str = "") -> None:
        self.store.record_snapshot(self.conversation, reason)

    def _edit(self, index: int, reason: str = "") -> None:
        if not (0 <= index < len(self.conversation)):
            return self._snapshot(reason)  # shouldn't happen; degrade safely
        self.store.record_edit(index, self.conversation[index], reason)

    def _truncate(self, keep_from: int, prepend: list[dict], reason: str = "") -> None:
        keeps_system = bool(
            self.conversation and self.conversation[0].get("role") == "system"
        )
        self.store.record_truncate(keep_from, prepend, keeps_system, reason)


    @property
    def store(self) -> ConversationRepository:
        """The conversation store, created on first use.

        LAZY ON PURPOSE, and this is not defensive programming. `convo_id` and
        friends used to be plain attributes, so they worked on ANY instance --
        including the `LiteTUI.__new__(LiteTUI)` construction the seat tests use
        to get an app with no Textual mount. Creating the store only in
        __init__ made the very first setter call explode there, which would
        have turned this refactor into a behaviour change for every caller that
        does not run the full constructor. Found by tests/test_seat_rebind.py,
        8 failures, before this line existed.
        """
        st = self.__dict__.get("_store")
        if st is None:
            st = ConversationRepository(on_error=self._report_persist_error)
            self.__dict__["_store"] = st
        return st

    @store.setter
    def store(self, v: ConversationRepository) -> None:
        self.__dict__["_store"] = v

    # ── store state, surfaced under its original names ───────────────────
    # These read and write ConversationRepository. They exist so the ~40 call
    # sites that say `self.convo_dir` did not have to become `self.store.
    # convo_dir` in the same commit that moved the logic -- one change at a
    # time is what makes a behaviour-preserving refactor reviewable.
    @property
    def convo_id(self) -> str:
        return self.store.convo_id

    @convo_id.setter
    def convo_id(self, v: str) -> None:
        self.store.convo_id = v

    @property
    def convo_dir(self):
        return self.store.convo_dir

    @convo_dir.setter
    def convo_dir(self, v) -> None:
        self.store.convo_dir = v

    @property
    def convo_path(self):
        return self.store.convo_path

    @convo_path.setter
    def convo_path(self, v) -> None:
        self.store.convo_path = v

    @property
    def _convo_pending(self) -> bool:
        return self.store.pending

    @_convo_pending.setter
    def _convo_pending(self, v: bool) -> None:
        self.store.pending = v

    @property
    def _convo_loading(self) -> bool:
        return self.store.loading

    @_convo_loading.setter
    def _convo_loading(self, v: bool) -> None:
        self.store.loading = v

    @property
    def _persist_error(self):
        return self.store.persist_error

    @_persist_error.setter
    def _persist_error(self, v) -> None:
        self.store.persist_error = v

    # ── conversation store ───────────────────────────────────────────
    # Implementations moved to litetui.conversation.ConversationRepository.
    # These aliases are the reason no call site changed: `LiteTUI._read_convo`
    # and friends still resolve, so the 18 test files that call them by name
    # keep working. Aliases, not wrappers -- one implementation, not two.
    _read_convo = staticmethod(ConversationRepository.read)
    _fmt_size = staticmethod(ConversationRepository.fmt_size)
    _convo_label = staticmethod(ConversationRepository.label)
    _convo_title = staticmethod(ConversationRepository.title)
    _flatten = staticmethod(ConversationRepository.flatten)

    def _list_convos(self) -> list[tuple[Path, dict, list[dict]]]:
        """Returns (transcript_path, meta, messages) newest first."""
        return ConversationRepository.list_all()





    def _resume(self, path: Path) -> None:
        try:
            meta, msgs = self._read_convo(path)
        except OSError as e:
            self._system(f"Could not read {path.name}: {type(e).__name__}: {e}")
            return
        if not msgs:
            self._system(f"{path.name} holds no messages — not resuming.")
            return

        self.conversation = msgs
        # The staged conversation is abandoned WITHOUT being written — that is
        # the whole point of staging. Clearing the flag before reassigning the
        # paths also stops a later write from materialising the RESUMED store as
        # if it were new.
        self._convo_pending = False
        self.convo_path = path
        self.convo_dir = path.parent
        self.convo_id = meta.get("id") or path.parent.name
        self._sync_seat_identity()
        self._refresh_ctx_label()   # resumed into a different conversation
        # The restored system message already names THIS store (it was written
        # with this uuid), so it is not rebuilt — rebuilding would overwrite
        # whatever the agent or /system had changed it to.
        #
        # ⚠️ EXCEPT the fleet-identity sentence, which is machine-authored and
        # PROCESS-scoped: the agent id is minted per process, so the restored
        # copy names the seat that wrote it, not the one now serving. Replaying
        # it verbatim tells the model to answer mail as an id nothing can
        # deliver to. Rewrite that one sentence; leave the rest alone.
        self._sync_fleet_identity()
        (self.convo_dir / paths.MEMORIES_DIR).mkdir(parents=True, exist_ok=True)

        log = self.query_one("#chat-log")
        log.remove_children()
        users = assistants = tools = 0
        for m in msgs:
            role = m.get("role")
            text = self._flatten(m.get("content"))
            if role == "user":
                self._user_bubble(text, False)
                users += 1
            elif role == "assistant":
                if text:
                    w = self._assistant_bubble()
                    try:
                        w.body.set_markdown(text)
                    except Exception:
                        w.body.content = Text(text)
                    assistants += 1
            elif role == "tool":
                tools += 1
        # Tool traffic is summarised rather than replayed — the widgets carry
        # streamed state that cannot be faithfully reconstructed from the log.
        # It IS still in self.conversation, so the model sees all of it.
        note = f", {tools} tool result(s) restored to context but not redrawn" if tools else ""
        mem_dir = self.convo_dir / paths.MEMORIES_DIR
        n_mem = len(list(mem_dir.glob("*.md"))) if mem_dir.exists() else 0
        store = ", ".join(
            f for f in CONVO_SEED_FILES if (self.convo_dir / f).exists()
        ) or "none"
        self._system(
            f"Resumed {self.convo_id}\n"
            f"  {users} user / {assistants} assistant message(s){note}\n"
            f"  store: {store} · {n_mem} file(s) in {paths.MEMORIES_DIR}/\n"
            f"  appending to {self.convo_dir.name}/{path.name}"
        )
        self._scroll_down()


    def _update_header(self) -> None:
        if self.tools_enabled:
            # COUNTED, not quoted. "tools:4" was a literal from when there
            # were exactly four, and it stayed 4 while view_image, chrome,
            # pccontrol, ask_user_question, skill, harness and the MCP set
            # arrived — the header lied for weeks. _all_tools() is what the
            # MODEL is offered, so the header now derives from the same list.
            mode = f"tools:{len(self._all_tools())}"
        else:
            mode = "no tools"
        think = self.thinking_level or "default"
        cwd = str(Path.cwd())
        home = str(Path.home())
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]
        # Which ENGINE is serving is now a real question \u2014 two backends can
        # hold two different models resident. Named, not inferred.
        engine = "llama.cpp" if self.backend.name == "llamacpp" else "LM Studio"
        parts = [p for p in (engine, self.model_id, mode, f"think:{think}", cwd) if p]
        self.sub_title = " \u00b7 ".join(parts)
        # The footer carries the thinking level too, and it only refreshed on a
        # context update -- so /think changed the header instantly and left the
        # footer lying until the next completion came back.
        self._refresh_ctx_label()

    def action_toggle_tools(self) -> None:
        self.tools_enabled = not self.tools_enabled
        if self.conversation and self.conversation[0].get("role") == "system":
            # Rebuilt through the single builder. Hand-rolling it here is how
            # the store block gets dropped on the first Ctrl+T.
            self.conversation[0]["content"] = self._system_prompt_text()
            self._edit(0, "tools toggled")  # one message, not the whole list
        state = "ON (bash, read, write, web_fetch)" if self.tools_enabled else "OFF"
        self._system(f"Tools {state} — Ctrl+T to toggle")
        self._update_header()

    # ── Connection ───────────────────────────────────────────────

    @work(exclusive=True, group="init")
    async def _connect(self) -> None:
        try:
            # The llama backend may spawn its own server here (never loading
            # a model) or attach to LiteSuite's — either way, say which.
            status = await self.backend.ensure_running()
            if status != "ok":
                self._system(f"llama.cpp: {status}")
            # Rebuild the chat client ONLY when the base_url moved (attaching
            # can move it): a client built earlier would stream at a server
            # the control plane is no longer talking to. When it has NOT
            # moved, the existing client object survives — anything attached
            # to it (a test's stubbed create, a keep-alive pool) stays valid.
            new_base = self.backend.base_url().rstrip("/")
            if str(self.client.base_url).rstrip("/") != new_base:
                self.client = AsyncOpenAI(base_url=new_base, api_key="litetui")
            rows = await self.backend.list_models()
            SKIP = {"embed", "embedding"}
            rows = [
                r for r in rows
                if not any(s in r.key.lower() for s in SKIP)
            ]
            self.model_rows = {r.key: r for r in rows}
            self.available_models = [r.key for r in rows]
            if self.available_models:
                # A configured default wins when the server is serving it. `pin`
                # re-applies it on EVERY connect; without pin it only fills an
                # empty/invalid selection, so a mid-session /model switch sticks.
                want = self.settings.default_model
                if want and want in self.available_models:
                    if self.settings.pin_default_model or not self.model_id:
                        self.model_id = want
                if not self.model_id or self.model_id not in self.available_models:
                    # Prefer a LOADED model for the default pick. The native
                    # listing now includes every DOWNLOADED model (LM Studio
                    # parity), and defaulting to a cold one would point the
                    # first turn at a model that must JIT-load — or fail.
                    loaded = [
                        r.key for r in self.model_rows.values() if r.loaded
                    ]
                    self.model_id = (
                        loaded[0] if loaded else self.available_models[0]
                    )
                if want and want not in self.available_models:
                    # AFTER the pick, and reading its RESULT. This used to sit
                    # above the pick and name available_models[0] — a second,
                    # parallel copy of a decision the pick makes differently:
                    # it prefers a LOADED model, and it leaves an already-valid
                    # model_id alone. Either case made the line a lie about
                    # which model is answering. One decision, one reader.
                    self._system(
                        f"Default model {want!r} is not being served — using "
                        f"{self.model_id!r}. (/settings to change it.)"
                    )
                self._update_header()
                self._fetch_ctx_window()
                # 🔴 DO NOT auto-apply the context length here. This used to call
                # _apply_context_length() on EVERY connect, which shells out to
                # `lms load <model> --context-length N`. Every app boot — including
                # every test that constructs LiteTUI — therefore LOADED A MODEL.
                # Concurrent boots loaded several at once and can OOM the machine.
                # Loading weights is expensive and destructive to whatever is
                # already resident; it must be an explicit act, never a side
                # effect of connecting.
                self._system(f"Connected — model: {self.model_id}")
                if self.tools_enabled:
                    self._system(f"agent loop: up to {self.settings.tool_iterations} tool iterations per turn (/settings)")
                # The full model listing used to print HERE, on every launch.
                # It is a catalogue, not a greeting: it pushed the splash and the
                # first prompt off-screen to answer a question nobody asked at
                # boot. `/models` renders the same list on demand (plugins/
                # model_switch.py), so nothing is lost by staying quiet.
                if len(self.available_models) > 1:
                    self._system(f"{len(self.available_models)} models available — /models to list, /model <n> to switch")
            else:
                self.sub_title = "No model loaded"
                self._system(
                    "No chat model available from "
                    f"{'LM Studio' if self.backend.name == 'lmstudio' else 'llama.cpp'}"
                    f" at {self.backend.host()}"
                )
        except Exception as e:
            self.sub_title = "Disconnected"
            # The one that rots silently: it kept naming localhost after the
            # client could be pointed elsewhere, so the error blamed the wrong host.
            # Name the host we ACTUALLY tried — the backend knows it; with two
            # engines, settings.lm_host would blame the wrong SERVER, not just
            # the wrong hostname.
            self._system(f"Could not connect to {self.backend.host()} — {e}")

    # ── Context window readout (footer) ───────────────────────

    @property
    def ctx_label_text(self) -> Text:
        """The footer: who I am, how hard I am thinking, which conversation,
        and how full the window is.

        Built as ONE Text on the ONE existing label. Do not mount a widget per
        field: Footer RECOMPOSES, and a fixed `id` on a recomposed child raises
        DuplicateIds the moment the removal has not landed before the mount --
        that crashed the whole app once. The label is addressed by CLASS for the
        same reason; duplicate classes degrade to a stale label, not a traceback.

        Every field is individually hideable from /settings. Fields are appended
        in order and there is ONE return: an early return for a missing value is
        what previously made tok/s unreachable whenever the context window had
        not resolved.
        """
        s = self.settings
        sep = "  \u00b7  "
        t = Text()

        def add(chunk: str, style: str) -> None:
            if t.plain:
                t.append(sep, "#5c6370")
            t.append(chunk, style)

        # Identity, but only when the seat actually holds it. An unregistered
        # seat displaying a name it does not own is worse than showing nothing:
        # it is a green light for a registration that never happened.
        if s.footer_show_seat:
            seat = getattr(self, "seat", None)
            if seat is not None and getattr(seat, "registered", False):
                add(str(seat.name), "bold #7d8799")
            elif seat is not None:
                add("unregistered", "#e5534b")
            else:
                add("no seat", "#5c6370")

        if s.footer_show_thinking:
            add(f"think:{self.thinking_level or 'default'}", "#5c6370")

        if s.footer_show_convo and self.convo_id:
            add(self.convo_id[:8], "#5c6370")

        used, mx = self.ctx_used, self.ctx_max
        pct = (used / mx) if (used is not None and mx) else None
        # One scale for both the count and the percent, so they cannot disagree
        # about how alarming the same number is.
        ctx_style = (
            "bold #e5534b" if (pct is not None and pct >= 0.9)
            else ("#e8a33d" if (pct is not None and pct >= 0.7) else "#7d8799")
        )

        if s.footer_show_context:
            if used is None and mx is None:
                add("ctx \u2014", "dim")
            else:
                u = f"{used:,}" if used is not None else "\u2014"
                m = f"{mx:,}" if mx is not None else "?"
                # A NOT-LOADED model's number is its CEILING, not its window.
                # Printing it unmarked is how "ctx / 262,144" can sit in the
                # footer while LM Studio is about to serve the model at 8k.
                if mx is not None and not getattr(self, "ctx_loaded", False):
                    add(f"ctx {u} / {m} max", "#5c6370")
                else:
                    add(f"ctx {u} / {m}", ctx_style)

        # The percent was ALREADY computed to pick the colour above and then
        # discarded, so the footer knew how full the window was and made you do
        # the division. Shown as its own field so it can be kept when the raw
        # counts are hidden — for most turns the ratio is the only part anyone
        # actually reads.
        if s.footer_show_context_pct and pct is not None:
            add(f"{pct * 100:.0f}%", ctx_style)

        if s.footer_show_tps:
            self._append_tps_into(t, sep)

        return t

    def _append_tps_into(self, t: Text, sep: str) -> None:
        if self.tps is None:
            return
        if t.plain:
            t.append(sep, "#5c6370")
        # Coloured by how it FEELS to use, not by an absolute scale: this is a
        # local model on one GPU, and the number that matters is whether the
        # answer arrives faster than you read it.
        style = "#e5534b" if self.tps < 5 else ("#e8a33d" if self.tps < 15 else "#7d8799")
        t.append(tps_text(self.tps), style)

    def watch_ctx_used(self, value: int | None) -> None:
        self._refresh_ctx_label()
        # THE AMBIENT CHANNEL — the brain's base luminance, so a filling window
        # literally brightens it. Silent without ctx_max: "used out of unknown"
        # is not a fraction, and reporting 0.0 would draw an EMPTY window rather
        # than an unknown one, which is a different and wrong claim.
        if value and self.ctx_max:
            self._glassbox(
                "window_fill", value / self.ctx_max,
                f"{value:,}/{self.ctx_max:,}", discrete=True,
            )

    def _refresh_ctx_label(self) -> None:
        # query_one would raise TooManyMatches if a recompose ever left two
        # behind; update every match instead so a transient duplicate is
        # cosmetic rather than an exception on a hot reactive path.
        try:
            labels = list(self.query(".ctx-label"))
        except Exception:
            # No screen on the stack yet. _update_header and the conversation
            # setup both run before mount, and self.query() RAISES in that
            # window rather than returning empty -- so the "not composed yet"
            # guard below could never be reached from those callers.
            return
        if not labels:
            return  # footer not composed yet; it reads the value when it composes
        text = self.ctx_label_text
        for label in labels:
            label.content = text

    @work(exclusive=True, group="ctxload")
    async def _apply_context_length(self, force: bool = False) -> None:
        """Ask LM Studio to (re)load the active model at the configured window.

        A reload evicts the resident weights, so it must not happen when it
        would change nothing — and must not be SKIPPED when it would.

        `force` separates the two callers. A /settings save where the number
        changed is an explicit instruction and always applies, including
        LOWERING the window to free VRAM. A model switch only applies when the
        model is not already serving at least that much.

        🪤 THE OLD GUARD (`self.ctx_max == want`) COULD NEVER FIRE. LM Studio
        clamps: ask for 120,000 and it loads 120,064, so the readout never
        equals the request. Harmless with one caller on a changed value; a
        reload on every switch as soon as there are more. Compare against what
        was ASKED, and treat "at least as much" as satisfied.

        Reads the model's state FRESH rather than trusting self.ctx_max, which
        _fetch_ctx_window fills from a separate worker and may still be the
        PREVIOUS model's number at this moment.
        """
        want = self.settings.default_context_length
        if not want or not self.model_id:
            return

        # Only the non-forced path needs to know the current state. Reading
        # first when we are going to load regardless is a pure cost — and it
        # cost a real failure: the extra round-trip pushed the load past the
        # window a test waits in, so the explicit-change control went red.
        if not force:
            try:
                info = await self.backend.model_info(self.model_id)
            except Exception:
                info = None
            if info:
                cur, _typ, is_loaded = info
                if is_loaded and cur and cur >= want:
                    return

        self._system(f"Loading {self.model_id} at {want:,} tokens…")
        try:
            # The backend owns the HOW: LM Studio via the SDK (replacing the
            # `lms load` shell-out), llama.cpp via its router.
            await self.backend.load(self.model_id, ctx=want)
        except llm_backend.BackendError as e:
            # The backend's message already names its host/path — report the
            # server's own words, never a generic failure.
            self._system(str(e))
            return
        except Exception as e:
            self._system(f"Could not set context length: {type(e).__name__}: {e}")
            return
        # Re-read rather than assume: the request is what we asked for, the
        # readout is what we got, and the server may clamp to what fits in VRAM.
        self._fetch_ctx_window()

    @work(exclusive=True, group="ctx")
    async def _fetch_ctx_window(self) -> None:
        """Ask the backend for the active model's (window, type, loaded).

        The ceiling-vs-window contract (a model that is merely INSTALLED must
        never report its ceiling as a serving window — the 262,144-vs-8k
        footer lie) now lives in each backend's model_info; both preserve it.
        """
        if not self.model_id:
            return
        mid = self.model_id
        try:
            got = await self.backend.model_info(mid)
        except Exception:
            got = None  # server hiccup — footer just shows "?" for the window size
        if got:
            self.ctx_max, self.model_type, self.ctx_loaded = got
        else:
            self.ctx_max, self.model_type, self.ctx_loaded = None, None, False
        self._refresh_ctx_label()

    # ── Message display ──────────────────────────────────────────────────────────

    #: Matches the fleet-identity sentence so it can be REPLACED rather than
    #: duplicated. Anchored on both ends: a bare "id <uuid>" would also match
    #: ids quoted inside the conversation.
    _FLEET_LINE_RE = re.compile(
        r"You are registered in the LiteHarness fleet as [^(]*\(id [0-9a-fA-F-]{36}, tier [a-z]+\)\. ",
    )

    def _fleet_identity_sentence(self) -> str:
        return (
            f"You are registered in the LiteHarness fleet as "
            f"{self.seat.name} (id {self.seat.agent_id}, tier {self.seat.tier}). "
        )

    def _sync_fleet_identity(self) -> bool:
        """Make the system prompt name THIS process's seat.

        🔴 THE AGENT ID IS MINTED PER PROCESS, so the sentence is only true for
        the process that wrote it. A resumed conversation replays the one the
        PREVIOUS process wrote, and the model is then told to answer mail as an
        id nothing can deliver to — reported from inside the app, with its live
        seat and its prompt disagreeing.

        Rewrites only that sentence. `_resume` deliberately does not rebuild the
        system message (it would discard the agent's own /system edits) and that
        rule still holds for every other word of it.

        Returns True when it changed something.
        """
        if not self.conversation or self.conversation[0].get("role") != "system":
            return False
        body = self.conversation[0].get("content") or ""
        if not isinstance(body, str):
            return False
        want = self._fleet_identity_sentence()
        if want in body:
            return False
        fixed, n = self._FLEET_LINE_RE.subn(want, body, count=1)
        if not n:
            return False   # no line yet; the registration path appends it
        self.conversation[0]["content"] = fixed
        return True

    def _append_to_system(self, text: str) -> None:
        """Extend the FIRST system message rather than adding another one.

        Multiple role:"system" turns are not portable. qwen/qwen3.8-27b's chat
        template raises "System message must be at the beginning" and the request
        fails with a 500; other builds of the same model accept it. Anything the
        model must know belongs in the one system turn it is guaranteed to read.
        """
        if not self.conversation or self.conversation[0].get("role") != "system":
            self._append({"role": "system", "content": text})
            return
        current = self.conversation[0].get("content") or ""
        if text in current:
            return                      # idempotent across resume/re-register
        self.conversation[0] = {
            **self.conversation[0],
            "content": (current.rstrip() + "\n\n" + text) if current else text,
        }
        if not getattr(self, "_convo_loading", False):
            self._edit(0, "system prompt extended")

    def _clear_screen(self, *, note: str | None = None) -> None:
        """Wipe the RENDERED transcript. The conversation is untouched.

        Deliberately distinct from /clear, which resets the conversation and
        starts a new file. This is the display only — the model's context is
        exactly what it was a moment ago.

        Why it is needed at all: after a compaction the log still shows every
        message that was just REPLACED by the summary, and the compaction notice
        even said "Scrollback above is untouched." So the screen kept rendering
        context that no longer exists, and scrolling up read as history when it
        was a record of something the model can no longer see.
        """
        self.query_one("#chat-log").remove_children()
        if note:
            self._system(note)

    def _resync_ctx_if_stale(self) -> None:
        """Re-read the window once the model is genuinely resident.

        🔴 WITHOUT THIS, REFUSING TO GUESS BECOMES REFUSING TO EVER FIRE. At
        boot the selected model is often not loaded, so ctx_max is its ceiling
        and _maybe_autocompact correctly declines to divide by it. Then the
        first message makes LM Studio JIT-load the model — and nothing asks
        again, so ctx_loaded stays False for the whole session and auto-compact
        never runs at all.

        Turning a wrong number into no number is not a fix on its own; the
        reading has to be retaken once it can be right.
        """
        if not getattr(self, "ctx_loaded", False) and self.model_id:
            self._fetch_ctx_window()

    def _autocompact_due(self) -> int | None:
        """The window percent when a compaction is DUE, else None. A TEST — it
        never starts one.

        The policy itself is TurnEngine.autocompact_due, which is pure and can
        be exercised without a Textual mount; this reads the live values it
        needs off the app. See that function for why each refusal to answer is
        load bearing.
        """
        return TurnEngine.autocompact_due(
            enabled=self.settings.autocompact_enabled,
            at_percent=self.settings.autocompact_at_percent,
            ctx_max=self.ctx_max,
            ctx_used=self.ctx_used,
            ctx_loaded=bool(getattr(self, "ctx_loaded", False)),
            failed_at=getattr(self, "_autocompact_failed_at", None),
        )

    def _maybe_autocompact(self) -> None:
        """Start a compaction if one is due. SCHEDULE this — never call it from
        inside a chat-group worker. See _autocompact_due.

        Needs headroom by design. A threshold near 100 leaves no room for the
        compaction request itself to produce a summary, which is the failure it
        exists to prevent.
        """
        pct = self._autocompact_due()
        if pct is None:
            return
        if self._chat_running():
            # A chat-group worker is live, and _compact is exclusive in that
            # same group, so starting one here would CANCEL it.
            #
            # This replaces a bool that could not have worked: it was set, then
            # cleared in a `finally` wrapped around the call that starts the
            # compaction. _compact is @work, so that call SCHEDULES and returns
            # immediately — the flag went false again microseconds later while
            # the compaction was still in flight. It guarded the scheduling
            # call, never the compaction. Ask the worker manager, which knows,
            # rather than a flag that races the thing it guards.
            return
        self._system(
            f"Auto-compacting - context at {pct}% of "
            f"{self.ctx_max:,} (threshold {self.settings.autocompact_at_percent}%)."
        )
        self._compact_is_auto = True
        self._handle_command("/compact")

    # ── glass box: the turn, as signal ───────────────────────────────────
    #
    # Tier 1 of the thermal-brain wiring. Every channel below is an event
    # LiteTUI ALREADY produces — this adds no new signal, only a transport, so
    # nothing here may change behaviour or cost anything when unobserved.

    def _glassbox(self, channel: str, intensity: float = 1.0, label: str = "",
                  *, discrete: bool = False) -> None:
        """Fire one channel at whatever plugins are watching.

        THE EMPTY-OBSERVER SHORT CIRCUIT IS FIRST AND MUST STAY FIRST. The
        thinking and output branches call this once per token, so with no
        observers the whole feature has to cost one attribute read and a falsy
        list check — not a clock read, not a dict write. A user who has not
        installed the brain must not pay for it.

        `discrete` means "this is an event, not a level". A tool call, a store
        write, a ledger and a window-fill change are things that HAPPENED;
        throttling one loses it. thinking and output are levels sampled per
        token, where two consecutive samples say the same thing.

        Never raises: PluginRegistry.emit already swallows a failing observer
        and records it on that plugin's status row.
        """
        reg = getattr(self, "plugins", None)
        if reg is None or not reg.observers:
            return
        if not discrete:
            now = time.monotonic()
            if now - self._gb_last.get(channel, 0.0) < GLASSBOX_MIN_INTERVAL_S:
                return
            self._gb_last[channel] = now
        reg.emit({"channel": channel, "intensity": float(intensity), "label": label})

    def _glassbox_tool(self, name: str) -> None:
        """A dispatch fires ONE channel, never both.

        `write` is store_write rather than tool_call because the design treats
        the agent changing durable state as a different event from the agent
        calling something — and firing both would double-count every write in
        whatever the observer is drawing.
        """
        channel = "store_write" if name == "write" else "tool_call"
        self._glassbox(channel, 1.0, name, discrete=True)

    def _glassbox_rate(self, channel: str) -> None:
        """Continuous channel whose intensity is the live tok/s, normalised
        against a fast-but-reachable ceiling so the common case has headroom
        rather than sitting pinned at 1.0."""
        tps = self.tps or 0.0
        self._glassbox(channel, min(1.0, tps / 60.0), tps_text(tps) if tps else "")

    def system_message(self, text: str) -> None:
        """Post a system line into the chat log — **the supported way for a
        plugin to say something to the user.**

        Public because `app._system(...)` was **49 of the 99** private
        reach-throughs from `plugins/`: half the coupling in the tree, every hit
        a call, zero reads, return value unused. A public, documented method
        *is* a supported API; the metric this moves is **private** reach-through.

        🔴 **WHY NOT `ctx.notify`, which was the approved plan.** It was
        unbuildable: **all 49 call sites have `app` in scope and NOT `ctx`.**
        Command handlers are module-level functions `(app, name, arg)`
        registered from inside `_register(ctx)` but **not closures over it**
        (`plugins/__init__.py:165`, `app.py:5187`), so a handler cannot reach
        `ctx` even in principle. `ctx.notify` would have typechecked, tested
        green, merged, and moved reach-through **99 → 99**.

        📌 The lesson that cost, generalised: counting *accesses* tells you a
        coupling exists; it does not tell you what a replacement would have
        **in scope** to replace it with. Ask "is X reachable from every call
        site?" before scheduling any "seal it behind X" step.
        """
        log = self.query_one("#chat-log")
        log.mount(ChatMessage(Text(text), classes="system-msg"))
        self._scroll_down()

    # Compatibility alias. Keeps the 62 in-file call sites and every not-yet-
    # migrated plugin working while S1 lands in two commits (arrival here,
    # call-site rewrite in plugins/).
    # Dropped only once `grep -rn "\._system(" src/` is empty — VERIFIED BEFORE
    # DELETING, NOT AFTER.
    _system = system_message

    def _user_bubble(self, text: str, has_image: bool, queued: bool = False):
        log = self.query_one("#chat-log")
        parts: list[str] = []
        if has_image:
            parts.append("[Image attached]")
        if text:
            parts.append(text)
        w = ChatMessage(Text("\n".join(parts)), classes="user-msg")
        # A message that silently waits is indistinguishable from one that was
        # dropped — the title is the visibility.
        w.border_title = "You · queued" if queued else "You"
        log.mount(w)
        self._scroll_down()
        return w

    def _assistant_bubble(self) -> AssistantMessage:
        log = self.query_one("#chat-log")
        w = AssistantMessage()
        w.border_title = "AI"
        log.mount(w)
        self._scroll_down()
        return w

    # -- tok/s -----------------------------------------------------------
    #
    # Timed from the FIRST token, not from the request, so this is generation
    # speed and not generation-plus-prompt-processing. On a 262k-context model
    # prompt processing can dominate, and folding it in would report a number
    # that says more about the prompt than about the model.

    def _tps_start(self) -> None:
        self._tps_t0 = None
        self._tps_n = 0
        self._tps_painted = 0.0

    def _tps_tick(self) -> None:
        """One streamed delta arrived. Live estimate only -- see _tps_final."""
        now = time.monotonic()
        if self._tps_t0 is None:
            self._tps_t0 = now
            return          # nothing to divide by yet
        self._tps_n += 1
        elapsed = now - self._tps_t0
        # Repaint at most 4x/second. The footer is one Static, but this runs on
        # every token of every turn, and a repaint per token on a 27B is a real
        # cost paid to render a number that changes in the third decimal.
        if elapsed >= 0.4 and now - self._tps_painted >= 0.25:
            self.tps = self._tps_n / elapsed
            self._tps_painted = now

    def _tps_final(self, completion_tokens: int) -> None:
        """Settle to the exact figure the server reports.

        The live number counts STREAM DELTAS, which are only approximately
        tokens. `usage.completion_tokens` is the server's own count and includes
        reasoning tokens, so it matches what the model actually generated.
        """
        if self._tps_t0 is None or not completion_tokens:
            return
        elapsed = time.monotonic() - self._tps_t0
        if elapsed > 0:
            self.tps = completion_tokens / elapsed

    def watch_tps(self, value: float | None) -> None:
        self._refresh_ctx_label()

    # -- elapsed time while a turn is in flight -----------------------
    # Ryan's "..." read as nothing happening while LM Studio chewed the prompt.
    # While no answer token has arrived (and while tool calls run), the bubble
    # shows live ELAPSED TIME. The display string is a pure function
    # (render_progress); this is only the repaint glue. The repaint task runs
    # on the worker's own event loop, interleaving with the `async for` stream.

    def _elapsed_cancel(self) -> None:
        task = self._elapsed_task
        if task is not None and not task.done():
            task.cancel()
        self._elapsed_task = None

    def _elapsed_start(self, body) -> None:
        self._elapsed_cancel()
        self._elapsed_body = body
        self._elapsed_body_t0 = time.monotonic()
        try:
            self._elapsed_task = asyncio.create_task(self._elapsed_repaint())
        except RuntimeError:
            self._elapsed_task = None

    def _elapsed_stop_body(self) -> None:
        self._elapsed_body = None

    def _tool_begin(self, tool) -> None:
        if tool not in self._inflight_tools:
            self._inflight_tools.append(tool)
            # Mount the cancel control immediately after THIS tool, so it sits
            # against the timer counting this call up.
            #
            # `tool.parent is None` is NOT an error case to swallow -- it is the
            # normal case when the caller announces the tool before mounting it,
            # which is exactly what _stream used to do. Skipping quietly meant
            # the control was never created at all: correctly placed by every
            # test, and invisible in the app. Defer instead, and the mount order
            # of the caller stops mattering.
            self._attach_cancel_button(tool)
            # Fired here rather than at dispatch because THIS is the point the
            # app itself treats as "a tool is now running" — same condition the
            # timer and the cancel control key off, so the brain cannot light
            # up for a call the app does not consider in flight.
            self._glassbox_tool(getattr(tool, "tool_name", "") or "tool")
        if self._elapsed_task is None or self._elapsed_task.done():
            try:
                self._elapsed_task = asyncio.create_task(self._elapsed_repaint())
            except RuntimeError:
                self._elapsed_task = None

    def _attach_cancel_button(self, tool, _retry: bool = False) -> None:
        """Put a cancel control directly after `tool`, whenever that becomes possible.

        Best-effort by design: failing to decorate a tool call must never take
        down the tool call. But "best-effort" previously covered an unmounted
        tool too, and that is a bug wearing a guard's clothes -- the one
        condition it silently tolerated was the one that always happened.
        """
        if tool not in self._inflight_tools or tool in self._cancel_buttons:
            return  # finished, or already decorated
        parent = tool.parent
        if parent is None:
            if not _retry:
                # Not mounted YET. Come back after the next refresh, by which
                # time the caller's own mount has landed.
                self.call_after_refresh(self._attach_cancel_button, tool, True)
            return
        try:
            btn = CancelToolButton()
            self._cancel_buttons[tool] = btn
            parent.mount(btn, after=tool)
        except Exception:
            self._cancel_buttons.pop(tool, None)

    def _tool_end(self, tool) -> None:
        if tool in self._inflight_tools:
            self._inflight_tools.remove(tool)
        btn = self._cancel_buttons.pop(tool, None)
        if btn is not None:
            try:
                btn.remove()
            except Exception:
                pass

    def _thinking_done(self) -> None:
        """The trace stopped streaming: the first content token, the
        first tool call, or the turn's end. Idempotent - every exit path
        calls it, so it acts exactly once, and with _thinking_live back
        to None the _elapsed_repaint gate is False: the header can no
        longer count. That is the 'timer stops at the right moment'
        control."""
        t = self._thinking_live
        if t is None:
            return
        self._thinking_live = None
        t.finalize()
        t.reset_header()

    async def _elapsed_repaint(self) -> None:
        # Repaint the in-flight bubble and any running tool calls ~4x/sec.
        # Self-retires after ~1s with nothing active, so it never outlives the
        # turn by long; _elapsed_start also cancels a lingering one.
        idle = 0.0
        while True:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            card = self._compact_card
            card_live = card is not None and card._took is None
            active = (
                self._elapsed_body is not None
                or self._inflight_tools
                or self._thinking_live is not None
                or card_live
            )
            # The cancel button tracks the PROCESS, not the tool bubble: only
            # a live subprocess is cancellable, and a button shown for a tool
            # with nothing to kill would be a control that does nothing.
            try:
                live = ttyguard.CANCELLABLE["proc"] is not None
                for btn in self.query(CancelToolButton):
                    btn.set_class(live, "visible")
            except Exception:
                pass
            if active:
                if self._elapsed_body is not None:
                    # ETA: project this turn's prompt-processing time from the
                    # previous turn's token count and the learned median rate.
                    # Before any reliable turn both are None and the pure fn
                    # yields elapsed-only (the honest state).
                    self._elapsed_body.content = render_progress(
                        self._elapsed_body_t0, now,
                        self._eta_estimate_tokens(), self._eta_learned_rate())
                if self._thinking_live is not None:
                    # The app owns the tps reactive; the block only renders it.
                    self._thinking_live.repaint_header(self.tps)
                for tool in self._inflight_tools:
                    tool._tick()
                if card_live:
                    card.tick()
                idle = 0.0
            else:
                idle += 0.25
                if idle >= 1.0:
                    return

    # -- ETA on prompt processing (commit 2) ---------------------------
    #
    # The KV-cache is the whole reason this exists: LM Studio reuses the
    # cached prefix, so a cache-hit turn reprocesses almost nothing in a few
    # ms while the first turn after a compaction reprocesses the whole context.
    # A rate learned from a cache-hit turn is a misleadingly LOW prompt-eval
    # speed (fixed overhead dominates when few tokens are reprocessed), and
    # applying it to a 100k-token turn predicts minutes for a turn that takes
    # seconds. So the rate is ONLY learned from turns whose first-token latency
    # reaches a floor (is_reliable_rate_sample) -- turns that demonstrably
    # reprocessed the prompt. A confidently wrong ETA is worse than an honest
    # elapsed counter, so until such a turn exists the body shows elapsed only.

    def _eta_record_first_delta(self) -> None:
        """Stamp the first delta of the current turn. first_token_s is then
        _eta_first_delta - _elapsed_body_t0 (the turn start). Idempotent: only
        the first delta sets it, so later deltas don't move it."""
        if self._eta_first_delta is None:
            self._eta_first_delta = time.monotonic()

    def _eta_learn(self, prompt_tokens) -> None:
        """End of a turn: remember this turn's token count as the ESTIMATE for
        the next turn's ETA, and if this turn is a reliable sample (first-token
        latency at the floor), fold its rate into the median. A cache-hit turn
        does not touch the median, so its misleadingly low rate never pollutes
        the ETA."""
        if prompt_tokens:
            self._eta_last_prompt_tokens = int(prompt_tokens)
        first = self._eta_first_delta
        self._eta_first_delta = None
        if first is not None and self._elapsed_body_t0 > 0.0:
            first_token_s = first - self._elapsed_body_t0
            if is_reliable_rate_sample(prompt_tokens, first_token_s):
                self._eta_samples.append(prompt_tokens / first_token_s)

    def _eta_learned_rate(self):
        """The rolling median of reliable rate samples, or None if none yet.
        A None rate makes render_progress elapsed-only, the honest state before
        a single reliable turn has happened."""
        if not self._eta_samples:
            return None
        return statistics.median(self._eta_samples)

    def _eta_estimate_tokens(self):
        """The token count the next turn's ETA is projected from: the most
        recent real count. None until the first turn reports usage."""
        return self._eta_last_prompt_tokens

    def _warn_reasoning_ignored(self) -> None:
        """The server sent a reasoning trace after we asked for none.

        That is proof the `reasoning_effort` field was skipped rather than
        honoured -- the request returned 200, so nothing else can tell us. Said
        once per session: it is a property of the loaded model, not of the turn.
        """
        if self._reasoning_ignored_warned:
            return
        self._reasoning_ignored_warned = True
        self._system(
            "Thinking is set to 'off', but the server sent a reasoning trace "
            "anyway.\n"
            "LM Studio SKIPPED reasoning_effort='none' for this model instead of "
            "refusing it -- a virtual model carries its own narrower set of "
            "levels, and a value outside it is dropped with a 200.\n"
            f"The model is now reasoning at the SERVER DEFAULT (xhigh), the most "
            f"expensive setting. Use /think {LEAST_THINKING_FALLBACK} for the "
            "least thinking this model actually supports."
        )

    def _still_following(self, log) -> bool:
        """Has the READER moved, or has the CONTENT moved?

        _at_bottom could not tell these apart, and they want opposite answers:
        a reader who scrolled up must be left alone, a reader whom the content
        outran must be caught up. Both look identical in the geometry it asked
        about -- scroll_y sitting below max_scroll_y.

        What separates them is which number moved. Growing content raises
        max_scroll_y and leaves scroll_y exactly where it was; only a human
        moves scroll_y. So compare against the position we ourselves last
        scrolled to, and content growth becomes invisible to the check.

        Concretely, the case that kept coming back: .thinking-body is
        `max-height: 10`, so a thinking block grows the log ~12 rows in a burst
        and then never grows again. Against max_scroll_y that burst instantly
        exceeded the 2-line slack and following was refused for the rest of the
        turn, with nothing left to restore it. Against our own anchor the burst
        does not register at all.

        The slack survives for the reason it was introduced: scroll_y is a float
        and lands fractionally. Unset anchor means nothing has been scrolled yet,
        which is trivially still following.
        """
        anchor = self._follow_anchor
        if anchor is None:
            return True
        try:
            return log.scroll_y >= anchor - 2
        except Exception:
            # Geometry unavailable: fail OPEN, exactly as _at_bottom does. An
            # over-eager scroll is a visual nit; a dead autoscroll is this bug.
            return True

    def _scroll_down(self, *, only_if_following: bool = False) -> None:
        """Scroll the conversation log to the bottom.

        Returns immediately when the `autoscroll` setting is off. That switch is
        about the STREAM: discrete events still scroll, because those are the
        user's own action and jumping to the bottom is what they asked for.

        `only_if_following` is for the STREAMING path. An unconditional
        scroll_end during a long thinking trace yanks the viewport away from a
        reader who deliberately scrolled up -- worse than the missing autoscroll
        it would be fixing. During a stream we follow the tail only when the
        reader was already at the tail.

        Discrete events (a new bubble, a tool call, the final render, AND a new
        thinking block appearing) still scroll unconditionally: those are the
        user's own action or the start/end of a turn, where jumping to the
        bottom is what they want.

        A NEW THINKING BLOCK WAS THE ONE MISSING FROM THAT LIST, and it read as
        "the log only moves once the answer arrives". Mounting the assistant
        bubble plus the block grows the log by more than 2 lines in a single
        frame, so the reader was judged to have scrolled up -- by the app's OWN
        newly mounted content -- and following was refused for the whole turn.
        The guard written to protect a reader who scrolled up was firing on
        content nobody had scrolled away from.

        That is now handled where it belongs, in _still_following: the check no
        longer asks "are we at the bottom" (which content growth falsifies) but
        "has the reader moved away from where WE last scrolled" (which only a
        human can do). Both fixes were needed -- deferring the mount-time scroll
        until after measurement, AND anchoring the follow test -- and shipping
        only the first one left the bug looking untouched.
        """
        # The setting gates the STREAM path only. `only_if_following` is what the
        # stream passes, so guarding on it keeps discrete events (new bubble,
        # tool call, final render) scrolling as before.
        if only_if_following and not self.settings.autoscroll:
            return
        log = self.query_one("#chat-log")
        if only_if_following and not self._still_following(log):
            return
        log.scroll_end(animate=False)
        # Remember where we put it. This is the whole fix: the next follow check
        # compares against THIS, not against a bottom that keeps moving away.
        self._follow_anchor = log.scroll_y

    # ── Image handling ───────────────────────────────────────────

    def watch_pending_image(self, value: str | None) -> None:
        indicator = self.query_one("#image-indicator")
        if value:
            indicator.add_class("visible")
        else:
            indicator.remove_class("visible")

    _IMG_RE = r"(?:png|jpe?g|gif|webp|bmp)"

    @classmethod
    def _looks_like_image_path(cls, text: str) -> bool:
        return bool(re.search(rf"\.{cls._IMG_RE}\b", text, re.I))

    @classmethod
    def _split_image_path(cls, text: str) -> tuple[Path | None, str]:
        """Pull an image path off the front of the input; return (path, rest).

        The original accepted ONLY a bare path as the entire message, so the two
        most natural inputs both fell through to plain text with no warning:
        Windows "Copy as path" (Shift+Right-click) wraps the path in QUOTES, and
        typing a question after the path made Path(text) nonexistent. The model
        then received a filename as prose, correctly said it could not see an
        image, and started writing an OCR tool to work around its own blindness.
        """
        s = text.strip()

        # 1. quoted path, optionally followed by a prompt
        m = re.match(r"""^(["'])(.+?)\1\s*(.*)$""", s, re.S)
        if m:
            p = Path(m.group(2))
            if p.suffix.lower() in IMAGE_EXTS and p.exists():
                return p, m.group(3).strip()
            return None, text

        # 2. the whole input is a path (may contain spaces)
        p = Path(s)
        if p.suffix.lower() in IMAGE_EXTS and p.exists():
            return p, ""

        # 3. a path ending at an image extension, then a prompt. Non-greedy so
        #    "C:\My Folder\shot.png what is this" splits at the FIRST extension.
        m = re.match(rf"^(\S.*?\.{cls._IMG_RE})\s+(.*)$", s, re.I | re.S)
        if m:
            p = Path(m.group(1))
            if p.exists():
                return p, m.group(2).strip()

        return None, text

    def _clipboard_text(self) -> str | None:
        """OS clipboard as text, or None. Windows-first, then a portable path.

        tkinter is stdlib and in-process, so it is tried first; it raises when
        the clipboard holds no text (an image, or nothing), which is a MISS and
        not an error. PowerShell is the fallback because a hidden Tk root can
        fail outright in some hosts, and a paste that dies silently is the bug
        being fixed here.
        """
        try:
            import tkinter

            root = tkinter.Tk()
            root.withdraw()
            try:
                return root.clipboard_get()
            finally:
                root.destroy()
        except Exception:
            pass
        try:
            out = ttyguard.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                timeout=5,
            )
            if out.returncode == 0 and out.stdout:
                return out.stdout.rstrip("\r\n")
        except Exception:
            pass
        return None

    def _insert_into_input(self, text: str) -> int:
        """Splice text at the cursor. Returns the number of chars inserted."""
        # A multi-line paste would otherwise submit on the first newline and
        # drop the rest; join instead so nothing is lost.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if "\n" in text:
            text = " ".join(part for part in text.split("\n") if part.strip())
        if not text:
            return 0
        inp = self.query_one("#message-input", Input)
        pos = inp.cursor_position
        inp.value = inp.value[:pos] + text + inp.value[pos:]
        inp.cursor_position = pos + len(text)
        inp.focus()
        return len(text)

    def on_paste(self, event: events.Paste) -> None:
        """Bracketed paste, when the terminal does send it."""
        if not event.text:
            return
        # Only take over while the Input is focused; a modal has its own.
        if isinstance(self.screen, ModalScreen):
            return
        if self._insert_into_input(event.text):
            event.stop()

    def action_paste_text(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        text = self._clipboard_text()
        if not text:
            self.notify(
                "No text in clipboard. Ctrl+O pastes an image.",
                severity="warning", timeout=3,
            )
            return
        n = self._insert_into_input(text)
        if n:
            self.notify(f"Pasted {n} chars", timeout=1)

    def action_paste_image(self) -> None:
        try:
            from PIL import ImageGrab, Image as PILImage

            img = ImageGrab.grabclipboard()
            if isinstance(img, list):
                # Copying a FILE in Explorer puts a path LIST on the clipboard,
                # not a bitmap. Previously this was not None, so it fell through
                # to img.size and died as "Paste failed: 'list' has no size".
                for entry in img:
                    p = Path(str(entry))
                    if p.suffix.lower() in IMAGE_EXTS and p.exists():
                        b64 = self._load_image_file(p)
                        if b64:
                            self.pending_image = b64
                            self.notify(f"Image attached from file: {p.name}", timeout=2)
                            return
                self.notify(
                    "Clipboard holds file paths, but no readable image among them",
                    severity="warning", timeout=4,
                )
                return
            if img is None:
                self.notify(
                    "No image in clipboard. Copy an image, or paste its path into the box.",
                    severity="warning", timeout=4,
                )
                return
            if max(img.size) > MAX_IMAGE_DIM:
                img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), PILImage.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self.pending_image = base64.b64encode(buf.getvalue()).decode()
            w, h = img.size
            self.notify(f"Image attached ({w}x{h})", timeout=2)
        except ImportError:
            self.notify(
                "Pillow required: pip install Pillow", severity="error", timeout=5
            )
        except Exception as e:
            self.notify(f"Paste failed: {e}", severity="error", timeout=3)

    # ── Modal callbacks ──────────────────────────────────────────

    def _on_model_picked(self, model_id: str | None) -> None:
        if not model_id or model_id == self.model_id:
            return
        self.model_id = model_id
        self._update_header()
        self._fetch_ctx_window()
        self._system(f"Switched to: {self.model_id}")
        self._apply_context_length()

    def _on_convo_picked(self, path_str: str | None) -> None:
        if not path_str:
            return
        path = Path(path_str)
        if path == self.convo_path:
            self._system("Already in that conversation.")
            return
        self._resume(path)

    # ── Stopping a turn ──────────────────────────────────────────

    def _chat_running(self) -> bool:
        return any(
            w.group == "chat" and w.state is WorkerState.RUNNING for w in self.workers
        )

    def action_cancel_tool(self) -> None:
        """Kill the in-flight bash tree; the TURN carries on with an honest
        result the model can react to. Esc (action_stop_turn) stays what it
        is: stop the whole turn. Two different verbs, deliberately."""
        proc = ttyguard.CANCELLABLE["proc"]
        if proc is None or proc.poll() is not None:
            self.notify("No cancellable tool is running", timeout=2)
            return
        # Set on THIS thread, before the kill is dispatched: _run_shell reads
        # this the moment communicate() returns, which can be as soon as the
        # tree dies.
        ttyguard.CANCELLABLE["cancelled"] = True
        self.notify("Cancelling…", timeout=2)
        self._cancel_tool_tree(proc.pid, proc)

    @work(thread=True, group="cancel")
    def _cancel_tool_tree(self, pid: int, proc=None) -> None:
        """taskkill, OFF THE UI THREAD, then say what actually happened.

        🔴 THIS USED TO RUN INLINE IN action_cancel_tool. kill_tree blocks for
        up to KILL_TREE_TIMEOUT_S, and taskkill against this app's own
        cmd.exe -> python tree was measured at median 6.38s and max 43.06s —
        so pressing cancel froze the entire TUI for up to the full budget.
        Reporting honestly makes that WORSE, not better: an honest message has
        to wait for the answer it is being honest about, and a message the user
        cannot see because the app stopped responding has served them no better
        than the lie it replaced.

        thread=True rather than the async @work used elsewhere in this file:
        the call is BLOCKING, not awaitable. Wrapping it in asyncio.to_thread
        inside an async worker would work identically and would put a second
        `asyncio.to_thread(...)` in the package, which is the exact string the
        tool-authority gate looks for — a true reading with a misleading shape.
        Textual has a thread worker; this is what it is for.
        """
        killed = ttyguard.kill_tree(pid, proc)
        # Handed to the result formatter through the envelope, so the MODEL is
        # told the same thing the user is. Two audiences, one truth.
        #
        # ORDERING, which is benign in the direction that matters: if the kill
        # SUCCEEDS, communicate() may return before this line runs and the
        # formatter reads the default True — which is the correct answer. If
        # the kill FAILS, the tree is still holding the pipe open, so
        # communicate() is still blocked and cannot read anything until well
        # after this line. The failure case, the one that must not be
        # mis-reported, is the one that is ordered.
        ttyguard.CANCELLABLE["kill_confirmed"] = killed
        if killed:
            self.call_from_thread(
                self.notify,
                "Tool cancelled — the model sees what it wrote so far",
                timeout=3,
            )
        else:
            # Never a false confirmation. We do not claim it is still running;
            # we claim we could not confirm that it stopped, and the user is
            # the one who can check.
            self.call_from_thread(
                self.notify,
                "Cancel could not be confirmed — the process may still be running",
                severity="warning",
                timeout=6,
            )

    def action_stop_turn(self) -> None:
        if not self._chat_running():
            # Escape with nothing running should be inert, not a dialog.
            return
        if self._stop_requested:
            # Already asked nicely. A model that has not produced a chunk since
            # cannot see the flag, so the second escape is the hard kill.
            self.workers.cancel_group(self, "chat")
            self._system("[force-stopped — no partial reply was recoverable]")
            self._stop_requested = False
            # Marked HERE as well as at _stream's stop branch: a cancelled
            # worker never reaches that branch, so without this the HARDER of
            # the two stops would be the one the wake ping ignored.
            self._turn_abandoned = True
            return
        self.push_screen(ConfirmStop(), self._on_stop_answer)

    def _on_stop_answer(self, stop: bool | None) -> None:
        if not stop:
            return
        self._stop_requested = True
        self.notify(
            "Stopping… (Esc again to force, if it is not responding)", timeout=4
        )

    def action_clear_image(self) -> None:
        if self.pending_image:
            self.pending_image = None
            self.notify("Image removed", timeout=2)

    def _load_image_file(self, path: Path) -> str | None:
        try:
            from PIL import Image as PILImage

            img = PILImage.open(path)
            if max(img.size) > MAX_IMAGE_DIM:
                img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), PILImage.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None

    def _tool_view_image(self, args: dict) -> str:
        """Stage an image for the model to actually see. Never raises.

        Returns a short CONFIRMATION, not the image. See plugins/view_image.py
        for why returning the bytes here would show the model nothing.
        """
        raw = str(args.get("path") or "").strip().strip('"').strip("'")
        if not raw:
            return "[error] view_image: `path` is required (absolute path to an image file)"

        # State the vision precondition rather than letting the server 400.
        # LM Studio accepts images only when the model loads as type "vlm"; a
        # GGUF shipped without an mmproj projector loads as "llm" and rejects
        # the request with an HTTP 400 that says nothing about projectors.
        if self.model_type == "llm":
            return (
                f"[error] view_image: the loaded model ({self.model_id}) is type 'llm', "
                "not 'vlm' -- it has no vision projector (mmproj), so images cannot be "
                "sent to it at all. Load a vision build of the model first."
            )

        path = Path(raw)
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.exists():
            return f"[error] view_image: no such file: {path}"
        if not path.is_file():
            return f"[error] view_image: not a file: {path}"
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            return (
                f"[error] view_image: {path.suffix or 'no extension'} is not a supported "
                "image type (png, jpg, jpeg, gif, webp, bmp)"
            )

        b64 = self._load_image_file(path)
        if b64 is None:
            return f"[error] view_image: could not decode {path.name} as an image"

        self._pending_tool_images.append((str(path), b64))
        kb = len(b64) * 3 // 4 // 1024
        return (
            f"Attached {path.name} ({kb} KB encoded). It is in the next message -- "
            "look there, not here."
        )

    # ── Chat logic ───────────────────────────────────────────────

    @on(Input.Changed, "#message-input")
    def _skill_ac_changed(self, event: Input.Changed) -> None:
        self.sync_skill_autocomplete(event.value)

    def sync_skill_autocomplete(self, value: str) -> None:
        """Show the picker only while a bare slash NAME is being typed.

        The trigger is deliberately narrow: `/` plus name characters and
        nothing else. Once a space is typed the user is writing arguments, and
        a list that keeps reopening under an argument is noise.
        """
        ac = getattr(self, "_skill_ac", None)
        if ac is None:
            return
        text = value or ""
        # 🔴 The guard asks about the TRIGGER, never about the library. It used
        # to read `not self.skills`, so an empty skills library switched off
        # completion for every COMMAND as well — the app's own surface made
        # unreachable by the absence of an optional add-on. refilter's own
        # return already handles "nothing matched".
        if not text.startswith("/") or " " in text:
            ac.dismiss_list()
            return
        ac.refilter(self._completion_candidates(), text[1:])

    def _completion_candidates(self) -> list[Completion]:
        """Every slash name the picker can complete to, commands first.

        Commands are folded to ONE row per CommandEntry on its primary token.
        `plugins.commands` is keyed by every ALIAS — 41 keys over 27 entries
        today — so reading it directly offers /model twice and /new three times.

        Ordering reuses `plugins.palette_sort_key` rather than minting a second
        table. A hand-authored copy of an order is the drift class the derived
        palette already replaced, and it would silently misplace any command
        nobody remembered to add.

        A skill whose name a command already claims is DROPPED, because
        `_handle_command` resolves a bare name to a skill ONLY when no command
        matches. Offering it would advertise something Enter will never do.

        Rebuilt per keystroke on purpose: /skills can refresh the library
        mid-session (see the reload path), and a cached list would go stale
        exactly when someone had just added the skill they are now typing.
        """
        out: list[Completion] = []
        taken: set[str] = set()
        # Keyed by `tokens`, which is unique per entry and hashable — the dict
        # is what collapses the aliases.
        for entry in {e.tokens: e for e in self.plugins.commands.values()}.values():
            name = entry.tokens[0].lstrip("/")
            if not name or name.lower() in taken:
                continue
            taken.add(name.lower())
            out.append(Completion(
                name, entry.help, "command",
                (0,) + plugins_mod.palette_sort_key(entry.group, entry.order, name),
            ))
        for s in self.skills:
            if s.name.lower() in taken:
                continue
            taken.add(s.name.lower())
            out.append(Completion(
                s.name, s.description or "", "skill", (1, 0, 0, s.name.lower()),
            ))
        out.sort(key=lambda c: c.sort)
        return out

    def accept_skill_completion(self) -> None:
        """Take the highlighted name. Trailing space, because a skill can take
        arguments and the next keystroke should be one."""
        ac = getattr(self, "_skill_ac", None)
        name = ac.current() if ac else None
        if not name:
            return
        box = self.query_one("#message-input", Input)
        box.value = f"/{name} "
        box.cursor_position = len(box.value)
        ac.dismiss_list()

    @on(OptionList.OptionSelected, "#skill-ac-list")
    def _skill_ac_selected(self, event) -> None:
        """Click or Enter inside the list. The click has already moved the
        highlight, so this is the same act Tab performs."""
        ac = getattr(self, "_skill_ac", None)
        if ac is not None and event.option_index is not None:
            ac.options.highlighted = event.option_index
        self.accept_skill_completion()
        self.query_one("#message-input", Input).focus()

    @on(Input.Submitted, "#message-input")
    def handle_submit(self, event: Input.Submitted) -> None:
        value = event.value
        event.input.value = ""
        self._submit_text(value, alt_chord=False)

    def action_submit_alt(self) -> None:
        """ctrl+shift+enter — the OTHER end of the mid-turn mapping."""
        inp = self.query_one("#message-input", Input)
        value = inp.value
        inp.value = ""
        self._submit_text(value, alt_chord=True)

    def _submit_text(self, value: str, alt_chord: bool) -> None:
        text = value.strip()
        if not text and not self.pending_image:
            return

        if text.startswith("/"):
            self._handle_command(text)
            return

        # Check if input names an image file (optionally followed by a prompt)
        image_b64 = self.pending_image
        if text and not image_b64:
            path, rest = self._split_image_path(text)
            if path is not None:
                image_b64 = self._load_image_file(path)
                if image_b64:
                    text = rest  # keep the question; do not discard it
                    self.notify(f"Image loaded: {path.name}", timeout=2)
                else:
                    self._system(f"Could not read image: {path}")
                    return
            elif self._looks_like_image_path(text):
                # Loud, because the silent version is what sent a bare path to
                # the model as TEXT: it cannot see the file, says so, and the
                # user reads that as the model being unable to view images.
                self._system(
                    f"That looks like an image path but I could not open it:\n  {text}\n"
                    "Check the path exists. Quotes are fine; a question after the path is fine."
                )
                return

        has_image = image_b64 is not None
        # This is the moment a conversation earns its directory. Everything
        # before it — boot, the system prompt, a /model switch, an abandoned
        # /resume — leaves nothing on disk.
        self._materialise_convo()

        # Build API message content
        if image_b64 and text:
            content: str | list = [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                },
                {"type": "text", "text": text},
            ]
        elif image_b64:
            content = [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                },
                {"type": "text", "text": "Describe this image."},
            ]
        else:
            content = text

        self.pending_image = None
        profile = self.settings.tool_policy_profile
        if self._chat_running():
            act = midturn_action(self.settings.enter_interrupts, alt_chord)
            if act == "queue":
                bubble = self._user_bubble(text, has_image, queued=True)
                self._pending_input.append(
                    {"content": content, "text": text, "bubble": bubble,
                     "tool_profile": profile}
                )
                self.notify("Queued — sends when this turn ends", timeout=3)
                return
            # Interrupt: soft stop — the existing stop path keeps the partial
            # reply and discards unanswered tool calls, which is what protects
            # the tool_call_id pairing. The message goes to the FRONT so the
            # flush sends it before anything queued behind it.
            self._user_bubble(text, has_image)
            self._pending_input.insert(
                0, {"content": content, "text": text, "tool_profile": profile}
            )
            self._stop_requested = True
            self.notify("Interrupting — your message sends next", timeout=3)
            return
        self._user_bubble(text, has_image)
        self._append({"role": "user", "content": content})
        self._active_tool_profile = profile
        self._stream()

    async def _ensure_chat_ready(self, *, timeout: float | None = None) -> None:
        """Ask, in plain words, whether model_id can serve a turn RIGHT NOW.

        D2/D11: the router answers `400 model is not loaded` for four different
        situations — unloaded but in the preset, absent from the preset, no
        model named at all, and still loading — and app.py rendered whichever
        one it got verbatim into the message. The backend seam tells them
        apart, and WAITS OUT a load already in flight rather than telling
        someone to /load a model that is loading.

        🔴 A PRE-FLIGHT QUERY, NOT A 400 HANDLER. It runs BEFORE create() and
        inspects no status code anywhere, which is why it cannot swallow the
        unload-400 that D3 depends on surfacing (llm_backend `_apply_sync`,
        guarded by test_the_unload_400_is_still_not_swallowed). Do not
        "simplify" this into an except clause around create() — that is the
        exact shape that guard was planted to catch.

        `timeout` bounds the wait for callers that are not a user's turn. See
        COMPACT_READY_TIMEOUT_S. Cancelling the wait does NOT kill the seam's
        worker thread — Python cannot — so it keeps polling to its own ceiling
        and its result is discarded. Harmless: the seam is read-only by law
        (a question about state must not change it) and starts no load.

        A backend without the method carries on. app.py takes whatever
        `make_backend` returns, and plugins and test doubles predate the seam;
        a missing capability must mean "no opinion", never AttributeError
        mid-turn.
        """
        ask = getattr(self.backend, "ensure_chat_ready", None)
        if ask is None:
            return
        if not self.model_id:
            # 🔴 NOTHING TO ASK ABOUT, AND A WORKING PATH TO PROTECT. With no
            # selection both _stream and _compact send the sentinel
            # `self.model_id or "local-model"` and let the server resolve it —
            # which is how a single-model LM Studio serves a user who never
            # picked anything. The seam answers "no model is selected" here,
            # correctly for a caller with no fallback and wrongly for this one:
            # obeying it would refuse turns that work today.
            #
            # This cost 12 existing tests to notice. Asking about a model the
            # request is not going to name is a question about the wrong thing.
            return

        async def _probe() -> None:
            try:
                await ask(self.model_id)
            except llm_backend.BackendError:
                # A DEFINITIVE no, in words written for a human. Refuse.
                raise
            except Exception:
                # 🔴 THE PROBE FAILED, NOT THE MODEL — carry on and let the
                # real request produce its own error.
                #
                # A DIAGNOSTIC MUST NOT BECOME A GATE. This is additive: it
                # exists to turn ONE confusing 400 into plain words. If it
                # cannot answer — server unreachable, a listing shape a newer
                # build changed, any bug in the probe itself — then we have no
                # information, and blocking a turn on no information converts a
                # working setup into a broken one and hides the real error
                # behind a new failure mode of our own making.
                #
                # Caught in the wild by 12 existing tests (compaction UI, wake
                # after compact) that stub the CLIENT and leave a real backend
                # with nothing listening: every one of them stopped compacting.
                # They were right, and this is why the blanket catch is correct
                # rather than convenient.
                return

        if timeout is None:
            await _probe()
            return
        try:
            await asyncio.wait_for(_probe(), timeout)
        except asyncio.TimeoutError:
            # Only reachable from wait_for: _probe swallows everything except
            # a BackendError, so a TimeoutError here is OUR bound expiring and
            # never a socket timeout inside the probe.
            raise llm_backend.BackendError(
                f"{self.model_id!r} is still loading — skipped rather than "
                f"holding the app for it. It will be retried."
            ) from None

    @work(exclusive=True, group="chat")
    async def _stream(self) -> None:
        """Agent loop: stream a turn; if the model called tools, execute them,
        feed results back, and stream again until a plain answer arrives."""
        self._stop_requested = False
        # A turn is starting, so nothing is abandoned any more. Cleared HERE
        # rather than where the ping reads it: a mark that only ever latched
        # would kill loop mode for the rest of the session after one Esc.
        self._turn_abandoned = False
        # 🔴 RE-READ THE WINDOW AT TURN START, NOT ONLY AT TURN END.
        #
        # The end-of-turn resync was wired into ONE of the loop's exits (the
        # plain-answer branch). A turn that ends by being stopped, or by hitting
        # the tool-iteration cap, skipped it — so a window read while the model
        # was unloaded stayed at the model's CEILING for the rest of the
        # session. The footer then shows "262,144 max" while the model is
        # serving 120,064, and _maybe_autocompact refuses to divide by a
        # ceiling, which means AUTO-COMPACT NEVER FIRES.
        #
        # Turn start has no such branches: every turn passes through here. It is
        # also the right moment — LM Studio JIT-loads on the previous turn's
        # first request, so by now the real window exists to be read.
        self._resync_ctx_if_stale()
        compact_due = False
        for _iteration in range(self.settings.tool_iterations):
            if self._stop_requested:
                break
            if _iteration and self._autocompact_due() is not None:
                # THE BUDGET IS SPENT INSIDE A TURN, SO IT MUST BE CHECKED
                # INSIDE ONE. Both _maybe_autocompact calls below sit at
                # `return` statements, so a turn that keeps calling tools
                # never reaches either and the window sails past the
                # threshold — watched live going 80 -> 84 -> 87%. With
                # tool_iterations at 100 a single turn can eat the lot.
                #
                # We only TEST here. Starting the compaction from inside
                # this worker would cancel this worker: _compact is
                # exclusive in the same "chat" group as _stream. So break
                # and schedule it for after we exit; wake_after_compact
                # then resumes the task.
                #
                # `_iteration and` skips iteration 0 deliberately: nothing
                # has been spent yet on this turn, and a turn STARTED by
                # the post-compact wake must be allowed to do real work
                # before it may compact again, or the two ping-pong.
                compact_due = True
                break
            widget = self._assistant_bubble()
            self._elapsed_start(widget.body)
            thinking: ThinkingBlock | None = None
            text_full = ""
            reasoning = ""
            tool_acc: dict[int, dict] = {}
            tool_msgs: dict[int, ToolMessage] = {}

            request_messages = self._request_messages()
            # PROMPT ASSEMBLY IS THE EVENT. This is where the conversation and
            # the live store are folded into the thing the model actually
            # reads, and it is the last moment before the turn is committed.
            self._glassbox(
                "context", 1.0, f"{len(request_messages)} messages", discrete=True
            )
            kwargs = TurnEngine.chat_request(
                model_id=self.model_id,
                messages=request_messages,
                tools_enabled=self.tools_enabled,
                max_tokens_tools=self.settings.max_tokens_tools,
                max_tokens_chat=self.settings.max_tokens_chat,
                request_overrides=self.backend.request_overrides(self.model_id),
                thinking_level=self.thinking_level,
                tools=self._all_tools() if self.tools_enabled else None,
            )

            self._tps_start()
            try:
                # ASK BEFORE OPENING THE STREAM. Inside this try on purpose:
                # the plain words then land in the same widget that used to
                # show the raw 400, with no second error path to keep in step.
                # No timeout here — the user is watching and asked for this
                # turn, so a model that is loading is worth waiting out.
                await self._ensure_chat_ready()
                stream = await self.client.chat.completions.create(**kwargs)
            except Exception as e:
                self._elapsed_stop_body()
                self._thinking_done()
                widget.body.content = Text(f"Error: {e}", style="bold red")
                widget.border_title = "Error"
                self._scroll_down()
                return

            try:
                async for chunk in stream:
                    u = getattr(chunk, "usage", None)
                    if u is not None and getattr(u, "total_tokens", None):
                        self.ctx_used = int(u.total_tokens)
                        self._tps_final(int(getattr(u, "completion_tokens", 0) or 0))
                        # ETA: this is the end of the turn -- the usage chunk
                        # carries prompt_tokens, so fold this turn into the
                        # learned rate (gated) and remember its count as the
                        # estimate for the next turn's ETA.
                        self._eta_learn(getattr(u, "prompt_tokens", None))
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    self._eta_record_first_delta()
                    # LM Studio streams the thinking trace as `reasoning_content`
                    # (some other OpenAI-compatible servers use `reasoning`).
                    token = getattr(delta, "reasoning_content", None) or getattr(
                        delta, "reasoning", None
                    )
                    if token:
                        self._tps_tick()
                        self._glassbox_rate("thinking")
                        reasoning += token
                        if thinking is None and self.settings.show_thinking:
                            thinking = ThinkingBlock()
                            self._thinking_live = thinking
                            widget.mount(thinking, before=widget.body)
                            # Discrete event -> unconditional. See _scroll_down.
                            # AFTER the refresh, not during it: mount() has not
                            # been measured yet, so scrolling in this frame targets
                            # the PRE-mount extent and parks the viewport just above
                            # the block that appeared. The policy in _scroll_down was
                            # already right; it was being asked at the wrong moment.
                            # Identical reasoning to ThinkingBlock.append's own
                            # call_after_refresh: "the scroll extent does not grow
                            # until the new content has been re-measured".
                            self.call_after_refresh(self._scroll_down)
                        if self.thinking_level == "off":
                            self._warn_reasoning_ignored()
                        # None when show_thinking is off. The trace still arrives and is
                        # still echoed back to the model — this hides the VIEW, it does
                        # not make the request cheaper.
                        if thinking is not None:
                            thinking.append(token)
                        # The stream had NO autoscroll AT ALL: neither this
                        # branch nor the answer branch below called it, so the
                        # log only moved at the final render -- which is why it
                        # read as "only scrolls when the message comes through".
                        self._scroll_down(only_if_following=True)
                    if delta.content:
                        self._tps_tick()
                        self._glassbox_rate("output")
                        self._thinking_done()
                        self._elapsed_stop_body()
                        text_full += delta.content
                        widget.body.content = Text(text_full + " \u258c")
                        self._scroll_down(only_if_following=True)
                    if self._stop_requested:
                        # Checked AFTER this chunk is rendered, not before: the
                        # chunk is already in hand, and the dialog promises that
                        # whatever it has written is kept. Breaking here runs the
                        # SAME finalisation path as a natural finish, so the
                        # partial text is recorded to the conversation.
                        try:
                            await stream.close()
                        except Exception:
                            pass
                        break
                    tool_calls = getattr(delta, "tool_calls", None) or []
                    if tool_calls:
                        # A tool call is the trace's end too (the model
                        # thinks, then decides), so the header timer stops here.
                        self._thinking_done()
                    for tc in tool_calls:
                        idx, named_now, argued_now = TurnEngine.accumulate_tool_call(
                            tool_acc, tc
                        )
                        if named_now:
                            if idx not in tool_msgs:
                                msg = ToolMessage(tool_acc[idx]["name"])
                                tool_msgs[idx] = msg
                                # Mount FIRST. _tool_begin attaches the cancel
                                # control beside this widget, which it cannot
                                # do while the widget has no parent. The old
                                # order skipped it silently.
                                self.query_one("#chat-log").mount(msg)
                                self._tool_begin(msg)
                            # Every name fragment, not only the first: the
                            # original scrolled here at the `if fn.name` level.
                            self._scroll_down()
                        if argued_now and idx in tool_msgs:
                            tool_msgs[idx].set_args(tool_acc[idx]["arguments"])
                            self._scroll_down()
            except Exception as e:
                self._elapsed_stop_body()
                self._thinking_done()
                widget.body.content = Text(f"Error: {e}", style="bold red")
                widget.border_title = "Error"
                self._scroll_down()
                return

            self._elapsed_stop_body()
            # Final render of this turn's bubble. _thinking_done
            # finalizes the trace (idempotent if a content or tool token
            # already ended it) and stops the header timer with the stream.
            self._thinking_done()
            if text_full:
                try:
                    widget.body.set_markdown(text_full)
                except Exception:
                    widget.body.content = Text(text_full)
            else:
                # Pure tool turn (or empty): don't leave a "..." bubble behind.
                if thinking is None:
                    widget.remove()
                else:
                    widget.body.styles.display = "none"
            self._scroll_down()

            # Record the assistant turn (content and/or tool_calls) so the
            # model keeps full context across the loop.
            if text_full or tool_acc:
                message: dict = {"role": "assistant", "content": text_full or None}
                if reasoning:
                    # Echo the trace back so the model keeps reasoning context.
                    message["reasoning_content"] = reasoning
                if tool_acc:
                    message["tool_calls"] = [
                        {
                            "id": slot.get("id") or f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": slot["name"],
                                "arguments": slot["arguments"] or "{}",
                            },
                        }
                        for i, slot in sorted(tool_acc.items())
                    ]
                self._append(message)

            if self._stop_requested:
                self._system(
                    '[stopped by you — partial reply kept'
                    + (', pending tool calls discarded]' if tool_acc else ']')
                )
                # This exit is one of the ways a compaction gets STARTED (the
                # scheduled _maybe_autocompact below). Record that the turn was
                # KILLED rather than finished, so the post-compaction wake ping
                # does not tell the model to resume what the user just stopped.
                self._turn_abandoned = True
                self.call_after_refresh(self._maybe_autocompact)
                return

            if not tool_acc:
                # Turn is over. Check the window AFTER this worker exits:
                # _compact shares group="chat" and would cancel us mid-frame.
                await self.plugins.finalize_turn()
                self.call_after_refresh(self._resync_ctx_if_stale)
                self.call_after_refresh(self._maybe_autocompact)
                return  # plain answer — agent loop done

            # Execute each tool call, display the result, feed it back.
            for i, slot in sorted(tool_acc.items()):
                name = slot["name"]
                args_json = slot["arguments"] or "{}"
                tc_id = slot.get("id") or f"call_{i}"
                msg = tool_msgs.get(i)
                try:
                    args = json.loads(args_json) if args_json else {}
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                except Exception as e:
                    result, ok = f"[error] invalid tool arguments: {e}", False
                else:
                    result, ok = await self._execute_tool(name, args)
                # ── one hygiene point for every tool result ──────────
                # bash, read, web_fetch, harness, skill and every MCP tool
                # pass through here and nowhere else, so this is where they
                # get cleaned — not per tool: a new tool would forget it, and
                # per-tool is where the bypass would hide. Strip BEFORE the
                # display AND before the model: the same string serves both,
                # and the escape bytes are what shred the terminal and burn
                # context on noise the model cannot use. Then re-assert the
                # terminal modes a child may have changed (sanitize.py: why).
                result = sanitize.strip_escapes(result)
                sanitize.reset_terminal_modes()
                if msg is not None:
                    msg.set_result(result, ok)
                    self._tool_end(msg)
                self._scroll_down()
                self._append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": name,
                        # What the MODEL sees from here on; the bubble above
                        # showed the raw. Verbatim unless tool_context_mode
                        # says otherwise (settings).
                        "content": await self._contextualise_tool_result(
                            name, result
                        ),
                    }
                )

            # 🔴 THE ONLY DOOR AN IMAGE CAN COME THROUGH.
            #
            # A tool result is a role:"tool" message whose content is a STRING.
            # No amount of base64 in that string makes the model see a picture;
            # it would describe nothing, confidently, having spent a megabyte of
            # context to do it. Images are visible ONLY as an image_url block on
            # a role:"user" message -- exactly what the paste path builds.
            #
            # So view_image stages, and this drains: after the round's tool
            # results are appended (order matters -- every tool_call_id must be
            # answered before a non-tool turn appears) and before the next
            # request goes out.
            # Queued input joins the conversation HERE, at the round
            # boundary, so the model sees it on its very next request instead
            # of after the whole loop unwinds. Same slot and same reason as the
            # staged images below.
            self._deliver_queued_input()

            if self._pending_tool_images:
                staged = self._pending_tool_images
                self._pending_tool_images = []
                content: list = []
                for _path, b64 in staged:
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    })
                names = ", ".join(Path(p).name for p, _ in staged)
                content.append({
                    "type": "text",
                    "text": (
                        f"[view_image] {names} — attached for you to look at now."
                    ),
                })
                self._append({"role": "user", "content": content})
                self._user_bubble(f"[view_image] {names}", True)

        if compact_due:
            pct = self._autocompact_due()
            self._system(
                f"[pausing at {pct}% of the window — compacting between tool"
                " iterations, then resuming]"
                if pct is not None
                else "[pausing to compact between tool iterations]"
            )
            self.call_after_refresh(self._maybe_autocompact)
            return

        self._system(
            f"[stopped \u2014 reached {self.settings.tool_iterations} tool iterations in one turn — raise it in /settings]"
        )

    # ── Compaction ───────────────────────────────────────────────

    @staticmethod
    def _msg_chars(msgs: list[dict]) -> int:
        total = 0
        for m in msgs:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict) and p.get("text")
                )
            total += len(c or "")
        return total

    @staticmethod
    def _safe_tail(msgs: list[dict], want: int) -> list[dict]:
        """Trim a tail forward until it starts on a user message.

        A tail that begins with a `tool` message (or an assistant message that
        made tool calls) references a tool_call_id whose assistant message we
        are about to drop. LM Studio rejects that, and the failure arrives on
        the NEXT turn, long after /compact reported success.
        """
        tail = msgs[-want:] if want else []
        while tail and tail[0].get("role") != "user":
            tail = tail[1:]
        return tail

    def _deliver_queued_input(self) -> bool:
        """Hand queued messages to the model at a ROUND boundary, mid-turn.

        Ryan: "if i queue a message the agent has to fully stop to get it ... it
        wont deliver between tool calls or agent thinking / output."

        Exactly so, and the reason was structural: the ONLY flush point was
        on_worker_state_changed, which fires when a chat-group worker ENDS --
        and _stream is a single worker that runs the entire agent loop. With
        tool_iterations at 100, a queued message could wait out a hundred rounds
        while the user watched the agent work on without it. The message was not
        lost, it was just unreachable until the turn died.

        Called from the same slot the staged-image drain uses, and for the same
        invariant, which that code already spells out: every tool_call_id must
        be answered before a non-tool turn appears. Delivering here is safe;
        delivering mid-round would break the pairing.

        Interrupts do not come through here. That path sets _stop_requested and
        returns out of the worker before this point, so it still ends the turn
        and sends next -- queue and interrupt stay two different verbs.
        """
        if not self._pending_input or self._stop_requested:
            return False
        # ONE per boundary, FIFO -- never the whole queue. Consecutive role:user
        # turns are a chat-template gamble and qwen's template 500s on some
        # shapes, which is exactly why _flush_pending_input has always sent one
        # per turn end (tests/test_message_queue.py names that case). Draining
        # the lot here would have rebuilt that hazard at a new site, against the
        # model this app is usually pointed at. The rest ride the next round,
        # and rounds are plentiful.
        item = self._pending_input.pop(0)
        self._active_tool_profile = item.get(
            "tool_profile",
            getattr(getattr(self, "settings", None), "tool_policy_profile",
                    tool_policy.INTERACTIVE),
        )
        self._append({"role": "user", "content": item["content"]})
        _mark_delivered(item)
        return True

    def _flush_pending_input(self) -> None:
        """Send the oldest held message once the chat group is idle.

        Scheduled from on_worker_state_changed, so EVERY way a chat-group
        worker ends — plain answer, stop, compact break, iteration cap,
        cancellation, error — reaches this one flush point, and no future exit
        path can forget it. If the group is busy again (a compaction, a new
        turn), retry shortly rather than racing it: _stream and _compact are
        exclusive in that group, and a flush that streamed now would CANCEL
        whichever is running.
        """
        if not self._pending_input:
            return
        if self._chat_running():
            self.set_timer(0.7, self._flush_pending_input)
            return
        item = self._pending_input.pop(0)
        self._active_tool_profile = item.get(
            "tool_profile",
            getattr(getattr(self, "settings", None), "tool_policy_profile",
                    tool_policy.INTERACTIVE),
        )
        self._materialise_convo()
        self._append({"role": "user", "content": item["content"]})
        _mark_delivered(item)
        self._stream()

    def _load_skills(self):
        """The skill index, from cache when there is one.

        Discovery walks every configured library on every boot, which is why a
        folder added while the app was running never appeared: the answer was
        computed once at startup and nothing could ask again. The cache does not
        change that -- it makes the recomputation an explicit, cheap act
        (/skills refresh) instead of a restart.

        Returns (skills, generated_at). generated_at is 0.0 for a fresh scan,
        which is how /skills knows whether it is showing cached data and how old
        it is: a cache nobody can date is a cache nobody can distrust.
        """
        if not self.settings.skills_enabled:
            return [], 0.0
        cached = skills_mod.read_cache(paths.ROOT)
        if cached is not None:
            return cached
        found = skills_mod.discover_all(paths.ROOT, self.settings.skill_roots)
        skills_mod.write_cache(paths.ROOT, found)
        return found, 0.0

    def refresh_skills(self) -> tuple[list[str], list[str]]:
        """Re-scan every library and rewrite the cache. Returns (added, removed).

        The delta is the point: "refreshed" tells you nothing, while "+1
        find-claude-skills" tells you the thing you just wrote was picked up.
        """
        before = {s.name for s in self.skills}
        found = skills_mod.discover_all(paths.ROOT, self.settings.skill_roots)
        skills_mod.write_cache(paths.ROOT, found)
        self.skills = found
        self.skills_cached_at = 0.0
        after = {s.name for s in found}
        return sorted(after - before), sorted(before - after)

    def get_css_variables(self) -> dict:
        """Every theme resolves $thinking-text / $thinking-box / $tool-text.

        These are theme VARIABLES rather than Textual Theme fields, and the
        themes we do not author carry none of them: textual-dark, dracula,
        nord, catppuccin-mocha and friends stay registered, and textual-dark is
        this app's hard fallback whenever a saved theme name will not resolve.

        An undefined CSS variable in Textual does not quietly skip one
        declaration -- it fails the stylesheet. So without this floor, adding a
        themeable colour would have broken the app outright for anyone sitting
        on a built-in theme, which is the single most likely place to be.

        Derived from whatever the ACTIVE theme does provide, so a built-in gets
        a thinking frame in its own warning colour rather than ours.
        """
        variables = super().get_css_variables()
        try:
            defaults = themes_mod.extra_defaults(
                primary=variables.get("primary") or "#a89a80",
                bone=variables.get("foreground") or "#c8c8ce",
            )
        except Exception:
            return variables
        for key, value in defaults.items():
            if not variables.get(key):
                variables[key] = value
        return variables

    def _register_custom_themes(self) -> None:
        """Register every custom theme from settings. Idempotent — an existing
        name is overwritten, which is what makes the creator an EDITOR too.
        A corrupt entry is skipped with a note rather than killing the boot:
        settings.json is hand-editable and a typo there must cost one theme,
        not the app."""
        for name, tokens in (self.settings.custom_themes or {}).items():
            try:
                self.register_theme(themes_mod.theme_from_tokens(name, tokens))
            except Exception as e:
                self.notify(f"custom theme {name!r} skipped: {e}", severity="warning")

    def watch_theme(self, theme_name: str) -> None:
        """Persist a theme change, wherever it came from (palette, settings).

        Without this the palette's "Change theme" lasted exactly one session
        — the pick worked, nothing recorded it, and boot reset to the default.
        Guarded: at construction settings may not exist yet, and a no-op
        write (same name) is skipped so booting never rewrites the file.
        """
        st = getattr(self, "settings", None)
        if st is None or st.theme_name == theme_name:
            return
        st.theme_name = theme_name
        try:
            settings_mod.save(st)
        except OSError:
            pass  # a theme that lasts one session beats a crash on switch

    def on_worker_state_changed(self, event) -> None:
        """The single flush point for held input — fires on every chat-group
        worker ending, whatever the reason. One site instead of a call at each
        of _stream's exits, because exits multiply and each new one would have
        to remember the flush."""
        if getattr(event.worker, "group", None) != "chat":
            return
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self.call_after_refresh(self._flush_pending_input)

    def _wake_after_compact(self) -> None:
        """The post-compaction ping: one user message that says "resume the
        in-flight task, or say standing by", then a normal turn.

        Runs as a plain callback, SCHEDULED by _compact - never called from
        inside it. _stream is exclusive work in the same "chat" group as
        _compact, and a direct call from inside the compact worker would
        cancel the compaction that is still unwinding. The guard covers the
        window between scheduling and firing, in which a real user turn may
        have claimed the chat group first: that turn wins, and the ping is
        dropped rather than queued behind it.
        """
        if self._chat_running():
            return
        if self._pending_input:
            # A REAL user message is waiting — it is a better wake than the
            # synthetic ping, and the flush is about to deliver it.
            return
        if self._turn_abandoned:
            # The last turn was KILLED by the user, not finished. "Resume the
            # in-flight task" is the wrong thing to say about a task they
            # deliberately stopped, and stopping is itself one of the ways a
            # compaction gets scheduled (see _stream's stop branch). ABANDONED
            # only — a turn that merely ENDED still wakes, which is the whole
            # feature. Cleared at the next turn's start, never here.
            return
        self._materialise_convo()
        self._user_bubble(WAKE_AFTER_COMPACT, False)
        self._append({"role": "user", "content": WAKE_AFTER_COMPACT})
        self._stream()

    @work(exclusive=True, group="chat")
    async def _compact(self, extra: str = "") -> None:
        # Read-and-clear FIRST: the early returns below must also consume
        # the flag, or an aborted autocompact marks the next MANUAL one auto.
        auto = getattr(self, "_compact_is_auto", False)
        self._compact_is_auto = False
        system = (
            self.conversation[0]
            if self.conversation and self.conversation[0].get("role") == "system"
            else None
        )
        body = self.conversation[1:] if system else list(self.conversation)
        if len(body) < 3:
            self._system("Nothing to compact yet — have a conversation first.")
            return

        tail = self._safe_tail(body, self.settings.compact_keep_recent)
        head = body[: len(body) - len(tail)] if tail else body
        if not head:
            self._system("Nothing to compact — everything is already recent.")
            return

        before_chars = self._msg_chars(self.conversation)
        before_count = len(self.conversation)
        card = CompactionCard(
            plan=(f"{len(head)} messages \u2192 summary \u00b7 keeping the "
                  f"last {len(tail)} verbatim \u00b7 {before_chars:,} chars before"),
            prompt_text=COMPACT_PROMPT + (f"\n\n{extra}" if extra else ""),
            auto=auto,
        )
        # AWAITED: mount() only schedules composition. The first stream
        # chunk can arrive before the card's children exist, and mounting a
        # thinking block `before=self.body` then dies with MountError —
        # body has no parent yet. Awaiting makes the card whole first.
        await self.query_one("#chat-log").mount(card)
        self._compact_card = card
        # The shared elapsed loop retires after ~1s idle, and a compaction can
        # begin with nothing else in flight -- without this the clock never ticks.
        if self._elapsed_task is None or self._elapsed_task.done():
            try:
                self._elapsed_task = asyncio.create_task(self._elapsed_repaint())
            except RuntimeError:
                self._elapsed_task = None
        self._scroll_down()

        ask = list(head) + [
            {"role": "user", "content": COMPACT_PROMPT + (f"\n\n{extra}" if extra else "")}
        ]
        if system:
            # Merge the live store in, so it can see what memory.md already
            # holds and update rather than duplicate.
            block = self._store_block(live=True)
            ask.insert(0, {**system, "content": system.get("content", "") + block})

        # Tools are passed so STEP 1 of COMPACT_PROMPT can actually happen.
        # Without them the instruction to persist is theatre: the model narrates
        # writing files and nothing reaches disk.
        summary = ""
        writes: list[str] = []
        try:
            # ONCE, before the first round — the answer cannot change midway
            # through a compaction, and asking per round would multiply the
            # bound below by compact_max_tool_iters. Bounded because this is
            # maintenance, not a turn: see COMPACT_READY_TIMEOUT_S. Said on
            # the card first, so a wait that does happen is explained rather
            # than looking like a hang — the card exists to stop compaction
            # being a spinner and a prayer.
            card.set_status(f"checking {self.model_id} is ready")
            await self._ensure_chat_ready(timeout=COMPACT_READY_TIMEOUT_S)
            for round_no in range(1, self.settings.compact_max_tool_iters + 1):
                kwargs = TurnEngine.compact_request(
                    model_id=self.model_id,
                    messages=ask,
                    max_tokens=self.settings.compact_max_tokens,
                    thinking_level=self.settings.compact_thinking_level,
                    tools_enabled=self.tools_enabled,
                    tools=self._all_tools() if self.tools_enabled else None,
                )

                stream = await self.client.chat.completions.create(**kwargs)
                text_full = ""
                tool_acc: dict = {}
                tool_msgs: dict = {}
                # Twin of the delta assembly in _stream, deliberately: the
                # compaction card speaks the same grammar as a normal turn
                # because it reuses the same chunk shapes and widgets.
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    token = getattr(delta, "reasoning_content", None) or getattr(
                        delta, "reasoning", None
                    )
                    if token:
                        card.think(token)
                        self._scroll_down(only_if_following=True)
                    if delta.content:
                        card.thinking_done()
                        text_full += delta.content
                        card.body.content = Text(text_full + " \u258c")
                        card.set_status(
                            f"round {round_no} \u00b7 summary "
                            f"{len(text_full):,} chars"
                        )
                        self._scroll_down(only_if_following=True)
                    for tc in (getattr(delta, "tool_calls", None) or []):
                        card.thinking_done()
                        idx, named_now, argued_now = TurnEngine.accumulate_tool_call(
                            tool_acc, tc
                        )
                        if named_now and idx not in tool_msgs:
                            msg = ToolMessage(tool_acc[idx]["name"])
                            tool_msgs[idx] = msg
                            card.add_tool(msg)
                            # Only on creation here, unlike _stream: the card
                            # scrolls when a tool APPEARS, not per fragment.
                            self._scroll_down()
                        if argued_now and idx in tool_msgs:
                            tool_msgs[idx].set_args(tool_acc[idx]["arguments"])

                card.thinking_done()
                if not tool_acc:
                    summary = text_full.strip()
                    break

                # The model called tools (persisting durable state before the
                # history is destroyed). Record its turn, run them VISIBLY,
                # feed the results back, go round again.
                ask.append({
                    "role": "assistant",
                    "content": text_full or None,
                    "tool_calls": [
                        {
                            "id": slot["id"] or f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": slot["name"],
                                "arguments": slot["arguments"],
                            },
                        }
                        for i, slot in sorted(tool_acc.items())
                    ],
                })
                card.set_status(
                    f"round {round_no} \u00b7 running {len(tool_acc)} tool call(s)"
                )
                for i, slot in sorted(tool_acc.items()):
                    fname = slot["name"]
                    ok = True
                    try:
                        fargs = json.loads(slot["arguments"] or "{}")
                        if not isinstance(fargs, dict):
                            raise ValueError("arguments must be a JSON object")
                        result, ok = await self._execute_tool(fname, fargs)
                        if ok and fname == "write":
                            writes.append(str(fargs.get("path", "?")))
                    except Exception as e:
                        result = f"[error] {type(e).__name__}: {e}"
                        ok = False
                    if i in tool_msgs:
                        tool_msgs[i].set_result(str(result), ok)
                    ask.append({
                        "role": "tool",
                        "tool_call_id": slot["id"] or f"call_{i}",
                        "name": fname,
                        "content": str(result),
                    })
                self._scroll_down(only_if_following=True)
        except Exception as e:
            self._autocompact_failed_at = self.ctx_used
            card.fail(f"failed \u2014 conversation unchanged \u00b7 {type(e).__name__}: {e}")
            self._system(f"Compact failed — conversation unchanged.\n{type(e).__name__}: {e}")
            return

        if not summary:
            card.fail("failed \u2014 no summary produced \u00b7 conversation unchanged")
            self._system(
                "Compact failed — no summary produced "
                f"(gave up after {self.settings.compact_max_tool_iters} tool rounds). "
                "The usual cause is the reasoning trace consuming the whole token budget\n"
                "before any summary is written. Raise Compact max tokens, or lower Compact\n"
                "thinking level, in /settings.\n"
                "Conversation unchanged."
                + (f"\nStore writes that DID land: {', '.join(writes)}" if writes else "")
            )
            self._autocompact_failed_at = self.ctx_used
            return

        pair = [
            {
                "role": "user",
                "content": "[Summary of earlier conversation, which has been compacted away]\n\n"
                + summary,
            },
            {"role": "assistant", "content": "Understood — I have that context."},
        ]
        rebuilt: list[dict] = ([system] if system else []) + pair + tail

        # Record BEFORE swapping self.conversation: keep_from indexes the list
        # as it stands on disk, which is the pre-compaction one.
        self._truncate(len(self.conversation) - len(tail), pair, "compact")
        self.conversation = rebuilt
        # Succeeded: forget any earlier failure so a later window that
        # happens to land on the same token count is not blocked by it.
        self._autocompact_failed_at = None
        after_chars = self._msg_chars(self.conversation)
        pct = (100 - after_chars * 100 // before_chars) if before_chars else 0
        try:
            card.body.set_markdown(summary)
        except Exception:
            card.body.content = Text(summary)
        # Signed properly: a toy conversation can GROW under compaction
        # (summary + pair outweigh a tiny history), and "\u2212-189%" is the
        # formatter lying twice at once.
        delta = f"\u2212{pct}%" if pct >= 0 else f"+{-pct}%"
        # THE LEDGER CHANNEL carries the real numbers, not a placeholder \u2014 the
        # same before/after the card shows, so the brain and the card can never
        # disagree about what a compaction did.
        self._glassbox(
            "ledger", 1.0,
            f"{before_count} \u2192 {len(self.conversation)} messages ({delta})",
            discrete=True,
        )
        card.finish(
            f"done \u00b7 {before_count} \u2192 {len(self.conversation)} messages "
            f"\u00b7 {before_chars:,} \u2192 {after_chars:,} chars ({delta})"
            + (f" \u00b7 persisted: {', '.join(Path(w).name for w in dict.fromkeys(writes))}"
               if writes else "")
        )
        if writes:
            store_note = "\n  persisted: " + ", ".join(
                Path(w).name for w in dict.fromkeys(writes)
            )
        elif self.tools_enabled:
            store_note = "\n  persisted: nothing (model judged nothing durable)"
        else:
            store_note = "\n  persisted: nothing — TOOLS ARE OFF, so it could not write"
        self._system(
            f"Compacted: {before_count} messages → {len(self.conversation)} "
            f"({before_chars:,} → {after_chars:,} chars, −{pct}%). "
            f"Kept the last {len(tail)}."
            + ("" if self.settings.clear_screen_after_compact
               else " Scrollback above is untouched — it shows messages the model"
                    " can no longer see.")
            + store_note
        )
        if self.settings.clear_screen_after_compact:
            # Everything above this point was just replaced by the summary.
            # Leaving it rendered means the screen and the context disagree.
            summary_note = (
                f"Compacted {before_count} messages into a summary "
                f"({before_chars:,} chars, -{pct}%). Screen cleared so what you can "
                f"scroll back to matches what the model can actually see. "
                f"The full transcript is still on disk in this conversation's file."
            )
            self._clear_screen(note=summary_note)

        if self.settings.wake_after_compact:
            # SCHEDULED, never called: see _wake_after_compact for why a
            # direct self._stream() here would cancel this very compact.
            # Success path only - a failed compact produced no summary, and
            # waking on "nothing changed" is a ping with no answer.
            self.call_after_refresh(self._wake_after_compact)

    # ── Commands ─────────────────────────────────────────────────

    def _mcp_server_names(self) -> list[str]:
        """Server names from mcp.json, for the per-server toggles.

        Returns [] rather than raising when MCP is absent or unreadable: a
        settings screen that cannot open because an optional config file is
        malformed is worse than one that shows no MCP section.
        """
        try:
            mgr = getattr(self, "mcp", None)
            if mgr is not None and getattr(mgr, "servers", None):
                return sorted(mgr.servers.keys())
            import json as _json
            from pathlib import Path as _Path
            cfg = _Path(__file__).resolve().parent.parent.parent / "mcp.json"
            if cfg.exists():
                data = _json.loads(cfg.read_text(encoding="utf-8"))
                servers = data.get("mcpServers") or data.get("servers") or {}
                if isinstance(servers, dict):
                    return sorted(servers.keys())
        except Exception:
            pass
        return []

    def _on_settings_saved(self, new: Settings | None) -> None:
        """Persist and apply. None = the user cancelled, so change nothing."""
        if new is None:
            return
        old = self.settings
        self.settings = new
        # New/edited custom themes must exist in the registry BEFORE the
        # theme_name below tries to apply one of them.
        self._register_custom_themes()
        if new.theme_name != self.theme:
            try:
                self.theme = new.theme_name
            except Exception:
                self._system(f"Theme {new.theme_name!r} not found — keeping {self.theme}")
        try:
            path = settings_mod.save(new)
        except OSError as e:
            self._system(f"Settings applied for this session but NOT saved: {e}")
            path = None

        # Loading weights is EXPLICIT and only on an actual change. This is the
        # only caller: it used to run on every connect, so every app boot (and
        # every test that constructed the app) loaded a model, several at once
        # under a parallel suite. A context length you did not just ask for must
        # never move resident weights.
        if (
            new.default_context_length
            and new.default_context_length != old.default_context_length
        ):
            # force=True: the user just typed this number. It applies even when
            # the model already serves MORE than that — lowering the window to
            # free VRAM is a legitimate instruction, and a "≥ is fine" guard
            # would silently ignore it.
            self._apply_context_length(force=True)

        # Apply the ones with immediate effect.
        self.tools_enabled = new.tools_enabled
        if new.thinking_level != old.thinking_level:
            self.thinking_level = new.thinking_level
            self._reasoning_ignored_warned = False  # re-arm: it is per-config
        self._update_header()
        self._refresh_ctx_label()

        changed = [
            f.name for f in fields_of(Settings)
            if getattr(old, f.name) != getattr(new, f.name)
        ]
        if not changed:
            self._system("Settings unchanged.")
            return

        note = f"Settings saved ({len(changed)} changed): {', '.join(changed[:8])}"
        if len(changed) > 8:
            note += f", +{len(changed) - 8} more"
        if path:
            note += f"\n  {path.name}"
        # Say plainly which ones do NOT take effect now, rather than letting a
        # user conclude the control is broken when nothing appears to happen.
        deferred = [c for c in changed if c in ("lm_host", "default_model",
                                                "default_context_length",
                                                "mcp_enabled", "mcp_disabled_servers",
                                                "skills_enabled")]
        if deferred:
            note += ("\n  Applies on next /reconnect: " + ", ".join(deferred))
        self._system(note)

    @staticmethod
    def _mark_message(data: dict) -> str:
        """The text half of a mark turn. Pure, so the shape is testable."""
        return (
            f"[mark] I marked the screen at ({data['x']},{data['y']}) — "
            f"monitor {data['mon']}, monitor-local ({data['mon_x']},{data['mon_y']}). "
            "The screenshot attached is that monitor, and the ring drawn on it is "
            "my marker: respond to what I am pointing at."
        )

    def _start_mark(self) -> None:
        """/mark — the human screen-marker channel.

        Spawns the interactive marker overlay (draggable ring + send/cancel),
        then polls for its handoff file. The overlay writes the JSON and a PNG
        of the marked monitor WITH THE RING STILL IN THE SHOT — the ring is
        the highlight; that is the whole feature.
        """
        if not MARK_SCRIPT.exists():
            self._system(f"/mark: overlay script missing at {MARK_SCRIPT}")
            return
        handoff = Path(tempfile.mkdtemp(prefix="litetui_mark_")) / "mark.json"
        try:
            # No -Label: a spaced label dies through some launch paths, and
            # the ring is self-explanatory. Keep the HANDLE — a timeout must
            # take the unanswered ring down, not leave it as screen litter.
            proc = ttyguard.popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(MARK_SCRIPT),
                 "-Interactive", "-HandoffFile", str(handoff),
                 "-Color", "cyan"],
                stdin=subprocess.DEVNULL,
            )
        except OSError as e:
            self._system(f"/mark: could not launch the overlay: {e}")
            return
        self._system(
            "Marker up — drag the ring onto the thing, then click send. "
            "(x or Esc cancels; times out in 3 minutes.)"
        )
        self._mark_wait(handoff, proc)

    @work(exclusive=True, group="mark")
    async def _mark_wait(self, handoff: Path, proc) -> None:
        """Poll for the overlay's handoff. Group "mark", NOT "chat" — waiting
        for a human to drag a ring must never cancel a running turn, and a
        turn must never cancel the wait."""
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            if handoff.exists():
                break
        else:
            try:
                proc.terminate()   # take the unanswered ring down
            except OSError:
                pass
            self._system("/mark: timed out — marker dismissed.")
            return
        try:
            # utf-8-sig: Windows PowerShell's Set-Content -Encoding UTF8
            # writes a BOM, and json.loads chokes on it — caught in the live
            # drill, not by inspection.
            data = json.loads(handoff.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            self._system(f"/mark: unreadable handoff: {e}")
            return
        if data.get("cancelled"):
            self._system("/mark: cancelled.")
            return
        b64 = self._load_image_file(Path(data["png"]))
        if not b64:
            self._system(f"/mark: could not read the screenshot at {data.get('png')}")
            return
        text = self._mark_message(data)
        content: list = [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": text},
        ]
        # Same held-vs-idle contract as inbox mail: mid-turn it queues
        # visibly and flushes as a real turn; idle it sends now.
        if self._chat_running():
            self._user_bubble(text, True, queued=True)
            self._pending_input.append({"content": content, "text": text})
            return
        self._materialise_convo()
        self._user_bubble(text, True)
        self._append({"role": "user", "content": content})
        self._stream()

    def _handle_command(self, cmd: str) -> None:
        parts = cmd.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        # Every command lives in the registry; plugins own the handlers.
        entry = self.plugins.commands.get(name)
        if entry is not None:
            entry.handler(self, name, arg)
            return

        # A slash name that is not a command may be a SKILL. Ryan typed
        # /ls-mark expecting exactly that and got "Unknown" printed beside a
        # list containing ls-mark -- and an autocomplete that completes to a
        # dead command would be a control that does nothing, which is this
        # repo's most-repeated defect.
        #
        # EXACT matches only. A mistyped command must still report itself
        # rather than quietly loading something that merely resembles it.
        bare = name.lstrip("/").lower()
        if any(s.name.lower() == bare for s in self.skills):
            # ARG RIDES ALONG. This rebuilt the command WITHOUT it, so
            # `/ls-youtube-transcript <url>` loaded the skill and threw the url
            # away — Ryan: "i couldnt send him the link and invoke it at once".
            # The autocomplete completes to "/name " with a trailing space,
            # which invites exactly the argument this line was discarding.
            self._handle_command(f"/skills {bare} {arg}".rstrip())
            return
        self._system(f"Unknown: {name} — try /help")

    def action_clear_chat(self) -> None:
        self._handle_command("/clear")


def main():
    LiteTUI().run()


if __name__ == "__main__":
    main()
