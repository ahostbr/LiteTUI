"""LiteTUI — a terminal chat client and agent harness for a local LM Studio model."""

import asyncio
import base64
import io
import json
import os
import re
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from litetui import harness as harness_mod
from dataclasses import dataclass, fields as fields_of
from functools import partial

from litetui import settings as settings_mod
from litetui.settings import Settings
from litetui import side_panel
from litetui.side_panel import present_dialog, show_dialog

from litetui import llm_backend
from litetui import model_residency, model_transport
from litetui import paths
from litetui import tasks as tasks_mod
from litetui import prompt_compiler
from litetui import runtime_log
from litetui.conversation import (
    CONVO_SEED_FILES,
    TRANSCRIPT_NAME,
    ConversationRepository,
)
from litetui import ttyguard
from litetui import mcp_client
from litetui import sanitize
from litetui.fmt import fmt_dur

# 🔴 RE-EXPORT, NOT JUST AN IMPORT. These nine helpers and one constant were
# defined here until T070 step O0 moved them to `textfmt`. They are imported
# back because callers reach them THROUGH THIS MODULE — tests do
# `from litetui.app import render_progress, tool_display_parts` and
# `app_mod.tps_text`, with 24 outside references to `render_progress` alone.
# Dropping the names here would have been a rename dressed as a refactor.
# ⚠️ So this move reduces app.py's LINE COUNT and nothing else: the API surface
# is unchanged and every name is still reachable at `litetui.app.<name>`.
from litetui.textfmt import (  # noqa: F401  (re-exported for existing callers)
    TOOL_NAME_DEFAULT,
    _markdown_to_text,
    is_reliable_rate_sample,
    load_prompt,
    memory_prompt,
    midturn_action,
    render_progress,
    thinking_header_text,
    tool_denied,
    tool_display_parts,
    profile_text,
    tps_text,
)
# 🔴 RE-EXPORT, same reason as `textfmt` above. Every widget below is used
# inside this file AND reached from tests as `from litetui.app import ToolMessage`
# / `app_mod.CompactionCard` — 16 outside references to ToolMessage alone, 13 to
# ThinkingBlock. The re-export is what makes O0 a move instead of a break.
# ⚠️ It buys a shorter app.py and nothing else: same API surface, same method
# count on LiteTUI, same plugin reach-through.
from litetui.widgets import (  # noqa: F401  (re-exported for existing callers)
    AnswerBody,
    AssistantMessage,
    CancelToolButton,
    ChatMessage,
    Completion,
    CompactionCard,
    ConfirmStop,
    ConfirmStopBody,
    ContextFooter,
    FoldBlock,
    LiteTUICommands,
    PromptInput,
    SkillAutocomplete,
    ThinkingBlock,
    ThinkingHeader,
    ToolMessage,
    _at_bottom,
    _FoldHeader,
    _mark_delivered,
)
# O2: nine stateless helpers now live in `appsvc`. They take `app` as their
# first parameter because they still READ app state — the coupling is visible
# in the signature instead of hidden behind `self`. LiteTUI loses nine methods;
# plugin reach-through and the shared-state knot are unchanged.
from litetui import appsvc
from litetui import scheduler as sched_mod
from litetui import tool_context
from litetui import tool_policy
from litetui.turn_engine import TurnEngine
from litetui import thinking_probe
from litetui import themes as themes_mod
from litetui.colorpicker import ColorPickerScreen  # noqa: F401 — CSS binds by class name
from litetui import tool_approval
from litetui.tool_approval import ToolApprovalBody, ToolApprovalScreen
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
from litetui import cron as cron_mod
from litetui import turnstats


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_DIM = 1536



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



# ════════════════════════════════════════════════════════════════
# Agent tools (pi-style): bash, read, write, web_fetch
# Limits mirror pi's defaults (2000 lines / 50KB).
# ════════════════════════════════════════════════════════════════

# Fallback only. The live value is self.settings.tool_iterations, editable in
# /settings; env LM_TOOL_ITERS still wins over both (see settings.ENV_OVERRIDES).
# This constant remains so module-level users and tests keep a sane number.
TOOL_MAX_ITERATIONS = int(os.environ.get("LM_TOOL_ITERS", "48"))






























# The launch wordmark. Module-level so a test can assert on it without a running
# app, and so the art is not buried inside a method body.
LITETUI_SPLASH = (
    "\n"
    "  \u2588\u2588     \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588  \u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588\n"
    "  \u2588\u2588       \u2588\u2588     \u2588\u2588   \u2588\u2588       \u2588\u2588   \u2588\u2588  \u2588\u2588   \u2588\u2588  \n"
    "  \u2588\u2588       \u2588\u2588     \u2588\u2588   \u2588\u2588\u2588\u2588\u2588    \u2588\u2588   \u2588\u2588  \u2588\u2588   \u2588\u2588  \n"
    "  \u2588\u2588       \u2588\u2588     \u2588\u2588   \u2588\u2588       \u2588\u2588   \u2588\u2588  \u2588\u2588   \u2588\u2588  \n"
    "  \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588   \u2588\u2588   \u2588\u2588\u2588\u2588\u2588\u2588   \u2588\u2588   \u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588\n"
    "      local-only coding agent \u00b7 LM Studio + llama.cpp \u00b7 /help\n"
)


#: The tool card's colours when no theme is reachable -- the values that were
#: hardcoded in this function before `tool-text` became a theme token. Kept as
#: the default so the pure function stays callable with no app and no theme.
























#: What every tool returns while the tools toggle is OFF, and the note that
#: replaces the tools prompt section. One string each, in one place, because
#: the model reads both and they must not drift apart.
#:
#: Both name the REAL controls, verified rather than invented: Ctrl+T is what
#: the app's own toggle message advertises (`action_toggle_tools`), and the
#: switch is `settings_screen.py:308`, labelled "Tools enabled" on the
#: "Agent loop" tab. A refusal that sends the user somewhere that does not
#: exist is worse than no refusal at all.
#:
#: 🔴 THE REFUSAL HALF NOW LIVES IN `prompts/tool-denied.md`, section
#: `## tools-off`, reached through `textfmt.tool_denied("tools-off")` — one
#: file holding every refusal LiteTUI hands the model. The constant that used
#: to sit here is still the FLOOR (`textfmt.TOOL_DENIED_FALLBACK`), used when
#: the file is missing or an edit breaks the section, so a refusal survives
#: its own source being broken.
#:
#: ⚠️ TOOLS_DISABLED_PROMPT below DID NOT MOVE, deliberately: it is a SYSTEM
#: PROMPT section, not a refusal, and it is composed at turn start rather than
#: returned in place of a tool result. Migrating it is a separate question and
#: has not been asked — the drift warning above is why it is flagged here
#: rather than moved quietly.
TOOLS_DISABLED_PROMPT = (
    "\nTOOLS ARE ADVERTISED BUT DISABLED. The schemas are offered so "
    "that a tool call is a real call rather than text you type out; every one "
    "of them will be refused without running. If you need a tool, call it "
    "normally, read the refusal, and tell the user that tools are off in "
    "LiteTUI and only they can enable them (Ctrl+T, or Settings -> Agent loop "
    "-> Tools enabled). Never write tool-call syntax into your reply as "
    "prose." "\n"
)


# ── T137: plain-words error surface ────────────────────────────────
# The user-facing line names WHAT seems wrong and WHAT to do, in one or two
# sentences. No URLs, no WinError codes, no exception reprs — the raw detail
# goes beside it into runtime_log.record_error (runtime-errors.log), never
# into chat. These helpers are the ONLY place that decides what a backend
# failure says out loud; every site in this file routes through them.


def _connection_family(exc: BaseException) -> bool:
    """True when the exception chain is a dead or unreachable server, not a
    protocol or content error.

    Two passes, deliberately ordered:
      1. DEFINITIVE — walk __cause__/__context__ (and URLError.reason, which
         holds the OS error as an attribute rather than a cause) looking for
         ConnectionError/TimeoutError subclasses. This is what catches the
         BackendError-wrapped-URLError case, because llm_backend raises with
         `from e`. A SUPPRESSED context (`raise ... from None`) is never
         walked: it is hidden on purpose — our own wait_for bound expiring
         behind a 'still loading' refusal must not read as a dead server.
      2. TEXTUAL — openai.APIConnectionError may carry no OS-level cause at
         all (its message can be a bare "Connection error."), so sniff the
         text of NON-BackendError exceptions only: a cleaned backend message
         that merely mentions "timeout" must never be misread as a dead
         server.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, (ConnectionError, TimeoutError)):
            return True
        reason = getattr(cur, "reason", None)   # URLError wraps the OS error here
        if isinstance(reason, BaseException):
            stack.append(reason)
        # Walk the DECLARED cause always; the implicit context only when
        # it was not suppressed. `raise ... from None` hides its context on
        # purpose (e.g. wait_for's TimeoutError behind our own bound), and a
        # hidden context is an implementation detail, not this failure's why.
        if cur.__cause__ is not None:
            stack.append(cur.__cause__)
        elif not cur.__suppress_context__ and cur.__context__ is not None:
            stack.append(cur.__context__)
    if not isinstance(exc, llm_backend.BackendError):
        text = str(exc).lower()
        needles = ("winerror 10061", "refused", "timed out", "timeout",
                   "connection error")
        return any(needle in text for needle in needles)
    return False


def _plain_backend_error(e: BaseException, backend_name: str | None = None) -> str:
    """The words the user reads when a backend call fails.

    Order matters and is deliberate:
      1. connection family FIRST — llm_backend wraps URLError into a
         BackendError `from e`, so checking BackendError first would hide the
         "server seems closed" copy behind the wrapper's message;
      2. BackendError — its message was cleaned at source (T137);
      3. anything else gets one plain sentence, never a repr.
    """
    if _connection_family(e):
        if backend_name == "lmstudio":
            return "LM Studio Seems Closed. Switch Backends Or Start LM Studio."
        if backend_name == "llamacpp":
            return ("The llama.cpp server seems closed — start it, or switch "
                    "backends (/backend).")
        return "The model server seems closed — start it, or check /backend."
    if isinstance(e, llm_backend.BackendError):
        return str(e)
    return "Something went wrong talking to the model server."


def key_label(key: str) -> str:
    """A Textual key name as a human reads it: "ctrl+p" -> "Ctrl+P"."""
    if not key:
        return ""
    return "+".join(
        part.capitalize() if len(part) > 1 else part.upper()
        for part in key.split("+")
    )


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

    /* The palette's only door (T573). WITHOUT `dock` THIS IS A ZERO-WIDTH
       WIDGET: Footer gives an undocked child no space, so the button
       mounted, rendered, reported visible=True display=True, and measured
       Size(0, 0) -- its on_click worked when called directly and could
       never be reached by an actual click. Docked, it sizes to content. */
    .palette-button {
        dock: right;
        width: auto;
        padding: 0 2 0 1;
        background: $footer-background;
        text-style: bold;
    }

    /* Every modal centres in the window. SettingsScreen was missing from this
       list and rendered docked to the TOP-LEFT — the rule existed, the new
       screen simply was not in it. */
    ColorPickerScreen {
        align: center middle;
    }

    /* `max-width` binds only in the sidebar strip; the modal is unchanged at a
       flat 54. See #picker-box for the mid-word clip this prevents.

       `overflow-y: auto` for the reason #job-box already carries it: the
       column is title 1 + field 12 + hue 1 + row 3 + the swap control 4 = 21
       rows of content, and a 24-row terminal offers 20. MEASURED, both hosts:
       "SwapButton(id=dialog-swap) rows 22..24 outside Vertical content rows
       2..21". Without this the control is the child that falls off the bottom
       -- the swap button clipping out of existence is precisely the defect
       T222 was written for, arriving by a different route. Scrolling keeps it
       REACHABLE rather than trimming the field to make arithmetic work. */
    #cp-box {
        width: 54;
        max-width: 100%;
        max-height: 100%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
        overflow-y: auto;
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

    /* `max-width: 100%` — the sidebar strip caps at 60 columns and this box is
       78, so without it the rows are CLIPPED MID-WORD ("LiteTUI ea"). The flat
       78 stays the basis so the modal is unchanged; the cap only binds when the
       host is narrower. Same fix ToolApprovalBody already carried — this box
       was converted EARLIER and never swept. */
    #picker-box {
        width: 78;
        max-width: 100%;
        max-height: 100%;
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
        /* 19, not 20: the box is height:auto capped at max-height:100%, so when
           the list is long NOTHING shrinks and the LAST child is clipped -- the
           swap button's bottom border fell one row outside the content box.
           title 2 + list 20 + hint 4 + button 3 = 29 into 28 rows of content.
           This cap is the only slack in the column, so it yields the one row.
           ⚠️ It does NOT make the picker safe on a SHORT terminal: at 24 rows
           main ALREADY clips the list and the hint with no button present.
           That is a separate, pre-existing defect -- measured, not inferred. */
        max-height: 19;
        background: $surface;
        border: none;
    }

    #picker-hint, #confirm-sub {
        color: $text-muted;
        padding-top: 1;
    }

    /* ── Settings ─────────────────────────────────────────── */

    /* `max-width` binds only in the 60-column sidebar strip; the modal keeps a
       flat 92. `height: 88%` STAYS HERE: this box is shared by the model panel
       and /settings, and both bodies are `height: 100%`, i.e. transparent to
       the percentage. See #help-box for the case where it has to move. */
    #set-box {
        width: 92;
        max-width: 100%;
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
       is its own short surface, so nothing pushes another off the bottom.

       `#mc-tabs` is the model panel. It copied /settings' layout — same
       #set-box, same TabPane + .set-scroll shape — and NOT this line, so its
       TabbedContent took its natural height and overflowed the box at every
       terminal height. Measured on the pre-conversion tree (02c8fe1, clean),
       modal path, walking children against their parent's content region:
           h=24  TabbedContent#mc-tabs rows 5..21 vs Vertical content 2..18
           h=32  rows 5..28 vs 2..25
           h=50  rows 5..44 vs 2..41
       Pre-existing and shipped; surfaced because the swap control was then the
       child that fell off the bottom. */
    #set-tabs, #mc-tabs {
        height: 1fr;
    }

    .set-scroll {
        height: 1fr;
        scrollbar-size: 1 1;
        padding-right: 1;
    }

    #set-tabs Tabs, #mc-tabs Tabs {
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

    /* 🔴 padding-left MATCHES `.set-help` AND `.set-label-inline`, WHICH IS THE
       WHOLE POINT. A row is Label / Input / help-Static stacked in one
       Vertical; the help text carries `padding-left: 1`, an Input's own border
       puts its text at column 1, and a switch row's label is `.set-label-inline`
       which is also 1. The bold label was the ONLY member at column 0, so every
       heading in Settings hung one cell to the left of the control and the
       sentence describing it — visible as a ragged left edge down the whole
       panel once you see it. Ryan, 2026-09-03, from a screenshot: "thats a bug,
       how the settings are offset". */
    .set-label {
        color: $text;
        text-style: bold;
        padding-left: 1;
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
    /* The 96%/92% moved UP to CalendarBody; this box is 100% of that, so the
       modal is the size it always was. See #help-box for the re-basing rule. */
    #cal-box {
        width: 100%;
        height: 100%;
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

    /* `max-height` stays HERE at 80%: DayBody is `height: 100%`, so this
       resolves against the same number it always did and there is one centring
       step, not two. `max-width` binds only in the 60-column sidebar strip. */
    #day-box {
        width: 82;
        max-width: 100%;
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

    /* `max-height` stays HERE at 90%; see #day-box. `overflow-y: auto` below
       is unchanged and now also carries the swap control into reach. */
    #job-box {
        width: 82;
        max-width: 100%;
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

    /* The 80% moved UP to HelpBody, which is now what stands between this box
       and the screen; a percentage resolved against an auto-height parent that
       is itself sized BY this box has no fixed point (see PickerBody). 100% of
       a body that is 80% of the screen is the same height the modal always had.
       `max-width` binds only in the 60-column sidebar strip, where a flat 88
       clips mid-word. */
    #help-box {
        width: 88;
        max-width: 100%;
        height: 100%;
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
        max-width: 100%;   /* clips in a narrow panel without this — see #picker-box */
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
        # shift+tab cycles the authority level, the way Claude Code's own
        # shift+tab cycles its permission modes -- Ryan gave that as the spec
        # with screenshots ("see same way claude works").
        #
        # 🔴 priority=True IS REQUIRED HERE AND I TRIED IT WITHOUT FIRST.
        # The plan (and the brief) was to declare this WITHOUT priority so a
        # focused dialog's own shift+tab would win by proximity. Measured: it
        # then never fires AT ALL. Textual's own `Screen` already binds
        # shift+tab to `focus_previous`, and a SCREEN binding beats an APP
        # binding — so the polite version is dead everywhere, not just in
        # dialogs, and every test would still have passed.
        #
        # priority=True fires everywhere, which on its own would walk focus
        # out of a pending tool approval (see SidePanel's focus trap). So the
        # precedence is decided EXPLICITLY in side_panel.handle_reverse_tab
        # rather than inherited from a resolution order that cannot express
        # "app, except in dialogs". test_a_dialog_keeps_its_own_shift_tab is
        # the gate.
        #
        # MEASURED, not assumed: Textual 8.0.2's XTermParser turns the legacy
        # back-tab sequence ESC [ Z into the key name "shift+tab", so this is
        # reachable without the kitty protocol -- unlike ctrl+shift+enter
        # above, which has no legacy encoding and needed the ctrl+j alias.
        Binding("shift+tab", "cycle_tool_profile", "Authority",
                priority=True, show=False),
        Binding("ctrl+p", "toggle_plan_mode", "Plan", priority=True, show=False),
    ]

    pending_image: reactive[str | None] = reactive(None)
    ctx_used: reactive[int | None] = reactive(None)
    # Generation speed of the most recent turn. Survives the turn so the footer
    # keeps showing the last measurement while idle rather than blanking.
    tps: reactive[float | None] = reactive(None)

    def __init__(
        self,
        *,
        rpc: bool = False,
        first_prompt: str | None = None,
        system_prompt: str | None = None,
        initial_model: str | None = None,
        tool_profile: str | None = None,
        plan_mode: bool = False,
        convo_id: str | None = None,
        **app_kwargs,
    ):
        super().__init__(**app_kwargs)
        self._rpc = rpc
        self._first_prompt = first_prompt
        self._cli_system_prompt = system_prompt
        self._cli_initial_model = initial_model
        self._cli_tool_profile = tool_profile
        # T558 plan mode. SESSION-ONLY, deliberately not persisted to settings:
        # a mode that survives a restart is a mode you forget you are in, and
        # this one silently refuses to build. Authority persists because it is a
        # standing permission; this is a posture for the work in front of you.
        self._plan_mode = bool(plan_mode)
        # T570 — which footer chip the keyboard is on, by ID and never by index.
        #
        # An INDEX would be wrong the moment the list changes under it, which it
        # does on its own: `bg` and `agents` appear and vanish as work starts and
        # finishes. Index 2 quietly becomes a different chip and the next Enter
        # opens something the user was not pointing at. An id that is no longer
        # in the list simply drops the selection, which is visible.
        self._footer_nav: str | None = None
        self._cli_convo_id = convo_id
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
        # T540: the levels this model actually supports (from probe or seed).
        # None = not yet probed; a list = the effective set.
        self._model_thinking_levels: list[str] | None = None
        # Images staged by view_image during a tool round, drained into a
        # role:"user" turn once the round's tool results are appended.
        self._pending_tool_images: list[tuple[str, str]] = []
        # "vlm" | "llm" | None, learned from the same request as the ctx window.
        self.model_type: str | None = None
        # tok/s accounting, reset per turn by TpsState.start. `tps` itself
        # stays a reactive on the class -- assigning it IS the repaint.
        self._tps = turnstats.TpsState()
        # The STORE owns where this conversation lives and how it is written.
        # The properties below keep `self.convo_id` and friends resolving, so
        # nothing that reads them had to change.
        self.store = ConversationRepository(on_error=self._report_persist_error)
        # Staged-but-not-created. See _new_convo / _materialise_convo.
        self.last_usage: dict | None = None
        self._stop_requested = False  # Esc-to-stop, checked inside the stream loop
        #: WHY _stop_requested was set, as the line to show the user, or None
        #: for the plain Esc case. It exists because the agent loop's tail used
        #: to report "reached N tool iterations" for EVERY early break — so
        #: stopping the turn any other way (Esc during tool execution, and now
        #: denying a tool) explained itself with a cap that was never reached
        #: and sent the user to /settings to raise it.
        self._stop_reason: str | None = None
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
        self._elapsed = turnstats.ElapsedState(self)
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
        # Background tool tasks (T499). Rows still running at boot come back LOST.
        self.bg_tasks: dict = tasks_mod.load(paths.data_root())
        #: Host authority for the turn currently consuming tools. Human turns
        #: start from settings; cron/inbox turns explicitly replace it with a
        #: narrower profile. The model never writes this field.
        self._active_tool_profile = self.settings.tool_policy_profile
        # T507-T1: CLI overrides, applied in on_mount after _connect.
        if self._cli_tool_profile:
            from litetui import tool_policy
            profile_map = {"autonomous": tool_policy.AUTONOMOUS, "interactive": tool_policy.INTERACTIVE, "scheduled": tool_policy.SCHEDULED}
            if self._cli_tool_profile in profile_map:
                self._active_tool_profile = profile_map[self._cli_tool_profile]
        #: Cron jobs, loaded once at construction. A scheduled prompt is an
        #: INPUT nobody typed, so it rides the same held/flushed path as inbox
        #: mail rather than growing a second delivery route.
        self._cron = cron_mod.CronService(self)
        # ETA state and its three fields moved to turnstats.EtaState (O4-a);
        # the docstring that explained them moved with them.
        self._eta = turnstats.EtaState()
        # Last emit time per glass-box channel, for the continuous ones only.
        self._gb_last: dict[str, float] = {}
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
        self.skills, self.skills_cached_at = appsvc.load_skills(self, )
        # 🔴 READ THE CONFIGS HERE; DIAL OUT LATER. A CONSTRUCTOR MUST NOT BLOCK
        # ON A SOCKET.
        #
        # This used to call `self.mcp.load()`, which connects every declared
        # server inline. `.mcp.json` declares an HTTP server on localhost:7423
        # (LiteSuite's bridge), and when LiteSuite is not running the POST waits
        # to be REFUSED. Measured 2026-09-03, three direct timings of that exact
        # request: 4.033s / 4.051s / 4.105s, "[WinError 10061] the target
        # machine actively refused it" — the Windows dual-stack localhost path
        # (::1 then 127.0.0.1, with retries), not a timeout anyone configured.
        #
        # So EVERY LiteTUI launch with LiteSuite down paid ~4 seconds before its
        # first frame, and every test that built an app paid it too: constructing
        # one cost 4316.7 ms against 47.5 ms for the whole Textual `run_test()`
        # boot and teardown. The compositor was never the cost.
        #
        # `reload_configs()` is the file read only — no network — so /settings,
        # `describe()` and the /mcp dialog still list what is DECLARED from the
        # first frame. Only the CONNECT moves, into `_mcp_connect` after mount.
        self.mcp = mcp_client.MCPManager(paths.ROOT)
        if self.settings.mcp_enabled:
            self.mcp.reload_configs()
        self._mcp_dispatch = self.mcp.dispatch()
        # A seat in the fleet, like any other agent. Registration is
        # deferred to the first poll tick so the roster shows the real
        # model rather than the empty string it holds before _connect.
        self.seat = harness_mod.Seat(
            agent_id=harness_mod.process_agent_id(),
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
        # T558. Gated on the mode, so LEAVING plan mode drops the instruction —
        # the composition is rebuilt through the same single builder the tools
        # toggle uses, and an absent section leaves nothing behind.
        self.plugins.add_prompt_section(
            "host", _ord["PLAN"],
            lambda: "\n" + paths.PLAN_PROMPT_FILE.read_text(encoding="utf-8").strip() + "\n",
            enabled=lambda: self._plan_mode and paths.PLAN_PROMPT_FILE.exists(),
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
            lambda: (
                "\n" + paths.TOOLS_PROMPT_FILE.read_text(encoding="utf-8").strip() + "\n"
                if self.tools_enabled
                else TOOLS_DISABLED_PROMPT
            ),
            # Gated on BOTH the toggle and the file. A missing file must not
            # render as an empty section: the tools would still be OFFERED to
            # the model with no instructions on how to use them, which is a
            # worse state than not offering them, and a silent one. The boot
            # check below is what makes the absence audible.
            #
            # 🔴 T073: the schemas are advertised even when the toggle is OFF,
            # so offering them with no instructions became reachable through
            # the TOGGLE and not only through a missing file. The render above
            # answers it — full instructions when on, a short note when off —
            # so the OFF state ships neither all of tools.md nor silence. The
            # file gate still applies only to the ON branch, which needs it.
            enabled=lambda: (
                paths.TOOLS_PROMPT_FILE.exists() if self.tools_enabled else True
            ),
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
        # The per-tool denylist, as a LIVE READ. A lambda and not a snapshot so
        # unticking a box in /tools takes effect on the next request instead of
        # the next restart. `or ()` because the field can be null in a
        # hand-edited settings.json, and a None here would crash tool_specs on
        # every turn rather than failing to hide one tool.
        self.plugins.tools_disabled = lambda: frozenset(
            self.settings.tools_disabled or ()
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
        _checks = [
            ("system prompt", paths.SYSTEM_PROMPT_FILE),
            ("tools prompt", paths.TOOLS_PROMPT_FILE),
        ]
        # Only worth saying when the mode is on: an absent file nobody is using
        # is not a fault, and a startup warning for it trains people to ignore
        # the two above, which always matter.
        if self._plan_mode:
            _checks.append(("plan prompt", paths.PLAN_PROMPT_FILE))
        for label, f in _checks:
            if not f.exists():
                self._system(f"[!] {label} missing: {f}\n    That section is absent from the model's context.")
        self._connect()
        # Plugin activate() hooks — the side-effecting half of the lifecycle,
        # run where the monitors it will absorb have always started.
        plugins_mod.activate_plugins(self, self.plugins, self._plugin_manifests)
        # MCP servers connect AFTER the first frame. See __init__ for why.
        if self.settings.mcp_enabled and self.mcp.configs:
            self._mcp_connect()
        # T507-T1: apply CLI args after connection is up.
        if self._cli_initial_model or self._first_prompt or self._cli_system_prompt:
            self._apply_cli_args()
        # T507-T2: start the RPC bridge in headless mode.
        if self._rpc:
            from litetui import rpc as rpc_mod
            rpc_mod.start_rpc_reader(self)
            self._rpc_emit_ready()

    @work(exclusive=True, group="mcp")
    async def _mcp_connect(self) -> None:
        """Connect the declared MCP servers, off the startup path.

        🔴 EVERY CONNECT RUNS IN A THREAD. `MCPServer.start` does a blocking
        HTTP POST or spawns a process; awaiting it on the event loop would move
        the four-second stall from "before the first frame" to "the UI is up and
        frozen", which is worse, not better — the same wait with a screen that
        now looks alive.

        📌 A REFUSED SERVER IS ANNOUNCED, NOT SWALLOWED. `connect()` returns the
        error text rather than raising (that is its documented contract), so
        nothing upstream would ever have seen it. A tool that simply never
        appears is indistinguishable from one the model chose not to call, which
        is the failure mode this whole surface exists to remove.
        """
        disabled = set(self.settings.mcp_disabled_servers or ())
        failed: list[str] = []
        connected: list[str] = []
        for name, sc in list(self.mcp.configs.items()):
            # The denylist is applied by NOT DIALLING, where it used to connect
            # and then stop. mcp.json stays the single source of what EXISTS —
            # `configs` still holds these, so /settings can re-enable them — but
            # a server the user switched off no longer costs a round trip to
            # find that out.
            if name in disabled or not isinstance(sc, dict) or sc.get("disabled"):
                continue
            err = await asyncio.to_thread(self.mcp.connect, name)
            (failed if err else connected).append(f"{name}: {err}" if err else name)

        if connected:
            # 🔴 THE ONE STALE THING AFTER A CONNECT. Specs are re-read per turn;
            # the DISPATCH map is cached. Without this the model is offered tools
            # the loop cannot route. See rebuild_mcp_dispatch.
            self.rebuild_mcp_dispatch()
            self._update_header()
            # `system_message`, not the `_system` alias: the alias is bound at
            # class level (`_system = system_message`), so a caller who replaces
            # the PUBLIC method on an instance — which is how a test watches
            # what the app says — never sees a line posted through the private
            # name. Found exactly that way; the announce arm read an empty list
            # while the line was being printed.
            self.system_message(f"mcp: {', '.join(connected)}")
        for line in failed:
            self.system_message(f"[!] mcp {line}")

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
            # derived tool count. (No longer the ONLY late tool — MCP moved off
            # the constructor in T239 and repaints the same header when its
            # servers land. Both arrive after the first frame, independently.)
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
            runtime_log.record(
                "harness_registration_failed",
                site="app.inbox_monitor",
                component="harness",
                operation="register",
                status="failed",
            )
            # The REASON, not just the fact. This event fires once, at startup,
            # and is the only durable trace of a seat that never joined the
            # fleet. It used to ride in the metadata log as `error=` — but the
            # sanitizer rejects any string with spaces, so that call was ALWAYS
            # silently dropped; record_error is where raw text actually lands.
            runtime_log.record_error(
                "harness_registration_failed",
                detail=self.seat.error or "unknown",
            )
            self._system("harness seat OFFLINE — the agent fleet is unreachable.")
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
        # 🔴 READS THE SETTING. It used to be a hardcoded SCHEDULED, so the
        # option labelled "every capability, UNATTENDED, never asks" did not
        # govern the unattended path that woke this turn -- Ryan hit it as a
        # workspace_write denial while Settings showed autonomous. A control
        # that names a case it does not govern is worse than no control.
        #
        # NOT a bare read: `unattended()` degrades a CONFIRM-CAPABLE profile
        # to the read-only floor, because nobody is here to answer a modal.
        # The default is `autonomous` (Ryan: "b default to auto") and needs no
        # degrading -- its confirm set is empty. This still fires for a user
        # who explicitly chose `interactive` and then received mail. See
        # tool_policy.unattended.
        profile = tool_policy.unattended(self.settings.tool_policy_profile)
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
                sched_mod.save(self.jobs, paths.ROOT)
            except OSError:
                pass
            self._system(blocked)
            return
        job.last_fired_slot = sched_mod.slot_of(now)
        job.run_count += 1
        try:
            sched_mod.save(self.jobs, paths.ROOT)
        except OSError:
            pass  # an unwritable store must not stop the job from running

        label = job.label or job.id
        text = job.prompt
        # 🔴 A SCHEDULED TURN IS ALWAYS AUTONOMOUS. HARDCODED ON PURPOSE.
        # T085, Ryan: "just change it so schedule only runs auto mode ... light
        # warning when setting that it must run auto for this reason".
        #
        # THE REASON IS HERE because a hardcoded profile, on a path that used
        # to read a setting, otherwise reads as a mistake: a scheduled task
        # fires when nobody is at the keyboard. A level that stops to ASK has
        # nobody to ask, so it would not run -- it would sit on a modal until
        # someone came back. Choosing a level here is choosing between "runs"
        # and "hangs", which is not a choice worth offering.
        #
        # ⚠️ THIS LINE HAS HELD THREE VALUES IN ONE EVENING: the job's own
        # field, then `settings.tool_policy_profile` (Ryan's first ruling),
        # now this. None was wrong when written. The conversation setting
        # deliberately does NOT reach here any more -- changing how autonomous
        # the CHAT is must not silently change what every saved automation may
        # do.
        #
        # 📌 `unattended()` is deliberately NOT applied: autonomous has an
        # EMPTY confirm set, so the degrade would be a no-op. Inbox mail is NOT
        # a job -- it still reads the setting through `unattended()`.
        profile = tool_policy.AUTONOMOUS
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
        # T640: the fold is a side call and now has its own model. A cold local
        # pick degrades to the main model rather than loading one, and says so
        # ONCE — per turn, not per fold, or a misconfiguration would narrate
        # itself into every tool result the turn produces.
        fold_model, fold_note = model_residency.resolve_side_call_model(
            self, getattr(self.settings, "tool_summary_model", None)
        )
        if fold_note and fold_note != getattr(self, "_last_fold_note", None):
            self._last_fold_note = fold_note
            self._system(f"[llm-tool-summ] {fold_note}")
        try:
            resp = await model_transport.for_app(self).create(
                model=fold_model,
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
        """The one host authorization door before any tool side effect."""
        self._rpc_emit({"type": "tool_call", "name": name, "args": args})
        # THE TOGGLE IS ENFORCED HERE, BEFORE RESOLUTION, so a disabled tool
        # cannot run even if another branch is added above the policy gate.
        # ok=False because nothing executed — callers use that flag to record
        # writes, and no write happened.
        # 🔴 DELIBERATELY NOT EXECUTABLE, AND THE TEXT FORM IS DELIBERATELY NOT
        # PARSED: detect-and-execute would defeat the one guarantee this
        # setting exists to make.
        if not self.tools_enabled:
            return tool_denied("tools-off"), False
        # 🔴 THE SECOND MECHANISM, AND IT IS NOT REDUNDANT WITH WITHHOLDING THE
        # SCHEMA. `PluginRegistry.tool_specs` already hides a switched-off tool
        # from the model, but `dispatch_for` deliberately does not consult
        # gates — "dispatch has never been gated, only the offer is". So a name
        # the model REMEMBERS still arrives here.
        #
        # The case that makes this necessary rather than merely prudent is the
        # ordinary one: a tool used earlier in THIS conversation and switched
        # off mid-session. Its call and its result are already in the
        # transcript, so withholding the schema removes it from the inventory
        # while leaving a worked example of using it in the history — and a
        # model imitating its own transcript is exactly the T072 mechanism.
        # No amount of schema withholding reaches that; only this line does.
        #
        # Before resolution, for the same reason the global toggle is: a tool
        # the user switched off must not run even if a branch is added above.
        if name in (self.settings.tools_disabled or ()):
            return tool_denied("tool-disabled", name=name), False
        fn = self._dispatch_for(name)
        if fn is None:
            return tool_denied("unknown-tool", name=name), False
        policy = self.plugins.policy_for(name)
        if policy is None:
            return tool_denied("no-metadata", name=name), False
        decision = tool_policy.evaluate(
            # 🔴 THE FLOOR, NOT THE DEFAULT. If we cannot say what authority
            # this turn holds, the answer is the least authority -- never the
            # widest, and never a confirm profile that would prompt a room
            # with nobody in it. This read `INTERACTIVE` while INTERACTIVE was
            # also the settings default, so the two agreed by coincidence;
            # T084 moved the default to `autonomous` and that coincidence
            # became a contradiction pointing the permissive way.
            getattr(self, "_active_tool_profile", None) or tool_policy.SCHEDULED,
            policy,
            args,
            paths.ROOT,
            tool_name=name,
            always_allow=frozenset(self.settings.tool_always_allow or ()),
            deny=frozenset(self.settings.tool_deny or ()),
        )
        if decision.action == tool_policy.DENY:
            return tool_denied("profile", name=name, reason=decision.reason), False
        if decision.action == tool_policy.CONFIRM:
            # Sidebar or modal, decided by the setting. `show_dialog` — not
            # `open_dialog` — because this frame ALREADY awaits, and the whole
            # turn is blocked on the answer. It returns the body's value, or
            # None on cancel: the same falsy-on-cancel contract
            # `push_screen_wait` had, so `not answer` below is unchanged.
            #
            # T577: A HEADLESS CHILD HAS NO KEYBOARD, SO THE QUESTION GOES TO
            # ITS HOST. The branch is HERE, at the awaiting caller, and not
            # inside `show_dialog`: that door is generic and three other
            # callers use it with no approval semantics at all, so an rpc
            # branch in there would make every dialog rpc-aware to serve one.
            # Ryan, 2026-09-10: "i want the approvals to route threw frontier
            # chat GUI".
            if self._rpc:
                answer = await tool_approval.approve_over_rpc(
                    self, name, args, decision
                )
                # None is NOT DENIED. See approve_over_rpc: one is a person
                # choosing, the other is a host that never spoke, and they
                # owe the model different sentences.
                unanswered = answer is None
            else:
                answer = await show_dialog(
                    self,
                    partial(ToolApprovalBody, name, args, decision),
                    modal_factory=partial(ToolApprovalScreen, name, args, decision),
                )
                unanswered = False
            # `not answer` covers three cases on purpose: DENIED, and None from
            # a screen dismissed without a value, and any future falsy answer.
            # ToolApproval.__bool__ is what keeps this line correct now that the
            # modal returns a tri-state instead of a bool.
            if not answer:
                # DENY STOPS THE TURN (Ryan, 2026-08-24 — asked whether this
                # should replace or supplement the existing verb, answered
                # REPLACE). The refusal is still returned and still recorded,
                # so the transcript says what happened; the loop just does not
                # get another round-trip to work around it with.
                #
                # The reason is set alongside the flag because the loop's tail
                # otherwise reports "reached N tool iterations" for ANY early
                # break — see _stop_reason.
                self._stop_requested = True
                if unanswered:
                    # SAY IT. A timeout that reads as "you denied" would tell
                    # the model a person refused, and a person who refused is
                    # a reason to stop asking -- so a broken host would look
                    # like a settled decision, forever.
                    self._stop_reason = (
                        f"[stopped — no host answered the approval for {name} "
                        f"within {tool_approval.APPROVAL_TIMEOUT_S:.0f}s; denied]"
                    )
                    return tool_denied("no-host", name=name), False
                self._stop_reason = f"[stopped — you denied {name}]"
                return tool_denied("by-user", name=name), False
            if answer.remember:
                self._remember_tool_rule(name, decision.capabilities)
        # BACKGROUND (T499): the model asked not to wait. Authorization above
        # is identical — the profile decision and any CONFIRM are taken HERE,
        # once, at fire time — and the door is still the one call below: the
        # awaitable is built once and either awaited now or handed to a
        # worker that awaits it. `tools/tool_door_gate.py` counts that call.
        # Only a tool whose schema declares `background` may leave the turn (T517,
        # Ryan: "not everything should be backgroundable"): the flag on any other
        # tool is dropped, and the auto-promotion below never applies to it.
        may_bg = tasks_mod.backgroundable(name)
        background = may_bg and isinstance(args, dict) and bool(args.pop("background", False))
        if isinstance(args, dict):
            args.pop("background", None)
        aw = asyncio.to_thread(fn, args)
        if background:
            return self._start_background(name, args, aw), True
        # AUTO-PROMOTION (T517, Ryan: "the calls are still blocked is it off by
        # default or something ?"): the model almost never asks for background,
        # so a call still running after the settings threshold becomes a
        # background task on its own — the same future, handed over, never a
        # second start. 0 turns it off.
        limit = int(getattr(self.settings, "tool_auto_background_s", 0) or 0) if may_bg else 0
        done, fut = await tasks_mod.wait_or_promote(aw, limit)
        if not done:
            return self._start_background(name, args, fut, promoted_after=limit), True
        try:
            return str(fut.result()), True
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}", False

    # ── background tasks (T499) ──────────────────────────────────────────
    #
    # A tool call the turn does not wait for. Input -> do work -> output,
    # with the output arriving as INPUT NOBODY TYPED — so it is delivered
    # down `_deliver_inbox`, the funnel mail and cron already use: held
    # mid-turn, flushed after, wakes the agent unattended. Why the result
    # cannot be a late role:"tool" message is in tasks.py's docstring.

    def _start_background(self, name: str, args: dict, aw, promoted_after: float | None = None) -> str:
        task = tasks_mod.new_task(name, args, getattr(self, "convo_id", ""))
        if promoted_after:
            # The call began in the FOREGROUND, so its child (a shell) sits in the
            # cancel slot, not on a task. It is the task's now — `/tasks kill` needs
            # the handle, and the cancel button must not reach a backgrounded child.
            task.proc, ttyguard.CANCELLABLE["proc"] = ttyguard.CANCELLABLE.get("proc"), None
        self.bg_tasks[task.id] = task
        self._save_background()
        # 🔴 NO `label=` HERE. `tasks.label_of` returns the first 60 characters
        # of the call's `command` or `prompt` (tasks.py:96), so passing it wrote
        # the request itself into the ALWAYS-ON runtime log -- the exact thing
        # `PROHIBITED` in tests/test_runtime_log_producers.py names, and `label`
        # is in that set by name. The chat line and the Background panel still
        # show it; the log gets the id and the tool, which is what a log needs to
        # correlate a failure without carrying what the user typed.
        runtime_log.record("task.started", task_id=task.id, tool=name)
        # Its own group, NOT "chat": `_stream` is exclusive in "chat", and a
        # worker started there would cancel the turn that started it.
        self.run_worker(
            self._run_background(task, aw),
            name=task.id, group="tasks", exclusive=False, exit_on_error=False,
        )
        return tasks_mod.start_text(task, paths.data_root(), promoted_after=promoted_after)

    async def _run_background(self, task, aw) -> None:
        # Set in THIS task's context before the door runs: asyncio.to_thread
        # copies the context when it executes, so `_run_shell` sees the task
        # and parks the child on it instead of the foreground cancel slot.
        tasks_mod.CURRENT.set(task)
        try:
            result, ok = str(await aw), True
        except Exception as e:
            result, ok = f"[error] {type(e).__name__}: {e}", False
        text = tasks_mod.finish(task, result, ok, paths.data_root())
        self._save_background()
        runtime_log.record(
            "task." + task.state, task_id=task.id, tool=task.tool,
            seconds=round(task.seconds, 1),
        )
        self._deliver_inbox({"from": "task", "priority": "normal", "body": text})

    def _save_background(self) -> None:
        """The task set MOVED: persist it, and repaint the footer.

        🔴 THE REPAINT IS HERE BECAUSE THIS IS THE ONE PLACE EVERY TRANSITION
        ALREADY PASSES THROUGH — start, finish and kill. `ctx_label_text` is a
        PROPERTY: it recomputes correctly on every call, so the chip was never
        wrong, it was never ASKED. `_start_background` and `_run_background`
        both move `bg_tasks` and neither had any reason to touch the footer, so
        the label kept whatever it was painted with the last time something
        unrelated refreshed it — which is why Ryan's screenshot (2026-09-10
        20:5x) showed `bg:1` beside a Background panel reading 0 after a 300s
        timeout finished. The panel was right: its `sync()` rebuilds on the
        membership change. The chip had simply not been repainted since.

        ⚠️ THE PANEL AND THE CHIP WERE ALREADY ONE SOURCE (`tasks.split_live`),
        so this is not a second reader being reconciled — adding one would have
        been the wrong fix. What was missing is that a background task finishing
        is the only state change in this app with NO user action attached to
        hang a repaint on.

        ⬜ A FINISHING TASK CANNOT WAIT FOR THE NEXT UNRELATED REFRESH, which is
        what made this look intermittent: a seat that keeps typing repaints the
        footer constantly and never sees it; a seat that fires a long task and
        waits sees a stale chip for as long as it waits.
        """
        try:
            tasks_mod.save(self.bg_tasks.values(), paths.data_root())
        except OSError:
            pass  # an unwritable store must not stop the task
        # AFTER the save, and deliberately not inside the try: a store that
        # cannot be written is a reason to keep going, not a reason to leave the
        # footer lying about what is running.
        self._refresh_ctx_label()

    def _kill_background(self, task_id: str) -> str | None:
        """Kill one background task. Returns None when it was killed, otherwise
        the reason it was not — same text the chat line carries.

        🔴 THE RETURN EXISTS FOR THE RPC CALLER (T571), and the refusals are the
        point of it. `--rpc` must answer ok/error, and the two conditions below
        are the whole difference between a kill and a no-op. A second copy of
        them on the rpc side would be a kill that reports success and takes
        nothing down — the process outlives the confirmation. `/tasks kill`
        ignores the return and behaves exactly as before.
        """
        task = self.bg_tasks.get(task_id)
        if task is None:
            reason = f"no task {task_id}"
            self._system(reason)
            return reason
        if task.state != tasks_mod.RUNNING or task.proc is None:
            reason = f"{task_id} is {task.state}; nothing to kill"
            self._system(reason)
            return reason
        task.state = tasks_mod.KILLED
        # 🔴 THE THIRD TRANSITION, AND IT WAS NOT PERSISTED EITHER. A kill moved
        # the state in memory only: the store still said `running`, so
        # `tasks.load` marked it LOST at the next boot rather than KILLED — a
        # task reported as "gone with the app" when it was deliberately stopped.
        # The same call now also repaints the footer, which is what takes the
        # chip down; see `_save_background`.
        self._save_background()
        self.notify(f"Killing {task_id}…", timeout=2)
        self._kill_background_tree(task)
        return None

    @work(thread=True, group="cancel")
    def _kill_background_tree(self, task) -> None:
        """Off the UI thread, like `_cancel_tool_tree`: kill_tree can block for seconds."""
        proc = task.proc
        killed = ttyguard.kill_tree(proc.pid, proc)
        msg = (
            f"{task.id} killed" if killed
            else f"{task.id}: kill could not be confirmed — the process may still be running"
        )
        self.call_from_thread(
            self.notify, msg, timeout=4, severity="information" if killed else "warning",
        )

    def _remember_tool_rule(self, name: str, capabilities: frozenset[str]) -> None:
        """Record "always allow" so the same question is not asked twice.

        Keyed by tool AND the capabilities that triggered THIS prompt, so the
        rule grants exactly the authority the human was shown and nothing
        wider.  The same tool asking for more prompts again.

        A failed write costs one repeated prompt next session; raising here
        would fail the tool call the human just approved, which is the worse of
        the two.  The in-memory rule still holds for the rest of this session,
        and the message says so rather than leaving a silent half-success.
        """
        key = tool_policy.rule_key(name, capabilities)
        if key in self.settings.tool_always_allow:
            return
        self.settings.tool_always_allow.append(key)
        try:
            settings_mod.save(self.settings)
        except OSError:
            self._system(
                f"could not save the always-allow rule for {key} "
                "— it holds for this session only"
            )

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
        block = appsvc.store_block(self, )
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
        """T507-T5: no-op — the seat id is now process-stable (process_agent_id).
        Conversation changes no longer rebind; one id per process, no ghosts."""
        pass

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

    def _append(self, msg: dict) -> None:
        """Append to the live conversation AND to disk. Single choke point."""
        self.conversation.append(msg)
        self.store.record_msg(msg)

    def _append_to_system(self, text: str) -> None:
        """Extend the FIRST system message rather than adding another one.

        Multiple role:"system" turns are not portable. qwen/qwen3.8-27b's chat
        template raises "System message must be at the beginning" and the request
        fails with a 500; other builds of the same model accept it. Anything the
        model must know belongs in the one system turn it is guaranteed to read.

        🔴 RETURNED FROM `appsvc` (O2 `22a7834` lifted it; ruled back here). It
        WRITES `self.conversation[0]`, and `appsvc` holds helpers that take `app`
        and READ it. §5c had already withdrawn `_sync_fleet_identity` permanently
        for writing the SAME object through the SAME channel — two rulings on one
        object have to agree, and `conversation` is read at 48 sites in `src/`.
        """
        if not self.conversation or self.conversation[0].get("role") != "system":
            self._append({"role": "system", "content": text})
            return
        current = self.conversation[0].get("content") or ""
        if text in current:
            return                      # idempotent across resume/re-register
        self.conversation[0] = {
            **self.conversation[0],
            "content": current.rstrip() + "\n\n" + text if current else text,
        }
        if not getattr(self, "_convo_loading", False):
            self._edit(0, "system prompt extended")

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
        does not run the full constructor. (Found by tests/test_seat_rebind.py,
        deleted in T585 with Seat.rebind; the finding outlived the file.)
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

    # -- the supported plugin surface ------------------------------------
    #
    # Added by S4 as PURE ADDITIONS beside the then-private `_jobs` and
    # `_mcp_dispatch`, so the arrival commit was green standing alone: converting
    # this file's own uses in the same commit would have made it a rename wearing
    # a refactor's clothes (PLAN §2b).
    #
    # ⚠️ `_jobs` IS GONE as of O3 — `CronService` owns the list and `jobs`
    # delegates to it, so this is no longer a facade over a private field beside
    # it. `_mcp_dispatch` is unchanged and still private.

    @property
    def jobs(self) -> list:
        """The live scheduler job list -- SHARED MUTABLE STATE, not a copy.

        NOT READ-ONLY, despite what T070's plan called it. Callers mutate
        through it: `plugins/scheduler_ui._apply_job_edit` does
        `jobs.remove(job)` / `jobs.append(...)` and then saves. Returning a
        copy here would silently drop every edit made in the calendar UI.
        """
        return self._cron.jobs

    @property
    def cron(self) -> "cron_mod.CronService":
        """The scheduler service, which OWNS the job list.

        Public so `scheduler_plugin` reaches a supported surface instead of two
        app privates: `app.cron.command(...)` and `cron_mod.monitor(app)` replace
        `app._cron_command(...)` and `app._cron_monitor()`.
        """
        return self._cron

    @property
    def mcp_dispatch(self) -> dict:
        """The cached MCP tool-name -> handler map.

        Read-only in practice -- the only caller does `.get(name)`. Cached at
        init from `self.mcp.dispatch()`; a caller who rebuilds it per lookup
        pays for the whole map on every tool call.
        """
        return self._mcp_dispatch

    def rebuild_mcp_dispatch(self) -> None:
        """Re-cache the MCP dispatch map after a server starts or stops.

        🔴 THIS IS THE ONE STALE THING, AND ONLY THIS ONE. `mcp_plugin` registers
        MCP as a DYNAMIC provider whose specs callable is `lambda: app.mcp.tool_specs()`
        -- evaluated per turn, so a newly connected server's tools reach the model
        by themselves. Its dispatch callable is `lambda name: app.mcp_dispatch.get(name)`,
        which reads THIS cache, built once in __init__. So without this call a
        connect would advertise tools the loop cannot route: the model sees the
        schema, calls it, and gets "unknown tool" -- a failure that reads as a
        bad model rather than a stale map.

        Cheap and idempotent, so every lifecycle verb calls it unconditionally
        rather than trying to work out whether the set actually changed.
        """
        self._mcp_dispatch = self.mcp.dispatch()

    # ── conversation store ───────────────────────────────────────────
    # Implementations live in litetui.conversation.ConversationRepository.
    # The aliases that carried `read`, `fmt_size`, `label` and `list_all`
    # through the move are GONE -- their callers now name the repository.
    # These two are still aliased because their call sites were not in that
    # commit's scope, so they are the last of this set, not a pattern to
    # extend. Aliases, not wrappers -- one implementation, not two.
    _convo_title = staticmethod(ConversationRepository.title)
    _flatten = staticmethod(ConversationRepository.flatten)


    def _resume(self, path: Path) -> None:
        try:
            meta, msgs = ConversationRepository.read(path)
        except OSError as e:
            # The file name stays (it IS the action); the raw OS error goes to
            # the sink, never into chat.
            runtime_log.record_error(
                "resume_read_failed",
                detail=f"{path}: {type(e).__name__}: {e}",
                exc=e,
            )
            self._system(f"Could not read {path.name} — the file seems locked or unreadable.")
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


    def update_header(self) -> None:
        if self.tools_enabled:
            # COUNTED, not quoted. "tools:4" was a literal from when there
            # were exactly four, and it stayed 4 while view_image, chrome,
            # pccontrol, ask_user_question, skill, harness and the MCP set
            # arrived — the header lied for weeks. _all_tools() is what the
            # MODEL is offered, so the header now derives from the same list.
            mode = f"tools:{len(self._all_tools())}"
        else:
            mode = "no tools"
        level = self.thinking_level or "default"
        model_levels = getattr(self, "_model_thinking_levels", None)
        if self.backend.name == "lmstudio" and level not in ("off", "default"):
            if model_levels and level in model_levels:
                think = f"think:{level}"
            elif model_levels and level not in model_levels:
                think = f"think:on ({level} kept for llama.cpp)"
            else:
                think = f"think:on ({level} on llama.cpp)"
        else:
            think = f"think:{level}"
        cwd = str(Path.cwd())
        home = str(Path.home())
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]
        engine = {"llamacpp": "llama.cpp", "codex": "Codex OAuth"}.get(self.backend.name, "LM Studio")
        parts = [p for p in (engine, self.model_id, mode, think, cwd) if p]
        self.sub_title = " \u00b7 ".join(parts)
        # The footer carries the thinking level too, and it only refreshed on a
        # context update -- so /think changed the header instantly and left the
        # footer lying until the next completion came back.
        self._refresh_ctx_label()

    # ARRIVAL ALIAS (PLAN §2b). One implementation under two names, so this
    # commit is green STANDING ALONE: app.py's own private call sites keep
    # working untouched, and converting them here would make the arrival a
    # rename wearing a refactor's clothes. The alias goes when the consumers do.
    _update_header = update_header

    def action_toggle_tools(self) -> None:
        """THE one verb for the global tools switch. Ctrl+T, the `/tools`
        list's global toggle, and the settings screen all end up here or do
        exactly what it does.

        🔴 IT PERSISTS (Ryan, 2026-08-24, asked and ruled). It used to write
        only the RUNTIME flag, never `settings.tools_enabled` — so Ctrl+T was
        silently session-scoped while the settings switch was not, and the two
        disagreed the moment you used either:

          * open Settings after Ctrl+T and the switch showed the SAVED value,
            not the live one — a control lying about the state it governs;
          * save Settings after Ctrl+T and your toggle was silently REVERTED,
            by a screen you opened to change something else entirely.

        Nothing documented that split, and boot reads the persisted field, so
        it read as an oversight rather than a design. Ryan was offered keeping
        Ctrl+T as a deliberate temporary override and chose one behaviour
        everywhere: "make all three persist".
        """
        self.tools_enabled = not self.tools_enabled
        # Written through the SAME field the settings screen binds to, so
        # there is nothing to keep in sync — there is one value with three
        # surfaces onto it.
        self.settings.tools_enabled = self.tools_enabled
        try:
            settings_mod.save(self.settings)
        except OSError:
            # A toggle that lasts one session beats a crash on Ctrl+T. Said
            # out loud rather than swallowed: a silent half-success here is
            # exactly the disagreement this change exists to remove.
            self._system("could not save the tools setting — this session only")
        if self.conversation and self.conversation[0].get("role") == "system":
            # Rebuilt through the single builder. Hand-rolling it here is how
            # the store block gets dropped on the first Ctrl+T.
            self.conversation[0]["content"] = self._system_prompt_text()
            self._edit(0, "tools toggled")  # one message, not the whole list
        state = "ON (bash, read, write, web_fetch)" if self.tools_enabled else "OFF"
        self._system(f"Tools {state} — Ctrl+T to toggle")
        self._update_header()

    @classmethod
    def binding_key(cls, action: str) -> str:
        """The key currently bound to `action`, or "" if nothing is.

        One place asks the binding table what a chord is, so prose about a key
        cannot drift from the key. Returning "" rather than raising is
        deliberate: an action may legitimately have no chord, and a missing
        binding must not take down the message that mentions it.
        """
        for binding in cls.BINDINGS:
            if getattr(binding, "action", None) == action:
                return getattr(binding, "key", "") or ""
        return ""

    def action_toggle_plan_mode(self) -> None:
        """ctrl+p: plan mode on or off (T558).

        The instruction is not appended to the conversation — it is a PROMPT
        SECTION gated on the mode, rebuilt here through the same single builder
        Ctrl+T uses. That is what makes leaving the mode actually leave it: a
        message appended on entry would still be sitting in context afterwards,
        telling the model to refuse to build long after the user asked it to.
        """
        self.set_plan_mode(not self._plan_mode)

    def set_plan_mode(self, on: bool, *, announce: bool = True) -> bool:
        """Plan mode to an EXPLICIT value. True when it actually changed.

        The key toggles; the wire sets (T558-B). Both land here so ONE place
        knows what entering and leaving the mode involves. The conversation
        rebuild is the part that is easy to write twice and get subtly
        different, and the second copy is the one that forgets it.
        """
        on = bool(on)
        if on == self._plan_mode:
            return False
        self._plan_mode = on
        if self.conversation and self.conversation[0].get("role") == "system":
            self.conversation[0]["content"] = self._system_prompt_text()
            self._edit(0, "plan mode toggled")  # one message, not the whole list
        if announce:
            # 🔴 THE KEY IS READ FROM THE BINDING, NEVER TYPED HERE (T573).
            # These two lines carried "Ctrl+P" as a literal while the binding
            # lived in BINDINGS, so moving the key would leave the app telling
            # the user to press something that no longer does anything -- in
            # the one message they are guaranteed to read. `key_label` returns
            # "" when the action has no binding at all, and the clause composes
            # out, so a keyless plan mode announces cleanly too.
            chord = key_label(self.binding_key("toggle_plan_mode"))
            leave = f" {chord} to leave." if chord else ""
            re_enter = f" — {chord} to re-enter" if chord else ""
            if self._plan_mode:
                self._system(
                    "Plan mode ON — ls-plan-w-quizmaster, questions through "
                    f"ask_user_question, no building.{leave}"
                )
            else:
                self._system(f"Plan mode OFF{re_enter}")
        self._update_header()
        return True

    def action_cycle_tool_profile(self) -> None:
        """shift+tab: one step down the authority scale, wrapping.

        Ryan's order, from his own screenshots of Claude Code:
        autonomous -> interactive -> scheduled -> autonomous. The step itself
        lives in `tool_policy.cycle`, derived from the PROFILES order, so the
        direction is written once rather than here as a second list.

        PERSISTS, for the same reason Ctrl+T does (Ryan, this session, "make
        all three persist"): a key that changes a setting the settings screen
        also shows must not leave the two disagreeing. Written through the
        SAME field the screen binds to -- one value, several surfaces.

        ⚠️ IT ALSO RETARGETS THE TURN IN FLIGHT. `_active_tool_profile` is what
        `_execute_tool` actually reads, so without this line a press during a
        running turn would change the footer and the saved setting while the
        tools kept running under the old authority -- the footer would be
        lying at exactly the moment someone is using the key to stop something.
        Restricting takes effect on the next tool call, which is the direction
        that matters.
        """
        if side_panel.handle_reverse_tab(self):
            # A dialog owns the key while it is open. Returning here is what
            # keeps Tab from walking out of a pending approval -- the app
            # binding is priority, so nothing else would stop it.
            return
        self.set_tool_profile(tool_policy.cycle(self.settings.tool_policy_profile))

    def set_tool_profile(self, profile: str, *, announce: bool = True) -> bool:
        """Authority to an EXPLICIT profile. False when the name is unknown.

        shift+tab cycles; the wire sets (T558-B). One body, so the two cannot
        drift — and in particular so the wire cannot forget
        `_active_tool_profile`, which is the field `_execute_tool` actually
        reads. Setting only the SETTING would move the footer and the saved
        value while tools kept running under the old authority: the readout
        would be lying at exactly the moment someone is using it to restrict
        something.

        An unknown name is REFUSED, never coerced. Storing a profile that
        resolves to nothing leaves the app with no policy at all, which is a
        worse answer than the caller being told no.
        """
        if profile not in tool_policy.PROFILES:
            return False
        self.settings.tool_policy_profile = profile
        self._active_tool_profile = profile
        try:
            settings_mod.save(self.settings)
        except OSError:
            # Same call as Ctrl+T: said out loud, never swallowed. A silent
            # half-success is the disagreement this persistence exists to end.
            self._system("could not save the authority level — this session only")
        self._refresh_ctx_label()
        if announce:
            self._system(
                f"{profile_text(profile)} — {tool_policy.PROFILES[profile].summary}"
            )
        return True

    # ── Connection ───────────────────────────────────────────────

    @work(exclusive=True, group="init")
    async def connect(self) -> None:
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
                if self.backend.name == "lmstudio":
                    self._probe_thinking()
            else:
                self.sub_title = "No model loaded"
                # No URL here: a bare address tells a human nothing to DO. Name
                # the action instead; which host was tried is in the log if it
                # ever matters (no models means none on that server).
                if self.backend.name == "lmstudio":
                    self._system(
                        "No chat model available from LM Studio — download one "
                        "in LM Studio's Model Manager."
                    )
                else:
                    self._system(
                        "No chat model available from llama.cpp — add a folder "
                        "of GGUF files in /settings, or download one in "
                        "LiteSuite's Model Hub."
                    )
        except Exception as e:
            runtime_log.record(
                "backend_connect_failed",
                site="app.connect",
                component="backend",
                name=self.backend.name,
                operation="connect",
                error_type=type(e).__name__,
            )
            # Raw detail (the host we actually tried + the exception) lands in
            # the sink; chat gets plain words. The old line named the host so a
            # reader could blame it — T137 keeps that fact for debugging, out of
            # chat, where a URL tells nobody what to do.
            runtime_log.record_error(
                "backend_connect_failed",
                detail=f"connect to {self.backend.host()}: {type(e).__name__}: {e}",
                exc=e,
            )
            self.sub_title = "Disconnected"
            self._system(_plain_backend_error(e, self.backend.name))

    _connect = connect          # arrival alias (PLAN §2b)

    def _rpc_emit(self, data: dict) -> None:
        """Emit one JSON event on stdout (no-op outside --rpc)."""
        if not self._rpc:
            return
        from litetui.rpc import rpc_emit
        rpc_emit(data)

    @work(exclusive=True, group="cli-args-ready")
    async def _rpc_emit_ready(self) -> None:
        """Emit the ready event once the model list is available."""
        for _ in range(20):
            if self.available_models:
                break
            await asyncio.sleep(0.5)
        from litetui.version import __version__

        # T594: resolve BEFORE announcing, so `ready` names the model that
        # will actually answer rather than the one that was configured.
        note = ""
        if getattr(self, "_rpc", False):   # doubles predate this seam
            action, model, why = self._headless_model_decision()
            if action == "substitute" and model:
                self.model_id = model
                note = why
            elif action == "refuse":
                note = why
        self._rpc_emit({
            "type": "ready",
            "version": __version__,
            "model": self.model_id or None,
            # T631: the host cannot derive this. LiteSuite spawns us with --rpc,
            # --cwd, --tool-profile, --mode and --model and NO backend -- the
            # choice is ours, from our own settings -- so its Frontier Chat pill
            # had nothing to show but the placeholder it sent us. `getattr`
            # twice and never str(): a stubbed backend with no `name` must
            # report null so the host renders nothing, where the string "None"
            # would put that word on screen.
            "backend": getattr(getattr(self, "backend", None), "name", None),
            **({"model_note": note} if note else {}),
            "cwd": os.getcwd(),
            "tool_profile": str(getattr(self, "_active_tool_profile", None)),
            # T647 — WHAT WE WERE ASKED FOR, BESIDE WHAT WE ARE RUNNING AS.
            # `tool_profile` alone is ambiguous: a child reporting "autonomous"
            # means EITHER the host passed --tool-profile autonomous OR it passed
            # nothing and we fell back to our own settings default, which IS
            # autonomous. Those are a deliberate choice and a fail-open, and they
            # were indistinguishable on the wire — which is why T577's profile had
            # to be derived from reading defaults instead of read. null here means
            # NOBODY ASKED.
            "tool_profile_requested": getattr(self, "_cli_tool_profile", None) or None,
        })

    @work(exclusive=True, group="cli-args")
    async def _apply_cli_args(self) -> None:
        """T507-T1: apply --model, --system-prompt, --prompt after connect."""
        # Wait for connect to populate available_models (up to 10s).
        for _ in range(20):
            if self.available_models:
                break
            await asyncio.sleep(0.5)
        if self._cli_initial_model:
            want = self._cli_initial_model
            loaded = {r.key for r in self.model_rows.values() if r.loaded}
            if want in loaded:
                self.model_id = want
                self._update_header()
                self._fetch_ctx_window()
            elif want in self.available_models:
                self._system(
                    f"[cli] --model {want!r} is downloaded but NOT loaded — "
                    f"load it first in LM Studio or use /model. "
                    f"Using {self.model_id!r}."
                )
            else:
                self._system(
                    f"[cli] --model {want!r} not found — "
                    f"loaded: {', '.join(sorted(loaded)) or '(none)'}. "
                    f"Using {self.model_id!r}."
                )
        if self._cli_system_prompt:
            self.conversation.insert(0, {"role": "system", "content": self._cli_system_prompt})
        if self._first_prompt:
            await self._ensure_chat_ready(timeout=15.0)
            self._submit_text(self._first_prompt, alt_chord=False)

    # ── Context window readout (footer) ───────────────────────

    def footer_nav_items(self) -> list[str]:
        """The navigable chips, in the order the footer draws them.

        🔴 BUILT FROM WHAT IS ACTUALLY VISIBLE, not from a fixed list. `bg` and
        `agents` are absent at zero and either can be hidden in /settings, so a
        fixed list would let Left/Right land on a chip that is not on screen —
        movement with no feedback, and then an Enter that opens something the
        user cannot see they selected.

        One source for the renderer and the key handler, for the same reason the
        counts have one source: two lists that agree today disagree the first
        time one of them learns about a new chip.
        """
        s = self.settings
        subs, bg = tasks_mod.split_live(getattr(self, "bg_tasks", {}).values())
        items = ["authority"]  # never hidden — see the note in ctx_label_text
        # PLAN SITS WHERE IT IS DRAWN, second. This list and the renderer are
        # the same order on purpose: Left/Right that walks a different sequence
        # from the one on screen is movement the user cannot follow.
        items.append("plan")
        if s.footer_show_thinking:
            items.append("think")
        if s.footer_show_bg and bg:
            items.append("bg")
        if s.footer_show_subagents and subs:
            items.append("agents")
        return items

    def footer_nav_move(self, delta: int) -> None:
        """Left/Right along the visible chips. Wraps, like the authority cycle."""
        items = self.footer_nav_items()
        if not items:
            self._footer_nav = None
            return
        if self._footer_nav not in items:
            # Selection was on a chip that has since gone (a task finished).
            # Land on an end rather than guessing where it "would have been".
            self._footer_nav = items[0] if delta > 0 else items[-1]
        else:
            i = items.index(self._footer_nav)
            self._footer_nav = items[(i + delta) % len(items)]
        self._refresh_ctx_label()

    def footer_nav_enter(self) -> None:
        """Down from the input: take the footer, on the first visible chip."""
        items = self.footer_nav_items()
        if not items:
            return
        self._footer_nav = items[0]
        self._refresh_ctx_label()

    def footer_nav_leave(self) -> None:
        self._footer_nav = None
        self._refresh_ctx_label()

    def footer_nav_activate(self) -> None:
        """Enter on the selected chip.

        Each arm CALLS the one body that already owns its behaviour rather than
        repeating it: the authority cycle is shift+tab's, the thinking picker is
        `/think` with no argument (T569), which carries its own no-screen-over-rpc
        guard. A second implementation here would be a second place for those to
        rot, and the picker's guard is the kind that fails by HANGING.
        """
        chip = self._footer_nav
        if chip == "authority":
            self.action_cycle_tool_profile()
        elif chip == "plan":
            # The SAME body Ctrl+P runs, for the reason the note above gives:
            # `set_plan_mode` is where entering and leaving the mode is defined
            # (the conversation rebuild especially), and a second copy here is
            # the one that would forget it.
            self.action_toggle_plan_mode()
        elif chip == "think":
            # 🔴 THE REGISTRY, NOT AN IMPORT. `from litetui.plugins.misc import
            # _cmd_think` reached the right body and re-accreted the monolith:
            # app.py owns the substrate and nothing below it, and
            # test_plugin_dogfood.py gates exactly that spelling. It went red at
            # 6b25437 and I did not see it, because the guard for a change in
            # app.py lives in a file named after plugins. `_handle_command` is
            # the door the keyboard already uses, so this is the same one body
            # by a route that cannot invert the layering.
            self._handle_command("/think")
        elif chip == "bg":
            from litetui.task_screens import open_background

            open_background(self)
        elif chip == "agents":
            from litetui.task_screens import open_subagents

            open_subagents(self)

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

        This stays ONE Text rather than one widget per field because Footer
        recomposes (see above), but it is width-aware before Textual gets a chance
        to clip the tail. The tail contains the context percentage -- the field a
        visual monitor needs most -- so optional chunks are dropped by explicit
        priority while authority, plan, seat and percent are never sacrificed.
        """
        s = self.settings
        sep = "  \u00b7  "
        chunks: list[tuple[str, Text]] = []

        # T570 — the selected chip, if the keyboard has taken the footer. Applied
        # by `add` so EVERY chip gets it for free: a per-chip opt-in is how one
        # of them ends up navigable but never highlighted, which reads as the
        # arrow key having done nothing.
        nav = getattr(self, "_footer_nav", None)

        def add(key: str, chunk: str, style: str, chip: str | None = None) -> None:
            chunks.append((
                key,
                Text(chunk, "reverse bold" if (chip and chip == nav) else style),
            ))

        # THE AUTHORITY LEVEL, FIRST AND WITHOUT A TOGGLE. Ryan asked for it
        # ("ALSO show this in the footer") after being denied a write while
        # Settings showed autonomous -- the whole bug was that the authority
        # actually in force was invisible. Every other field here is hideable
        # from /settings; this one deliberately is not, because a readout that
        # can be switched off reproduces exactly the condition that hid T084.
        #
        # The RESOLVED profile for the turn, not the stored setting: an
        # unattended turn can be running under something narrower than what
        # Settings says (tool_policy.unattended), and the whole point is to
        # show which one is actually in force. Absence renders as absence --
        # profile_text returns "" before any turn has resolved one.
        level = profile_text(getattr(self, "_active_tool_profile", None))
        if level:
            add("authority", level, "#7d8799" if tool_policy.stops_you(
                self._active_tool_profile) else "#7aa2f7", chip="authority")

        # PLAN MODE, beside Authority and unhideable for the same reason (T558):
        # it changes what the model will agree to do, so a mode you cannot see
        # is a refusal you cannot explain.
        #
        # 🔴 IT USED TO BE ABSENT WHEN OFF, and this note used to say an "off"
        # chip "would occupy the footer permanently to say nothing". That is
        # reversed deliberately — Ryan, liteask a-5d6c1ca0: "make sure plan mode
        # is toggelable via the footer ... once the user navs to the footer with
        # the arrow keys pressing enter should toggle plan mode". A chip you can
        # only reach while the mode is already ON is a switch with no OFF
        # position: you could leave plan mode from the footer and never enter it
        # there. Drawing it off is what makes it a control rather than a readout.
        plan_on = bool(getattr(self, "_plan_mode", False))
        add("plan", "plan:on" if plan_on else "plan:off",
            "bold #bb9af7" if plan_on else "#5c6370", chip="plan")

        # Identity, but only when the seat actually holds it. An unregistered
        # seat displaying a name it does not own is worse than showing nothing:
        # it is a green light for a registration that never happened.
        if s.footer_show_seat:
            seat = getattr(self, "seat", None)
            if seat is not None and getattr(seat, "registered", False):
                add("seat", str(seat.name), "bold #7d8799")
            elif seat is not None:
                add("seat", "unregistered", "#e5534b")
            else:
                add("seat", "no seat", "#5c6370")

        if s.footer_show_thinking:
            add("think", f"think:{self.thinking_level or 'default'}", "#5c6370", chip="think")

        # T570 — WHAT IS RUNNING WITHOUT ME. Ryan: "sub agents and background
        # process should show in the footer".
        #
        # ABSENT AT ZERO, never "bg:0". An idle session is the common case and a
        # permanent pair of zeros costs width to say nothing; a chip appearing is
        # itself the signal that something started.
        # getattr, like `_active_tool_profile` and `_plan_mode` above: the footer
        # is built by things that are not a whole app (the FakeApp in
        # tests/test_footer.py is one), and a readout that CRASHES because a
        # registry has not been constructed is worse than one that shows nothing.
        subs, bg = tasks_mod.split_live(getattr(self, "bg_tasks", {}).values())
        if s.footer_show_bg and bg:
            add("bg", f"bg:{len(bg)}", "#7aa2f7", chip="bg")
        if s.footer_show_subagents and subs:
            add("agents", f"agents:{len(subs)}", "#bb9af7", chip="agents")

        if s.footer_show_convo and self.convo_id:
            add("convo", self.convo_id[:8], "#5c6370")

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
                add("ctx", "ctx \u2014", "dim")
            else:
                u = f"{used:,}" if used is not None else "\u2014"
                m = f"{mx:,}" if mx is not None else "?"
                # A NOT-LOADED model's number is its CEILING, not its window.
                # Printing it unmarked is how "ctx / 262,144" can sit in the
                # footer while LM Studio is about to serve the model at 8k.
                if mx is not None and not getattr(self, "ctx_loaded", False):
                    add("ctx", f"ctx {u} / {m} max", "#5c6370")
                else:
                    add("ctx", f"ctx {u} / {m}", ctx_style)

        # The percent was ALREADY computed to pick the colour above and then
        # discarded, so the footer knew how full the window was and made you do
        # the division. Shown as its own field so it can be kept when the raw
        # counts are hidden — for most turns the ratio is the only part anyone
        # actually reads.
        if s.footer_show_context_pct and pct is not None:
            add("pct", f"{pct * 100:.0f}%", ctx_style)

        if s.footer_show_tps:
            stats = Text()
            appsvc.append_tps_into(self, stats, sep)
            if stats.plain:
                chunks.append(("tps", stats))

        # The footer owns the usable width. During its first compose it is
        # mounted but its children are not, so use the measured palette width
        # when available and its stable 12-cell footprint otherwise. The label's
        # CSS right padding consumes the remaining one cell. FakeApp unit tests
        # have no query/size at all; in that unmounted world width is unknown
        # and the full historical text is returned unchanged.
        available = getattr(self, "_footer_available_width", None)

        # Drop low-value fields until the protected visual-cron facts fit.
        # Display order never changes; only membership does. Calculate against
        # Rich cell widths (not len()) so wide glyphs cannot reintroduce clipping.
        if available is not None:
            for drop_key in ("tps", "convo", "bg", "agents", "think", "ctx"):
                total = sum(chunk.cell_len for _, chunk in chunks)
                total += Text(sep).cell_len * max(0, len(chunks) - 1)
                if total <= available:
                    break
                chunks = [(key, chunk) for key, chunk in chunks if key != drop_key]

        # Rich's explicit spans override CSS foreground, so a stylesheet-only
        # editor control would save successfully and leave the readout unchanged.
        # Only an EXPLICIT override recolours neutral text. Old themes keep
        # their palette; authority and warning/status colours retain meaning.
        theme = getattr(self, "current_theme", None)
        foreground = (getattr(theme, "variables", None) or {}).get("footer-foreground")

        def neutral_style(style: str) -> str:
            if foreground:
                return style.replace("#5c6370", foreground).replace("#7d8799", foreground)
            return style

        t = Text()
        for key, chunk in chunks:
            if foreground and key != "authority":
                chunk.style = neutral_style(str(chunk.style))
                chunk.spans = [
                    span._replace(style=neutral_style(str(span.style)))
                    for span in chunk.spans
                ]
            if t.plain:
                t.append(sep, foreground or "#5c6370")
            t.append(chunk)
        return t


    def _rpc_emit_usage(self, used: int | None) -> None:
        """Tell the host how full the window is. No-op outside `--rpc`.

        🔴 THE NUMBERS EXISTED ALL ALONG AND NEVER LEFT THE PROCESS (T645). The
        host's meter reads a `thread.token-usage.updated` activity, the codex
        adapter emits one, and litetui emitted nothing — so every LiteTUI thread
        showed "Context usage not reported yet" while `ctx_used` sat right here.

        The key spellings are the host's, not ours: `findNumberDeep`
        (session-logic.ts:206-230) accepts `context_tokens` and
        `max_context_tokens`, so matching what it already reads costs one
        docstring instead of a new projection.

        🔴 `max_context_tokens` IS WITHHELD UNLESS `ctx_loaded`. `ctx_max` may be
        the model's advertised CEILING rather than the window LM Studio actually
        loaded — the qwen 262,144-vs-8k case — and the ring DIVIDES by it. A
        ceiling would draw a confident, wrong, near-empty meter; omitting it makes
        the host return null and the pane keeps saying "not reported yet", which
        is what it says today and is TRUE. An honest blank beats a wrong number.

        ⚠️ AND NOTHING IS SENT WITH NO `used`. Before the first turn that is None,
        and reporting 0 would claim an EMPTY window rather than an unmeasured one.
        """
        if used is None:
            return
        usage: dict = {"context_tokens": used}
        if self.ctx_max and getattr(self, "ctx_loaded", False):
            usage["max_context_tokens"] = self.ctx_max
        # The provider's own accounting rides along when a turn has produced any;
        # extra keys are harmless because the host looks for the ones it knows.
        for key, value in (getattr(self, "last_usage", None) or {}).items():
            if isinstance(value, (int, float)):
                usage[key] = value
        self._rpc_emit({"type": "usage", "usage": usage})

    def watch_ctx_used(self, value: int | None) -> None:
        self._refresh_ctx_label()
        # T645: the host's context meter, fed from the same reactive the TUI's
        # own label reads — so the two can never disagree about the number.
        self._rpc_emit_usage(value)
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
    async def apply_context_length(self, force: bool = False) -> None:
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
        if getattr(getattr(self, "backend", None), "remote", False):
            return
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
            # Cleaned at source (T137): the message is already plain words —
            # report it, never a generic failure.
            self._system(str(e))
            return
        except Exception as e:
            runtime_log.record_error(
                "context_length_failed",
                detail=f"load {self.model_id} ctx={want}: {type(e).__name__}: {e}",
                exc=e,
            )
            self._system("Could not set the context length — something went wrong while loading the model.")
            return
        # Re-read rather than assume: the request is what we asked for, the
        # readout is what we got, and the server may clamp to what fits in VRAM.
        self._fetch_ctx_window()

    _apply_context_length = apply_context_length     # arrival alias (PLAN §2b)

    @work(exclusive=True, group="ctx")
    async def fetch_context_window(self) -> None:
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

    # The public name spells out "context" to match `apply_context_length`;
    # the alias keeps the abbreviated private spelling its 4 in-file callers use.
    _fetch_ctx_window = fetch_context_window         # arrival alias (PLAN §2b)

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

    # 🔴 RETURNED FROM `appsvc` (O2 `22a7834` lifted all three; ruled back here).
    # `_glassbox` writes `self._gb_last`, and appsvc holds helpers that take
    # `app` and READ it. The other two came back with it, not on their own
    # merits: each is a one-line wrapper, so leaving them there would have made
    # them call `app._glassbox(...)` — an app method that WRITES, which the
    # rephrased appsvc contract denies. Restored from the pre-O2 blob verbatim
    # rather than by un-transforming the moved copy.

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

    def watch_tps(self, value: float | None) -> None:
        self._refresh_ctx_label()

    # -- elapsed time while a turn is in flight -----------------------
    # Ryan's "..." read as nothing happening while LM Studio chewed the prompt.
    # While no answer token has arrived (and while tool calls run), the bubble
    # shows live ELAPSED TIME. The display string is a pure function
    # (render_progress); this is only the repaint glue. The repaint task runs
    # on the worker's own event loop, interleaving with the `async for` stream.

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
        self._elapsed.ensure_running()

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
        # Freeze, don't reset — the readout (time · tokens · avg tok/s) stays
        # on the header for every turn instead of being wiped to a label.
        t.freeze_header()

    async def _elapsed_repaint(self) -> None:
        # Repaint the in-flight bubble and any running tool calls ~4x/sec.
        # Self-retires after ~1s with nothing active, so it never outlives the
        # turn by long; ElapsedState.start also cancels a lingering one.
        idle = 0.0
        while True:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            card = self._compact_card
            card_live = card is not None and card._took is None
            active = (
                self._elapsed.body is not None
                or self._inflight_tools
                or self._thinking_live is not None
                or card_live
            )
            # The cancel button tracks the SLOT, not the tool bubble — and not
            # child liveness: a populated slot means communicate() may still be
            # blocked, including with the direct child already dead and a
            # grandchild holding the pipes (the state action_cancel_tool's old
            # poll() guard refused to cancel). A button shown for a tool with
            # nothing in flight would be a control that does nothing.
            try:
                live = ttyguard.CANCELLABLE["proc"] is not None
                for btn in self.query(CancelToolButton):
                    btn.set_class(live, "visible")
            except Exception:
                pass
            if active:
                if self._elapsed.body is not None:
                    # ETA: project this turn's prompt-processing time from the
                    # previous turn's token count and the learned median rate.
                    # Before any reliable turn both are None and the pure fn
                    # yields elapsed-only (the honest state).
                    self._elapsed.body.content = render_progress(
                        self._elapsed.body_t0, now,
                        self._eta.estimate_tokens(), self._eta.learned_rate())
                if self._thinking_live is not None:
                    # The app owns the tps reactive; the block only renders it.
                    # The reasoning half of TpsState's partition — not a
                    # separate tally, so it can never disagree with the
                    # footer's output count or with the rate.
                    self._thinking_live.repaint_header(
                        self.tps, self._tps.reasoning)
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

    @work(thread=True, group="thinking-probe", exclusive=True)
    def _probe_thinking(self) -> None:
        """T540: discover the real thinking levels for the current model.

        🔴 T611 — THIS IS A CHAT REQUEST, AND IT FIRES WITHOUT A USER TURN.
        `connect()` calls it for every lmstudio connect, headless children
        included, and the probe POSTs to /api/v1/chat (or five times to
        /v1/chat/completions) naming `model_id` — through urllib, not through
        model_transport, so nothing `_ensure_chat_ready` does reaches it.
        `connect()` can set `model_id` from the PERSISTED `default_model`
        filtered against `available_models`, which is the DOWNLOADED listing
        and not the resident one, so the id can be cold with nobody present.
        Same three-branch answer as everywhere else, and READ-ONLY: the probe
        asks about the model that will answer, it does not choose one.
        """
        model = self.model_id
        if not model:
            return
        if getattr(self, "_rpc", False):   # doubles predate this seam
            action, resident, _why = self._headless_model_decision()
            if action == "refuse":
                return
            if action == "substitute" and resident:
                model = resident
        host = self.settings.lm_host
        seed = getattr(self.settings, "lmstudio_graded_thinking_models", ())
        levels = thinking_probe.get_effective_levels(host, model, seed)
        self._model_thinking_levels = levels
        self._update_header()

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
                        b64 = appsvc.load_image_file(self, p)
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
            runtime_log.record_error(
                "image_paste_failed",
                detail=f"{type(e).__name__}: {e}",
                exc=e,
            )
            self.notify("Paste failed — the clipboard did not hold a readable image.", severity="error", timeout=3)

    # ── Modal callbacks ──────────────────────────────────────────

    def on_model_picked(self, model_id: str | None) -> None:
        if not model_id or model_id == self.model_id:
            return
        self.model_id = model_id
        self._model_thinking_levels = None
        thinking_probe.clear_cache(model_id)
        self._update_header()
        self._fetch_ctx_window()
        self._system(f"Switched to: {self.model_id}")
        self._apply_context_length()
        if self.backend.name == "lmstudio":
            self._probe_thinking()

    # ⚠️ `_on_` IS NOT A "HIDDEN FROM TEXTUAL" PREFIX. MessagePump dispatch does
    # `cls.__dict__.get(f"_{method_name}") or cls.__dict__.get(method_name)` —
    # the UNDERSCORED name is the one it looks for FIRST. So the private spelling
    # was never the framework-invisible one, and this rename moves the callback
    # to the SECOND lookup slot, not into the pump. Neither name collides: no
    # Message in this app or in Textual produces the handler name
    # `on_model_picked` (the only local Message subclass is `ticker.Changed`).
    _on_model_picked = on_model_picked               # arrival alias (PLAN §2b)

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
        # 🔴 THE GUARD IS "A CALL IS IN FLIGHT", NEVER CHILD LIVENESS. This
        # used to also require `proc.poll() is None` — and that clause was the
        # bug, not a guard against it: with shell=True the direct child
        # (cmd.exe) can exit while a grandchild it spawned still holds the
        # stdout/stderr pipes. communicate() then stays blocked, the turn is
        # stuck, and cancel answered "No cancellable tool is running" about a
        # tool that WAS running — with the button above even visible for it,
        # because the ticker keys on the slot, not poll(). The slot IS the
        # in-flight signal: _run_shell populates it at spawn and clears it only
        # once the call has ended (including before its timeout result formats),
        # so a populated slot means communicate() may still be blocked and
        # there is something to kill.
        if proc is None:
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

    def _force_stop(self) -> None:
        """The hard kill, shared by the keyboard and the wire (T632).

        Extracted so `stop_turn_over_rpc` cannot drift from the second Escape.
        """
        self.workers.cancel_group(self, "chat")
        self._system("[force-stopped — no partial reply was recoverable]")
        self._stop_requested = False
        # Marked HERE as well as at _stream's stop branch: a cancelled
        # worker never reaches that branch, so without this the HARDER of
        # the two stops would be the one the wake ping ignored.
        self._turn_abandoned = True

    def action_stop_turn(self) -> None:
        if not self._chat_running():
            # Escape with nothing running should be inert, not a dialog.
            return
        if self._stop_requested:
            # Already asked nicely. A model that has not produced a chunk since
            # cannot see the flag, so the second escape is the hard kill.
            self._force_stop()
            return
        # Sidebar or modal, decided by the setting. With dialog_style at its
        # default this is still literally `push_screen(ConfirmStop(), cb)`.
        present_dialog(self, ConfirmStopBody, ConfirmStop, self._on_stop_answer)

    def stop_turn_over_rpc(self) -> bool:
        """Stop the turn for a headless host. True when there was one to stop.

        🔴 THE WIRE MUST NOT BORROW THE KEYBOARD'S VERB, AND IT WAS (T632).
        `abort` called `action_stop_turn`, whose first stop is a ConfirmStop
        dialog. T572's `refuse_over_rpc` correctly declines to open a keyboard
        dialog in a headless child — so the confirmation never happened,
        `_stop_requested` was never set, and nothing stopped. The SECOND abort
        could not help either: its hard-kill branch is gated on the very flag
        the first one failed to set, so `abort` had no path to stopping
        anything at all, with or without a question open.

        ⚠️ THE GUARD WAS NOT THE BUG. It turned a hang into a no-op, which is
        the better failure. What was missing is that no entrance was ever built
        for the caller that cannot answer a dialog — the confirmation is the
        keyboard's step, not the decision.
            A REFUSAL THAT PROTECTS THE CALLER STILL OWES IT ANOTHER DOOR.

        Escalates exactly as Escape does: first call asks, second forces.
        """
        if not self._chat_running():
            return False
        if self._stop_requested:
            self._force_stop()
        else:
            self._stop_requested = True
        return True

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

        b64 = appsvc.load_image_file(self, path)
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

        try:
            self.query_one("#message-input", PromptInput).push_history(text)
        except Exception:
            pass

        if text.startswith("/"):
            self._handle_command(text)
            return

        # Check if input names an image file (optionally followed by a prompt)
        image_b64 = self.pending_image
        if text and not image_b64:
            path, rest = self._split_image_path(text)
            if path is not None:
                image_b64 = appsvc.load_image_file(self, path)
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

    def _headless_model_decision(self) -> tuple[str, str | None, str]:
        """What a `--rpc` child may do about the model, without loading one.

        🔴 LM STUDIO JIT-LOADS WHATEVER A CHAT REQUEST NAMES, and that is a
        DELIBERATE convenience for a person at a keyboard — `_chat_ready_sync`
        says so and names D2/D11. Headless is the opposite case: a consult
        panel spawns these children, nobody is present, and a cold model id
        silently takes ~18 GB of a GPU someone else is using. Measured
        2026-09-10: six probe children put a second 27B beside the one Ryan
        was running, and nothing in the RPC output said a load had happened —
        `ready` looks identical either way.

        Returns (action, model, message):
          ("ok",        id,   "")   the selection is resident; proceed
          ("substitute", id,  why)  ANOTHER is resident; use it, say which
          ("refuse",    None, why)  NOTHING is resident; answer nothing

        🔴 T642 — "SEVERAL LOADED" IS A SUBSTITUTE, NOT A REFUSAL, AND THAT
        REVERSES WHAT THIS RETURNED. It used to refuse unless EXACTLY ONE other
        model was resident, reasoning that choosing among several is "picking one
        on the user's behalf". A tester's fresh thread then refused its first
        prompt with TWO models sitting in VRAM, which inverts Ryan's standing
        rule: "when a model is already loaded, USE THAT ONE." The hazard that
        reasoning named is real, so the choice is a STATED tie-break in
        `model_residency.substitute_main_model` plus a note saying what was used
        — not silence. Refuse now means one thing: nothing at all is loaded.

        ⚠️ READ-ONLY, like the seam it guards. A question about state must
        not change it — the same law `_ensure_chat_ready` is written under.
        """
        ask = getattr(self.backend, "loaded_models", None)
        if ask is None:
            # A backend with no opinion (llama.cpp will not JIT-load at all,
            # by law; doubles predate the seam). Never AttributeError mid-turn.
            return ("ok", self.model_id, "")
        try:
            resident = [m for m in ask() if m]
        except Exception as exc:  # noqa: BLE001 - ANY failure to read the
            # loaded set means we cannot prove a request is safe, so refuse.
            why = (
                f"cannot tell which models are loaded ({exc}); refusing rather "
                f"than sending a request that might load one"
            )
            return ("refuse", None, why)
        want = self.model_id
        if want and want in resident:
            return ("ok", want, "")

        settings = getattr(self, "settings", None)
        chosen = model_residency.substitute_main_model(
            set(resident),
            want=want,
            subagent_model=getattr(settings, "subagent_model", None),
            tool_summary_model=getattr(settings, "tool_summary_model", None),
            default_model=getattr(settings, "default_model", None),
        )
        names = ", ".join(sorted(resident)) or "(none)"
        if chosen is not None:
            why = (
                f"asked for {want!r}, not loaded; using {chosen!r}. "
                f"Loaded: {names}."
            )
            return ("substitute", chosen, why)
        why = (
            f"asked for {want!r}; loaded: {names}. A headless child does not "
            f"load models — load one in LM Studio, or pass --model naming one "
            f"that is loaded."
        )
        return ("refuse", None, why)


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
        # T594: the headless gate runs FIRST, because refusing has to happen
        # before anything that could name a cold id reaches LM Studio.
        if getattr(self, "_rpc", False):   # doubles predate this seam
            action, model, why = self._headless_model_decision()
            if action == "refuse":
                self._rpc_emit({"type": "error", "kind": "model_not_loaded",
                                "message": why})
                raise llm_backend.BackendError(f"model not loaded — {why}")
            if action == "substitute" and model:
                self.model_id = model
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
        # Cleared with the flag it explains. A reason that outlived its turn
        # would attribute THIS turn's ending to the last turn's cause.
        self._stop_reason = None
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
        self._rpc_emit({"type": "turn_start", "model": self.model_id, "thinking_level": self.thinking_level})
        compact_due = False
        stopped_early = False
        for _iteration in range(self.settings.tool_iterations):
            if self._stop_requested:
                # NOT the iteration cap. Reaching the bottom of this function
                # prints "reached N tool iterations — raise it in /settings",
                # so an early break that falls through explains itself with a
                # limit that was never hit and sends the user to change a
                # setting that had nothing to do with it.
                stopped_early = True
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
            self._elapsed.start(widget.body)
            thinking: ThinkingBlock | None = None
            text_full = ""
            reasoning = ""
            tool_acc: dict[int, dict] = {}
            provider_metadata = None
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
                tools=self._all_tools(),  # advertised even when OFF — see turn_engine
                backend_name=self.backend.name,
                graded_thinking_models=self.settings.lmstudio_graded_thinking_models,
            )

            self._tps.start()
            try:
                # ASK BEFORE OPENING THE STREAM. Inside this try on purpose:
                # the plain words then land in the same widget that used to
                # show the raw 400, with no second error path to keep in step.
                # No timeout here — the user is watching and asked for this
                # turn, so a model that is loading is worth waiting out.
                await self._ensure_chat_ready()
                stream = await model_transport.for_app(self).create(**kwargs)
            except Exception as e:
                runtime_log.record(
                    "turn_stream_failed",
                    site="app.stream.open",
                    component="backend",
                    operation="stream",
                    error_type=type(e).__name__,
                )
                runtime_log.record_error(
                    "turn_stream_failed",
                    detail=f"{type(e).__name__}: {e}",
                    exc=e,
                )
                self._elapsed.stop_body()
                self._thinking_done()
                widget.body.content = Text(_plain_backend_error(e, self.backend.name), style="bold red")
                widget.border_title = "Error"
                self._scroll_down()
                self._rpc_emit({"type": "turn_end", "stopReason": "error", "error": _plain_backend_error(e, self.backend.name)})
                return

            try:
                async for chunk in stream:
                    provider_metadata = getattr(chunk, "provider_metadata", None) or provider_metadata
                    u = getattr(chunk, "usage", None)
                    if u is not None:
                        self._record_usage(u)
                    if u is not None and getattr(u, "total_tokens", None):
                        self.ctx_used = int(u.total_tokens)
                        rate = self._tps.final(
                            int(getattr(u, "completion_tokens", 0) or 0))
                        if rate is not None:
                            self.tps = rate
                        # ETA: this is the end of the turn -- the usage chunk
                        # carries prompt_tokens, so fold this turn into the
                        # learned rate (gated) and remember its count as the
                        # estimate for the next turn's ETA.
                        self._eta.learn(
                            getattr(u, "prompt_tokens", None),
                            self._elapsed.body_t0,
                            is_reliable_rate_sample,
                        )
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    self._eta.record_first_delta()
                    # LM Studio streams the thinking trace as `reasoning_content`
                    # (some other OpenAI-compatible servers use `reasoning`).
                    token = getattr(delta, "reasoning_content", None) or getattr(
                        delta, "reasoning", None
                    )
                    if token:
                        # TAGGED as reasoning, so TpsState's partition can tell
                        # the thinking header's number from the footer's. Both
                        # branches call the same tick; only the tag differs.
                        rate = self._tps.tick(reasoning=True)
                        if rate is not None:
                            self.tps = rate
                        self._glassbox_rate("thinking")
                        reasoning += token
                        self._rpc_emit({"type": "reasoning_delta", "text": token})
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
                        rate = self._tps.tick()
                        if rate is not None:
                            self.tps = rate
                        self._glassbox_rate("output")
                        self._thinking_done()
                        self._elapsed.stop_body()
                        text_full += delta.content
                        self._rpc_emit({"type": "text_delta", "text": delta.content})
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
                runtime_log.record(
                    "turn_stream_failed",
                    site="app.stream.iterate",
                    component="backend",
                    operation="stream",
                    error_type=type(e).__name__,
                )
                # Mid-turn close is the common case here: LM Studio dies while
                # streaming. Sink gets the raw exception, bubble gets plain words.
                runtime_log.record_error(
                    "turn_stream_failed",
                    detail=f"{type(e).__name__}: {e}",
                    exc=e,
                )
                self._elapsed.stop_body()
                self._thinking_done()
                widget.body.content = Text(_plain_backend_error(e, self.backend.name), style="bold red")
                widget.border_title = "Error"
                self._scroll_down()
                # T526: the open-failure branch above emits this; this branch
                # did not, so an rpc client (LiteSuite's LiteTuiAdapter) that
                # saw turn_start waited forever on a mid-stream 400.
                self._rpc_emit({"type": "turn_end", "stopReason": "error", "error": _plain_backend_error(e, self.backend.name)})
                return
            finally:
                if isinstance(stream, model_transport.ResponseStream):
                    await stream.close()

            self._elapsed.stop_body()
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
                if provider_metadata:
                    message["provider_metadata"] = provider_metadata
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
                self._rpc_emit({"type": "turn_end", "stopReason": "cancelled"})
                self.call_after_refresh(self._maybe_autocompact)
                return

            if not tool_acc:
                # Turn is over. Check the window AFTER this worker exits:
                # _compact shares group="chat" and would cancel us mid-frame.
                await self.plugins.finalize_turn()
                self._rpc_emit({"type": "turn_end", "stopReason": "stop"})
                self.call_after_refresh(self._resync_ctx_if_stale)
                self.call_after_refresh(self._maybe_autocompact)
                return  # plain answer — agent loop done

            # Execute each tool call, display the result, feed it back.
            for i, slot in sorted(tool_acc.items()):
                name = slot["name"]
                args_json = slot["arguments"] or "{}"
                tc_id = slot.get("id") or f"call_{i}"
                msg = tool_msgs.get(i)
                if self._stop_requested:
                    result, ok = "[cancelled] not executed — turn stopped", False
                else:
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
                # T219. The same argument the block above makes about escape
                # bytes applies to SECRETS, and this is the point that already
                # sees every tool result: on 2026-09-03 an env listing through
                # `bash` put LITESUITE_JWT_SECRET and OPENAI_API_KEY verbatim
                # into the transcript, the model's context and a screenshot.
                # AFTER the strip, so a value wearing escape bytes is matched as
                # the text it actually is. Before the display AND before the
                # model — the same string serves both, and a secret on screen is
                # a secret in the screenshot.
                result = sanitize.redact_secrets(result)
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

            if self._stop_requested:
                # Pair every call before stopping, even on the last allowed round.
                stopped_early = True
                break

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
            self._rpc_emit({"type": "turn_end", "stopReason": "cancelled"})
            self.call_after_refresh(self._maybe_autocompact)
            return

        if stopped_early:
            # The turn was STOPPED, not exhausted. _stop_reason names the
            # cause when there is one (denying a tool); a plain Esc during
            # tool execution has already notified the user, so the generic
            # line only has to avoid claiming a cap was reached.
            self._system(self._stop_reason or "[stopped by you]")
            self._turn_abandoned = True
            self._rpc_emit({"type": "turn_end", "stopReason": "cancelled"})
            return

        self._rpc_emit({"type": "turn_end", "stopReason": "tools_cap"})
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
                    # The floor when there is no settings object at all --
                    # same reasoning as the tool door above.
                    tool_policy.SCHEDULED),
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
                    # The floor when there is no settings object at all --
                    # same reasoning as the tool door above.
                    tool_policy.SCHEDULED),
        )
        self._materialise_convo()
        self._append({"role": "user", "content": item["content"]})
        _mark_delivered(item)
        self._stream()


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
        # CSS repaints itself; the footer is a pre-styled Rich Text, so rebuild
        # it on a theme switch too (including the palette's Change theme route).
        refresh_footer = getattr(self, "_refresh_ctx_label", None)
        if refresh_footer is not None:
            refresh_footer()
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

    def _record_usage(self, usage) -> None:
        """Last provider usage, numeric-only; cache hits do not reduce context."""
        self.last_usage = {
            key: model_transport.numeric_usage(getattr(usage, key, None))
            for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                        "cached_tokens", "cache_write_tokens", "input_tokens_details",
                        "usage_details")
        }

    @work(exclusive=True, group="chat")
    async def _compact(self, extra: str = "") -> None:
        # A new operation gets a fresh cancellation latch, not the prior turn's.
        # Keep _turn_abandoned: maintenance must not revive stopped user work.
        self._stop_requested = False
        self._stop_reason = None
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
        self._elapsed.ensure_running()
        self._scroll_down()

        ask = list(head) + [
            {"role": "user", "content": COMPACT_PROMPT + (f"\n\n{extra}" if extra else "")}
        ]
        if system:
            # Merge the live store in, so it can see what memory.md already
            # holds and update rather than duplicate.
            block = appsvc.store_block(self, live=True)
            ask.insert(0, {**system, "content": system.get("content", "") + block})

        # Tools are passed so STEP 1 of COMPACT_PROMPT can actually happen.
        # Without them the instruction to persist is theatre: the model narrates
        # writing files and nothing reaches disk.
        summary = ""
        writes: list[str] = []
        stream = None
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
                    request_overrides=self.backend.request_overrides(self.model_id),
                    tools_enabled=self.tools_enabled,
                    tools=self._all_tools(),  # advertised even when OFF — see turn_engine
                    backend_name=self.backend.name,
                    graded_thinking_models=self.settings.lmstudio_graded_thinking_models,
                )

                stream = await model_transport.for_app(self).create(**kwargs)
                provider_metadata = None
                text_full = ""
                tool_acc: dict = {}
                tool_msgs: dict = {}
                # Twin of the delta assembly in _stream, deliberately: the
                # compaction card speaks the same grammar as a normal turn
                # because it reuses the same chunk shapes and widgets.
                async for chunk in stream:
                    provider_metadata = getattr(chunk, "provider_metadata", None) or provider_metadata
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
                    **({"provider_metadata": provider_metadata} if provider_metadata else {}),
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
                    if self._stop_requested:
                        result, ok = "[cancelled] not executed — turn stopped", False
                    else:
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
                if self._stop_requested:
                    self._turn_abandoned = True
                    card.fail("stopped — conversation unchanged")
                    self._system(self._stop_reason or "[stopped by you]")
                    return
        except Exception as e:
            self._autocompact_failed_at = self.ctx_used
            # The repr goes to the sink; card and chat get plain words. A
            # BackendError's message IS already plain words (cleaned at source)
            # — that is the WHY ('m1' is not loaded — /load m1 first...), and a
            # compaction that refuses without saying why reads as a hang.
            runtime_log.record_error(
                "compact_failed",
                detail=f"{type(e).__name__}: {e}",
                exc=e,
            )
            card.fail("failed \u2014 conversation unchanged")
            self._system(f"Compact failed — conversation unchanged.\n{_plain_backend_error(e, self.backend.name)}")
            return
        finally:
            if isinstance(stream, model_transport.ResponseStream):
                await stream.close()

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
        elif old.custom_themes.get(new.theme_name) != new.custom_themes.get(new.theme_name):
            # Editing the active custom theme changes its variables, not its
            # name. Re-run Textual's CSS watcher or its background stays stale.
            self.mutate_reactive(App.theme)
        try:
            path = settings_mod.save(new)
        except OSError as e:
            # The raw OS error (WinError + path) goes to the sink; chat gets
            # what happened, in plain words.
            runtime_log.record_error(
                "settings_save_failed",
                detail=f"{type(e).__name__}: {e}",
                exc=e,
            )
            self._system("Settings applied for this session but NOT saved — the settings file could not be written.")
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
            runtime_log.record_error(
                "mark_handoff_unreadable",
                detail=f"{handoff}: {type(e).__name__}: {e}",
                exc=e,
            )
            self._system("/mark: the handoff file could not be read.")
            return
        if data.get("cancelled"):
            self._system("/mark: cancelled.")
            return
        b64 = appsvc.load_image_file(self, Path(data["png"]))
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


def wants_ansi_fallback() -> bool:
    """True on a legacy Windows console (plain conhost) that cannot take
    truecolor VT output — there the theme's darks quantize into bright 16-color
    bands around the composer and header. ``ansi_color=True`` makes Textual
    emit the console's own ANSI palette instead, which renders coherently.

    Windows Terminal, ConPTY panes, and any console where VT+truecolor probing
    succeeds return False, so nothing changes on a modern terminal. Measured
    2026-09-01: a directly spawned conhost on Win11 26200 reports vt=False
    truecolor=False (rich get_windows_console_features), and litetui 0.22.1
    rendered the broken scheme there (sandbox 0057 guest cmd, Ryan's sighting).
    """
    if sys.platform != "win32" or os.environ.get("WT_SESSION"):
        return False
    try:
        from rich._windows import get_windows_console_features

        features = get_windows_console_features()
        return not (features.vt and features.truecolor)
    except Exception:
        return False


def main():
    LiteTUI(ansi_color=wants_ansi_fallback()).run()


if __name__ == "__main__":
    main()
