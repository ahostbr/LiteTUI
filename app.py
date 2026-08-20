"""LiteTUI — a terminal chat client and agent harness for a local LM Studio model."""

import asyncio
import base64
import io
import json
import os
import re
import subprocess
import time
import urllib.request
import uuid
from html.parser import HTMLParser
from pathlib import Path

import harness as harness_mod
from dataclasses import fields as fields_of

import config
import settings as settings_mod
from settings import Settings, sampling_kwargs
from settings_screen import SettingsScreen
import ask_user_question
import chrome_tool
import pccontrol_tool
import ttyguard
import mcp_client
import sanitize
import skills as skills_mod

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import WorkerState
from textual import work, on
from openai import AsyncOpenAI
from rich.markdown import Markdown
from rich.text import Text


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_DIM = 1536
ROOT = Path(__file__).parent
SYSTEM_PROMPT_FILE = Path(__file__).parent / "systemprompt.md"

# ── Conversation persistence ─────────────────────────────────────
# .convos/<uuid>/
#     convo.jsonl   append-only transcript
#     memory.md     INDEX the agent maintains — one line per memory
#     soul.md       who this agent is; persists across resumes
#     handoff.md    what is in flight, for whoever picks this up
#     memories/     the actual notes: i-learned-this.md, uncapped
CONVO_DIR = Path(__file__).parent / ".convos"
#: Marks an already-injected store block inside the system message. Detection
#: by MARKER rather than a flag is what makes /resume correct: a flag lives in
#: memory and dies with the process; the marker is persisted with the message.
STORE_HEADER = "## Your store, loaded once at the start of this conversation"
TRANSCRIPT_NAME = "convo.jsonl"
MEMORIES_DIR = "memories"

CONVO_SEED_FILES = {
    "memory.md": (
        "# Memory Index\n\n"
        "One line per memory, NEWEST AT THE TOP. Bodies live in "
        f"`{MEMORIES_DIR}/`.\n\n"
        "`- [short title](memories/slug.md) — the hook`\n\n"
        "POINTERS ONLY. ~50 tokens (about 200 chars) per line, hard. Enough to\n"
        "decide whether to open the file, nothing more. If you are explaining\n"
        "the thing here, it belongs in the topic file instead.\n\n"
        "This file is injected into the system prompt ONCE, at the start of the\n"
        "conversation, so a long line permanently crowds out other entries.\n\n"
        "Append and edit only — never rewrite it to make it shorter. A line\n"
        "removed here orphans a file that nothing will ever open again.\n\n"
        "---\n\n"
    ),
    "soul.md": (
        "# Soul\n\n"
        "Who I am in this conversation. I write this for myself; it survives\n"
        "/resume and /compact when the transcript does not.\n\n"
        "## How the user works\n"
        "_Preferences, tone, what they want more or less of._\n\n"
        "## Standing corrections\n"
        "_Things I got wrong and was corrected on. The correction, and WHY —\n"
        "a rule without its reason gets re-litigated or misapplied._\n\n"
        "## How I work here\n"
        "_Habits that have proven useful in this conversation specifically._\n"
    ),
    "handoff.md": (
        "# Handoff\n\n"
        "Written so the next session can ACT without re-deriving anything.\n\n"
        "> Every row must be CHECKABLE: name the file, the command, or the\n"
        "> identifier. A query can be re-run; a bare claim can only be believed.\n"
        "> State when each row was last MEASURED — not when it was assumed.\n\n"
        "## 1. In flight\n"
        "_What is running or half-done right now. 'Nothing' is a valid and\n"
        "useful answer — say it explicitly rather than leaving the section out._\n\n"
        "## 2. Owed — split by owner\n"
        "_Mine / theirs / the user's. An unowned item is one nobody does._\n\n"
        "## 3. Absent by decision\n"
        "_What is deliberately NOT being done, and what defends that choice.\n"
        "Without this, the next session rediscovers it and redoes it._\n\n"
        "## 4. Caveats riding the green lines\n"
        "_What 'it works' does NOT cover. The limits of every pass claim._\n\n"
        "## 5. My corrections and retractions\n"
        "_What I claimed and later found wrong. Carry these forward: a\n"
        "retracted claim that is not written down comes back as fact._\n"
    ),
}

# LM Studio's accepted set, read out of its own 400 body rather than the docs
# (which omit reasoning_effort entirely):
#   Invalid 'reasoning_effort' value: 'x'. Supported values: none, minimal,
#   low, medium, high, xhigh.
# "off" is this app's name for the server's "none" so the wording matches the
# rest of the UI; everything else passes through unchanged.
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")

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

COMPACT_PROMPT = (
    "You are about to lose this conversation. The messages below are being "
    "REPLACED by whatever you produce now, so anything you do not carry across "
    "is gone.\n\n"
    "Do TWO things, in this order.\n\n"
    "STEP 1 — PERSIST, using your write tool, before you summarise.\n"
    "This is the moment the store exists for. Right now, while you still have "
    "the full conversation, decide what outlives it:\n"
    "  • A durable lesson — a root cause, a reusable pattern, a decision and "
    "its reason? Write the body to a NEW file in your memories/ folder and add "
    "ONE pointer line to memory.md. The pointer is ~50 tokens, never the memory "
    "itself.\n"
    "  • Learned something about how the user works, or been corrected? Update "
    "soul.md — the correction AND the reason for it.\n"
    "  • Anything in flight, owed, or deliberately not being done? Rewrite "
    "handoff.md so the next session can act without re-deriving it.\n"
    "Skip any of these that genuinely do not apply. Do not invent a lesson to "
    "have something to write — an honest 'nothing durable happened' is correct "
    "and common. But if something IS durable, this is your last chance.\n\n"
    "STEP 2 — then reply with the summary itself, as plain text.\n"
    "Cover: what the user is trying to achieve; decisions made and the reasons; "
    "files, paths, commands and identifiers that came up; what has been tried "
    "and what the result was; and anything still open or owed.\n"
    "Be specific — names, numbers and paths, not 'we discussed the config'. "
    "Write it as notes to yourself, not as a report to the user.\n"
    "Do not describe what you wrote to the store; the summary stands on its own."
)

def memory_prompt(convo_id: str, folder: Path) -> str:
    """The block appended to systemprompt.md so the agent can find its own store.

    A store the agent cannot name is a store it will never open. This is the
    dereference: the path arrives in the system prompt every turn, not in a
    file the agent would have to already know about in order to look up.
    """
    p = str(folder).replace("\\", "/")
    return f"""

## Your conversation store

You are conversation `{convo_id}`. Your own directory is:

    {p}

It already exists and holds four things. Use your read/write/bash tools on them
by absolute path.

- `{p}/memory.md` — an INDEX you maintain. One line per memory, newest at the
  top, each pointing at a file in `{MEMORIES_DIR}/`.

  🔴 EVERY INDEX LINE IS A POINTER, NEVER THE MEMORY ITSELF. Hard limit: ~50
  tokens (about 200 characters) per line — a title, a link, and a hook just
  long enough to decide whether to open the file. If you find yourself
  explaining the thing in the index, you are writing it in the wrong file:
  put it in `{MEMORIES_DIR}/` and leave one line here.

  This whole file is injected into every prompt. A bloated index costs you on
  every single turn AND pushes older entries out of view, so a long line does
  not merely waste space — it evicts other memories.
- `{p}/{MEMORIES_DIR}/` — the memories themselves, one file per idea, e.g.
  `i-learned-this.md`. Uncapped. Write the durable thing here and add its one
  line to memory.md.
- `{p}/soul.md` — who you are here: how this user works, corrections you were
  given and why, habits that proved useful. Update it when you learn something
  about working WITH them rather than about the task.
- `{p}/handoff.md` — what is in flight, what is owed and by whom, what is
  deliberately not being done, the caveats on your green claims, and your own
  retractions. Written so the next session can act without re-deriving.

Rules that make this worth doing:

1. THE THREE FILES BELOW WERE INJECTED ONCE, AT THE START OF THIS
   CONVERSATION — they are a SNAPSHOT, not a live view, and they are not
   re-sent each turn. If you have written to any of them since, or you need
   their current contents, READ THEM WITH THE `read` TOOL. DO open a file in
   `{MEMORIES_DIR}/` when an index line suggests it holds what you need;
   those are never injected.
2. WRITE THE DURABLE THING ONLY — a decision, a root cause, a reusable
   pattern, a preference. Not what just happened; the transcript has that.
3. APPEND AND EDIT, NEVER COMPACT. Do not rewrite memory.md to shorten it.
   Deleting an index line orphans a file nothing will open again.
4. WRITE IT DOWN WHEN YOU GET CORRECTED, including the reason. A rule without
   its reason gets misapplied later.
5. BEFORE WRITING A NEW MEMORY, check whether one already covers it. Update
   that file rather than adding a near-duplicate.
"""


# ════════════════════════════════════════════════════════════════
# Agent tools (pi-style): bash, read, write, web_fetch
# Limits mirror pi's defaults (2000 lines / 50KB).
# ════════════════════════════════════════════════════════════════

TOOL_MAX_LINES = 2000
TOOL_MAX_BYTES = 50 * 1024  # 50KB
# Fallback only. The live value is self.settings.tool_iterations, editable in
# /settings; env LM_TOOL_ITERS still wins over both (see settings.ENV_OVERRIDES).
# This constant remains so module-level users and tests keep a sane number.
TOOL_MAX_ITERATIONS = int(os.environ.get("LM_TOOL_ITERS", "48"))
WEB_FETCH_MAX_CHARS = 20_000
WEB_FETCH_TIMEOUT_S = 20
BASH_DEFAULT_TIMEOUT_S = 120

TOOLS_PROMPT = """
You have four tools: bash, read, write, web_fetch.
- bash: run a shell command (ls, dir, grep, find, git, python, ...). Returns stdout+stderr, truncated to the last 2000 lines / 50KB. Non-zero exits are reported.
- read: read a text file by path; use offset (1-indexed) / limit for large files; capped at 2000 lines / 50KB, continue with offset.
- write: create or fully overwrite a file (parent dirs are created).
- web_fetch: fetch an http(s) URL and get its content as plain text (max 20000 chars).
Use tools whenever they help fulfil the user's request. Inspect tool output before answering. If a call fails, read the error and adapt.
"""


def _truncate_tail(text: str, max_lines: int = TOOL_MAX_LINES, max_bytes: int = TOOL_MAX_BYTES) -> str:
    lines = text.split("\n")
    if len(lines) > max_lines:
        text = "\n".join(lines[-max_lines:])
    b = text.encode("utf-8", errors="replace")
    if len(b) > max_bytes:
        text = b[-max_bytes:].decode("utf-8", errors="replace")
    return text


def _truncate_head(text: str, max_lines: int = TOOL_MAX_LINES, max_bytes: int = TOOL_MAX_BYTES) -> tuple[str, bool]:
    truncated = False
    lines = text.split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    text = "\n".join(lines)
    b = text.encode("utf-8", errors="replace")
    if len(b) > max_bytes:
        text = b[:max_bytes].decode("utf-8", errors="replace")
        truncated = True
    return text, truncated


def _resolve_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def tool_bash(args: dict) -> str:
    command = (args.get("command") or "").strip()
    if not command:
        return "[error] missing 'command'"
    try:
        timeout = int(args.get("timeout") or BASH_DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = BASH_DEFAULT_TIMEOUT_S
    try:
        # Through the envelope, which owns stdin=DEVNULL, errors="replace",
        # CREATE_NO_WINDOW and the terminal repair. Those used to be spelled out
        # here, per site — which is a convention, and one copy-paste away from
        # being lost. errors="replace" in particular is not cosmetic: under
        # strict decoding one byte undecodable in the active locale kills
        # subprocess's reader THREAD, the traceback goes to stderr, and this
        # call returns "(no output)" while the child really did produce output.
        proc = ttyguard.run(
            command,
            shell=True,
            timeout=timeout,
            cwd=str(Path.cwd()),
        )
    except subprocess.TimeoutExpired as e:
        partial = _truncate_tail((e.stdout or "") + (e.stderr or ""))
        return f"[timed out after {timeout}s]\n{partial}".strip()
    out = proc.stdout or ""
    err = proc.stderr or ""
    if err:
        out = (out + "\n[stderr]\n" + err) if out else "[stderr]\n" + err
    out = _truncate_tail(out)
    if proc.returncode != 0:
        suffix = f"\n\n[command exited with code {proc.returncode}]"
        out = (out + suffix) if out else suffix.strip()
    return out or "(no output)"


def tool_read(args: dict) -> str:
    raw = args.get("path") or ""
    if not raw:
        return "[error] missing 'path'"
    p = _resolve_path(raw)
    if not p.exists():
        return f"[error] file not found: {p}"
    if p.is_dir():
        return f"[error] {p} is a directory — use bash (ls/dir) to list it"
    data = p.read_bytes()
    if _is_binary(data):
        return f"[binary file: {p.name}, {len(data)} bytes — use bash to inspect]"
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    total = len(lines)
    try:
        offset = max(1, int(args.get("offset") or 1))
    except (TypeError, ValueError):
        offset = 1
    if offset > total:
        return f"[offset {offset} is beyond end of file ({total} lines total)]"
    start = offset - 1
    end = total
    if args.get("limit") is not None:
        try:
            end = min(start + max(1, int(args["limit"])), total)
        except (TypeError, ValueError):
            pass
    chunk = "\n".join(lines[start:end])
    chunk, truncated = _truncate_head(chunk)
    note = ""
    if truncated:
        note = f"\n\n[truncated at {TOOL_MAX_LINES} lines / {TOOL_MAX_BYTES // 1024}KB. Use offset={end + 1} to continue.]"
    elif end < total:
        note = f"\n\n[{total - end} more lines in file. Use offset={end + 1} to continue.]"
    return chunk + note


def tool_write(args: dict) -> str:
    raw = args.get("path") or ""
    content = args.get("content")
    if not raw:
        return "[error] missing 'path'"
    if content is None:
        return "[error] missing 'content'"
    p = _resolve_path(raw)
    try:
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as e:
        return f"[error] write failed: {type(e).__name__}: {e}"
    return f"Wrote {len(content)} chars to {p}"


class _HTMLTextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "head"}
    BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "tr", "section", "article", "pre", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data) -> None:
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    ex = _HTMLTextExtractor()
    try:
        ex.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    text = "".join(ex.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def tool_web_fetch(args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return "[error] 'url' must start with http:// or https://"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; LiteTUI)"}
        )
        with urllib.request.urlopen(req, timeout=WEB_FETCH_TIMEOUT_S) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            data = r.read(512 * 1024)
    except Exception as e:
        return f"[error] fetch failed: {type(e).__name__}: {e}"
    text = data.decode("utf-8", errors="replace")
    if "html" in ctype or text.lstrip()[:1] == "<":
        text = _html_to_text(text)
    if len(text) > WEB_FETCH_MAX_CHARS:
        text = text[:WEB_FETCH_MAX_CHARS] + f"\n\n[truncated at {WEB_FETCH_MAX_CHARS} chars]"
    return text.strip() or "[empty response]"


TOOL_DISPATCH: dict[str, object] = {
    "bash": tool_bash,
    "read": tool_read,
    "write": tool_write,
    "web_fetch": tool_web_fetch,
}

# The model CANNOT see an image through a tool result. A tool result is a
# role:"tool" message whose content is a STRING; images are only visible as an
# image_url block on a role:"user" message. So this tool does not return the
# picture -- it stages it, and the tool loop injects a user turn carrying the
# image through the same door the paste path uses. Returning base64 here would
# burn a megabyte of context to show the model nothing.
VIEW_IMAGE_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "view_image",
        "description": (
            "Look at an image file on disk. Give an absolute path. The image is "
            "attached to the conversation and you will see it in the next message "
            "-- the result of this call is only a confirmation, not the picture."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to a png/jpg/gif/webp/bmp file",
                }
            },
            "required": ["path"],
        },
    },
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command in the current working directory "
                "(bash on Unix, cmd.exe on Windows). Returns stdout and stderr, "
                "truncated to the last 2000 lines or 50KB. Non-zero exit codes are reported. "
                "Optionally provide a timeout in seconds (default 120)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "number", "description": "Timeout in seconds (default 120)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": (
                "Read the contents of a text file (relative or absolute path). "
                "Output is truncated to 2000 lines or 50KB (whichever is hit first). "
                "Use offset/limit for large files and continue with offset when truncated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to read (relative or absolute)"},
                    "offset": {"type": "number", "description": "Line number to start reading from (1-indexed)"},
                    "limit": {"type": "number", "description": "Maximum number of lines to read"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": (
                "Write content to a file, creating parent directories as needed. "
                "Overwrites the file if it exists. Use for new files or full rewrites."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to write (relative or absolute)"},
                    "content": {"type": "string", "description": "Content to write to the file"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a URL over HTTP(S) and return its body as plain text "
                "(HTML converted to text, capped at 20000 chars). Use for web pages, docs, and JSON APIs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute http(s) URL to fetch"},
                },
                "required": ["url"],
            },
        },
    },
]


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


class AssistantMessage(Vertical):
    """Assistant bubble — optional thinking block above the answer body."""

    def __init__(self) -> None:
        super().__init__(classes="assistant-msg")
        self.thinking: ThinkingBlock | None = None
        self.body = Static("...", id="answer-body")

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

    def set_args(self, args_json: str) -> None:
        self._args = args_json
        self._update_display()

    def set_result(self, result: str, ok: bool) -> None:
        self._result = result
        self._ok = ok
        self._update_display()

    # NOTE: must NOT be named `_render` — Textual's Widget._render() is an
    # internal method that must return a Visual; shadowing it breaks layout.
    def _update_display(self) -> None:
        arg_line = self._args.replace("\n", " ")
        if len(arg_line) > 110:
            arg_line = arg_line[:107] + "..."
        parts: list[tuple[str, str]] = [(f"\U0001F527 {self.tool_name}", "bold #e8a33d")]
        if arg_line:
            parts.append(("  " + arg_line, "#8b95a7"))
        if self._result is not None:
            lines = self._result.split("\n")
            shown = "\n".join(lines[: self.MAX_DISPLAY_LINES])
            if len(lines) > self.MAX_DISPLAY_LINES:
                shown += f"\n\u2026 ({len(lines) - self.MAX_DISPLAY_LINES} more lines, {len(self._result)} chars total)"
            style = "bold #e5534b" if not self._ok else "#7d8799"
            parts.append(("\n" + shown, style))
        self.content = Text.assemble(*parts)


class PickerScreen(ModalScreen[str | None]):
    """A clickable list modal. Returns the chosen option's id, or None.

    Shared by /model and /resume so the two never drift into different
    interactions — the pattern is identical, only the rows differ.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, rows: list[tuple[str, str]], current: str | None = None,
                 hint: str = "↑↓ move · Enter or click to select · Esc to cancel"):
        super().__init__()
        self._title = title
        self._rows = rows          # (id, label)
        self._current = current
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static(self._title, id="picker-title")
            yield OptionList(
                *[Option(label, id=oid) for oid, label in self._rows], id="picker-list"
            )
            yield Static(self._hint, id="picker-hint")

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        if self._current is not None:
            ids = [oid for oid, _ in self._rows]
            if self._current in ids:
                ol.highlighted = ids.index(self._current)
        ol.focus()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


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


class LiteTUI(App):
    """TUI chat client for LM Studio."""

    TITLE = "LiteTUI"
    SUB_TITLE = "Connecting..."

    CSS = """
    Screen {
        background: $surface-darken-1;
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
        border: dashed $warning-darken-2;
    }

    .thinking-header {
        color: $warning;
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
        color: $text-muted;
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
    ConfirmStop, PickerScreen, HelpScreen, SettingsScreen {
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
        self.convo_id: str = ""
        self.convo_dir: Path | None = None
        self.convo_path: Path | None = None  # <convo_dir>/convo.jsonl
        self._convo_loading = False  # suppress writes while replaying from disk
        self._stop_requested = False  # Esc-to-stop, checked inside the stream loop
        self._persist_error: str | None = None
        self._store_injected = False  # see STORE_HEADER; once per conversation
        self.client = AsyncOpenAI(
            # Settings first, then config's env/default. config.BASE_URL is
            # computed at IMPORT time, so reading it here would pin the client to
            # the environment and silently ignore a host set in /settings.
            base_url=f"{self.settings.lm_host.rstrip('/')}/v1",
            api_key="lm-studio",
        )
        # Discovered ONCE, before the first system prompt is built -- the skill
        # index rides in that prompt, so discovering later would ship a prompt
        # that omits every skill for the first turn.
        # Honour the setting at DISCOVERY, not at use: an empty index means the
        # skill tool is never offered and the index block never enters the system
        # prompt, which is what "off" has to mean for a context-costing feature.
        self.skills = skills_mod.discover(ROOT) if self.settings.skills_enabled else {}
        self.mcp = mcp_client.MCPManager(ROOT)
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
            agent_id=harness_mod.new_agent_id(), name="LiteTUI", model="",
        )
        self._seat_started = False
        # The question widget must reach the RUNNING app instance to
        # turn can ever call the tool.
        ask_user_question.set_app(self)
        self._new_convo()
        self._load_system_prompt()

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat-log")
        yield Static(
            "  Image attached — Ctrl+X to remove", id="image-indicator"
        )
        yield Input(
            placeholder="Message... (Ctrl+V paste | Ctrl+O image | /help)",
            id="message-input",
        )
        yield ContextFooter()

    def on_mount(self) -> None:
        self.query_one("#message-input", Input).focus()
        self._connect()
        self._inbox_monitor()

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
            self._append_to_system((
                    f"You are registered in the LiteHarness fleet as "
                    f"{self.seat.name} (id {self.seat.agent_id}, tier {self.seat.tier}). "
                    "Other agents can message you and their mail arrives as a user turn "
                    "prefixed [inbox from <id>]. Use the `harness` tool to answer: "
                    "action=discover to see who is online, action=send with `to` and "
                    "`body` to reply. Reply to the SENDER id from the [inbox from ...] "
                    "line, never to your own id."
                ))
        else:
            # Say so once. A seat nobody can reach that reports nothing is
            # indistinguishable from one that is simply idle.
            self._system(f"harness seat OFFLINE ({self.seat.error or 'unknown'})")
            return
        try:
            while True:
                await asyncio.sleep(harness_mod.POLL_SECONDS)
                msgs = await asyncio.to_thread(self.seat.poll)
                for m in msgs:
                    self._deliver_inbox(m)
        except asyncio.CancelledError:
            raise

    def _deliver_inbox(self, msg: dict) -> None:
        """Show the message, then WAKE the agent with it as a user turn.

        Queued rather than dropped when a turn is already running: mail that
        arrives mid-turn is exactly the mail worth not losing.
        """
        text = harness_mod.format_message(msg)
        self._user_bubble(text, False)
        self._append({"role": "user", "content": text})
        if not self._chat_running():
            self._stream()

    def _all_tools(self) -> list[dict]:
        """Static tools + the `skill` tool + every MCP tool, as OpenAI specs."""
        specs = list(TOOLS)
        # Offered unless we KNOW the model cannot see (type "llm"). Unknown
        # stays offered: the tool reports the precondition itself, which is
        # more useful than the tool silently not existing.
        if self.model_type != "llm":
            specs.append(VIEW_IMAGE_TOOL_SPEC)
        if pccontrol_tool.SCRIPT.exists():
            specs.append(pccontrol_tool.PCCONTROL_TOOL_SPEC)
        if chrome_tool.SCRIPT.exists():
            specs.append(chrome_tool.CHROME_TOOL_SPEC)
        # Always available: it renders inside this very app and has
        # no external precondition (unlike the browser tools' SCRIPT check).
        specs.append(ask_user_question.ASK_USER_QUESTION_TOOL_SPEC)
        if self.skills:
            specs.append(skills_mod.SKILL_TOOL_SPEC)
        # Only offered once the seat is actually registered. Advertising fleet
        # verbs to an agent with no return address produces confident sends
        # that go nowhere.
        if self.seat.registered:
            specs.append(harness_mod.HARNESS_TOOL_SPEC)
        specs.extend(self.mcp.tool_specs())
        return specs

    def _dispatch_for(self, name: str):
        """Resolve a tool name across all three sources, static first."""
        fn = TOOL_DISPATCH.get(name)
        if fn is not None:
            return fn
        if name == "skill":
            return lambda args: skills_mod.load(self.skills, args.get("name", ""))
        if name == "harness":
            return lambda args: harness_mod.run(self.seat, args)
        if name == "view_image":
            return self._tool_view_image
        if name == "pccontrol":
            return pccontrol_tool.run
        if name == "chrome":
            return chrome_tool.run
        if name == "ask_user_question":
            return ask_user_question.run
        return self._mcp_dispatch.get(name)

    def _system_prompt_text(self) -> str:
        """systemprompt.md + the store block + the tools block, in that order.

        Single builder so the tools toggle cannot silently drop the store block
        — rebuilding it in two places is how one of them goes stale.
        """
        base = ""
        if SYSTEM_PROMPT_FILE.exists():
            base = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
        if self.convo_dir is not None:
            base = (base + memory_prompt(self.convo_id, self.convo_dir)).strip()
        if self.tools_enabled:
            base = (base + TOOLS_PROMPT).strip()
            # Pointers only; bodies load through the `skill` tool. Gated on
            # tools_enabled because without that tool the index would
            # advertise something the model has no way to open.
            if self.skills:
                base = (base + skills_mod.index_block(self.skills)).strip()
        return base

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
        if self.convo_dir is None:
            return ""
        p = self.convo_dir / name
        try:
            text = p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if len(text) > cap:
            text = (
                text[:cap]
                + f"\n\n[... truncated at {cap} chars — {name} is too long to inject "
                f"in full. Move detail into {MEMORIES_DIR}/ and leave pointers here.]"
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
        """Create .convos/<uuid>/ with its seed files. Leaves conversation alone."""
        self.convo_id = str(uuid.uuid4())
        self._refresh_ctx_label()   # the footer names the conversation
        self.convo_dir = CONVO_DIR / self.convo_id
        self.convo_path = self.convo_dir / TRANSCRIPT_NAME
        try:
            (self.convo_dir / MEMORIES_DIR).mkdir(parents=True, exist_ok=True)
            for fname, seed in CONVO_SEED_FILES.items():
                f = self.convo_dir / fname
                if not f.exists():  # never clobber a resumed store
                    f.write_text(seed, encoding="utf-8")
        except OSError as e:
            self._note_persist_error(e)
        # The owning seat, so /resume can say WHOSE conversation this was.
        # Written at creation because the seat may be renamed or re-registered
        # later, and the answer wanted is who owned it THEN.
        seat = getattr(self, "seat", None)
        self._write_record(
            {
                "type": "meta",
                "v": 3,
                "id": self.convo_id,
                "created": time.time(),
                "model": self.model_id,
                **({"agent_name": seat.name} if seat is not None else {}),
                **({"agent_id": seat.agent_id} if seat is not None else {}),
            }
        )

    def _note_persist_error(self, e: Exception) -> None:
        if self._persist_error is not None:
            return
        self._persist_error = f"{type(e).__name__}: {e}"
        try:
            self._system(
                f"[save failed — this conversation is memory-only]\n{self._persist_error}"
            )
        except Exception:
            pass  # not mounted yet; /convos re-surfaces it

    def _write_record(self, rec: dict) -> None:
        if self.convo_path is None or self._convo_loading:
            return
        try:
            self.convo_path.parent.mkdir(parents=True, exist_ok=True)
            with self.convo_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            # Surface it once. A persistence layer that fails silently is worse
            # than none at all: you find out at /resume, when it is too late.
            self._note_persist_error(e)

    def _append(self, msg: dict) -> None:
        """Append to the live conversation AND to disk. Single choke point."""
        self.conversation.append(msg)
        self._write_record({"type": "msg", "ts": time.time(), "message": msg})

    def _snapshot(self, reason: str = "") -> None:
        """Full-list record. Kept for readability of older files; prefer _edit."""
        self._write_record(
            {
                "type": "snapshot",
                "ts": time.time(),
                "reason": reason,
                "messages": self.conversation,
            }
        )

    def _edit(self, index: int, reason: str = "") -> None:
        """Record an in-place change to ONE message.

        /system and the tools toggle only ever rewrite message 0, which is the
        biggest message in the file (system prompt + store block + tools). A
        snapshot for that wrote the entire conversation to disk to record a
        one-message change.
        """
        if not (0 <= index < len(self.conversation)):
            return self._snapshot(reason)  # shouldn't happen; degrade safely
        self._write_record(
            {
                "type": "edit",
                "ts": time.time(),
                "reason": reason,
                "index": index,
                "message": self.conversation[index],
            }
        )

    def _truncate(self, keep_from: int, prepend: list[dict], reason: str = "") -> None:
        """Record 'drop the head, splice these in front of what remains'."""
        keeps_system = bool(
            self.conversation and self.conversation[0].get("role") == "system"
        )
        self._write_record(
            {
                "type": "truncate",
                "ts": time.time(),
                "reason": reason,
                "keep_from": keep_from,
                "keep_system": keeps_system,
                "prepend": prepend,
            }
        )

    @staticmethod
    def _read_convo(path: Path) -> tuple[dict, list[dict]]:
        meta: dict = {}
        msgs: list[dict] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a torn final line from a hard kill
                kind = rec.get("type")
                if kind == "meta":
                    meta = rec
                elif kind == "snapshot":
                    msgs = list(rec.get("messages") or [])
                elif kind == "edit":
                    i = rec.get("index")
                    if isinstance(i, int) and 0 <= i < len(msgs) and isinstance(
                        rec.get("message"), dict
                    ):
                        msgs[i] = rec["message"]
                elif kind == "truncate":
                    keep_from = rec.get("keep_from")
                    if not isinstance(keep_from, int) or keep_from < 0:
                        continue  # unreadable marker: leave the history intact
                    head = []
                    if (
                        rec.get("keep_system")
                        and msgs
                        and msgs[0].get("role") == "system"
                    ):
                        head = [msgs[0]]
                    prepend = [
                        m for m in (rec.get("prepend") or []) if isinstance(m, dict)
                    ]
                    msgs = head + prepend + msgs[keep_from:]
                elif kind == "msg" and isinstance(rec.get("message"), dict):
                    msgs.append(rec["message"])
        return meta, msgs

    @staticmethod
    def _fmt_size(n: int) -> str:
        for unit, div in (("MB", 1024 * 1024), ("KB", 1024)):
            if n >= div:
                return f"{n / div:.1f}{unit}"
        return f"{n}B"

    @classmethod
    def _convo_title(cls, msgs: list[dict]) -> str:
        for m in msgs:
            if m.get("role") != "user":
                continue
            c = cls._flatten(m.get("content")).replace("\n", " ")
            if c:
                return c[:60] + ("\u2026" if len(c) > 60 else "")
        return "(no user message)"

    @staticmethod
    def _flatten(content) -> str:
        if isinstance(content, list):  # image turn: [{image_url...}, {text...}]
            text = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")
            )
            return ("[image] " + text).strip()
        return (content or "").strip()

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
        self.convo_path = path
        self.convo_dir = path.parent
        self.convo_id = meta.get("id") or path.parent.name
        self._refresh_ctx_label()   # resumed into a different conversation
        # The restored system message already names THIS store (it was written
        # with this uuid), so it is not rebuilt — rebuilding would overwrite
        # whatever the agent or /system had changed it to.
        (self.convo_dir / MEMORIES_DIR).mkdir(parents=True, exist_ok=True)

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
                        w.body.content = Markdown(text)
                    except Exception:
                        w.body.content = text
                    assistants += 1
            elif role == "tool":
                tools += 1
        # Tool traffic is summarised rather than replayed — the widgets carry
        # streamed state that cannot be faithfully reconstructed from the log.
        # It IS still in self.conversation, so the model sees all of it.
        note = f", {tools} tool result(s) restored to context but not redrawn" if tools else ""
        mem_dir = self.convo_dir / MEMORIES_DIR
        n_mem = len(list(mem_dir.glob("*.md"))) if mem_dir.exists() else 0
        store = ", ".join(
            f for f in CONVO_SEED_FILES if (self.convo_dir / f).exists()
        ) or "none"
        self._system(
            f"Resumed {self.convo_id}\n"
            f"  {users} user / {assistants} assistant message(s){note}\n"
            f"  store: {store} · {n_mem} file(s) in {MEMORIES_DIR}/\n"
            f"  appending to {self.convo_dir.name}/{path.name}"
        )
        self._scroll_down()

    def _list_convos(self) -> list[tuple[Path, dict, list[dict]]]:
        """Returns (transcript_path, meta, messages) newest first."""
        if not CONVO_DIR.exists():
            return []
        out = []
        for d in CONVO_DIR.iterdir():
            if not d.is_dir():
                continue
            p = d / TRANSCRIPT_NAME
            if not p.exists():
                continue
            try:
                meta, msgs = self._read_convo(p)
            except OSError:
                continue
            out.append((p, meta, msgs))
        out.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)
        return out

    def _update_header(self) -> None:
        mode = "tools:4" if self.tools_enabled else "no tools"
        think = self.thinking_level or "default"
        parts = [p for p in (self.model_id, mode, f"think:{think}") if p]
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
            models = await self.client.models.list()
            SKIP = {"embed", "embedding"}
            self.available_models = [
                m.id for m in models.data
                if not any(s in m.id.lower() for s in SKIP)
            ]
            if self.available_models:
                # A configured default wins when the server is serving it. `pin`
                # re-applies it on EVERY connect; without pin it only fills an
                # empty/invalid selection, so a mid-session /model switch sticks.
                want = self.settings.default_model
                if want and want in self.available_models:
                    if self.settings.pin_default_model or not self.model_id:
                        self.model_id = want
                elif want:
                    self._system(
                        f"Default model {want!r} is not being served — using "
                        f"{self.available_models[0]!r}. (/settings to change it.)"
                    )
                if not self.model_id or self.model_id not in self.available_models:
                    self.model_id = self.available_models[0]
                self._update_header()
                self._fetch_ctx_window()
                self._apply_context_length()
                self._system(f"Connected — model: {self.model_id}")
                if self.tools_enabled:
                    self._system(f"agent loop: up to {self.settings.tool_iterations} tool iterations per turn (/settings)")
                if len(self.available_models) > 1:
                    listing = "\n".join(
                        f"  {'> ' if m == self.model_id else '  '}{i+1}. {m}"
                        for i, m in enumerate(self.available_models)
                    )
                    self._system(f"Available models:\n{listing}\nUse /model <number> to switch")
            else:
                self.sub_title = "No model loaded"
                self._system("No chat model loaded in LM Studio")
        except Exception as e:
            self.sub_title = "Disconnected"
            # The one that rots silently: it kept naming localhost after the
            # client could be pointed elsewhere, so the error blamed the wrong host.
            # Name the host we ACTUALLY tried. Reporting config.LM_HOST here is
            # the error-message rot config.py's own docstring warns about.
            self._system(f"Could not connect to {self.settings.lm_host} — {e}")

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
        """
        t = Text()

        # Identity, but only when the seat actually holds it. An unregistered
        # seat displaying a name it does not own is worse than showing nothing:
        # it is a green light for a registration that never happened.
        seat = getattr(self, "seat", None)
        if seat is not None and getattr(seat, "registered", False):
            t.append(str(seat.name), "bold #7d8799")
        elif seat is not None:
            t.append("unregistered", "#e5534b")
        else:
            t.append("no seat", "#5c6370")

        sep = "  \u00b7  "
        t.append(sep, "#5c6370")
        t.append(f"think:{self.thinking_level or 'default'}", "#5c6370")

        if self.convo_id:
            t.append(sep, "#5c6370")
            t.append(self.convo_id[:8], "#5c6370")

        t.append(sep, "#5c6370")

        used, mx = self.ctx_used, self.ctx_max
        if used is None and mx is None:
            t.append("ctx \u2014", "dim")
            return t
        u = f"{used:,}" if used is not None else "\u2014"
        m = f"{mx:,}" if mx is not None else "?"
        pct = (used / mx) if (used is not None and mx) else 0.0
        style = "bold #e5534b" if pct >= 0.9 else ("#e8a33d" if pct >= 0.7 else "#7d8799")
        t.append(f"ctx {u} / {m}", style)
        self._append_tps(t, sep)
        return t

    def _append_tps(self, t: Text, sep: str) -> None:
        """Generation speed, last of all -- the label is `dock: right`, so the
        end of this Text is the right edge of the footer."""
        if self.tps is None:
            return
        t.append(sep, "#5c6370")
        # Coloured by how it FEELS to use, not by an absolute scale: this is a
        # local model on one GPU, and the number that matters is whether the
        # answer arrives faster than you read it.
        style = "#e5534b" if self.tps < 5 else ("#e8a33d" if self.tps < 15 else "#7d8799")
        t.append(f"{self.tps:.1f} tok/s", style)

    def watch_ctx_used(self, value: int | None) -> None:
        self._refresh_ctx_label()

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
    async def _apply_context_length(self) -> None:
        """Ask LM Studio to (re)load the active model at the configured window.

        No-ops when unset, or when the model already reports that length — a
        reload is expensive and evicts the loaded weights, so doing it when
        nothing would change is a cost with no effect.
        """
        want = self.settings.default_context_length
        if not want or not self.model_id:
            return
        if self.ctx_max == want:
            return

        def _load() -> tuple[int, str]:
            # Through ttyguard, never raw subprocess: `lms` is a child that can
            # leave the terminal in a mouse-reporting mode, and the envelope is
            # what repairs it. test_ttyguard enforces this and caught the raw
            # call this method originally shipped with.
            proc = ttyguard.run(
                ["lms", "load", self.model_id, "--context-length", str(want), "--yes"],
                timeout=300,
            )
            return proc.returncode, (proc.stderr or proc.stdout or "").strip()

        self._system(f"Loading {self.model_id} at {want:,} tokens…")
        try:
            code, out = await asyncio.to_thread(_load)
        except FileNotFoundError:
            self._system(
                "Context length is set in /settings but the `lms` CLI is not on PATH, "
                "so it could NOT be applied — the model is running at whatever window "
                "LM Studio loaded it with."
            )
            return
        except Exception as e:
            self._system(f"Could not set context length: {type(e).__name__}: {e}")
            return

        if code != 0:
            # Report the server's own words. A generic failure here would send
            # the reader to the wrong place.
            self._system(f"`lms load` failed ({code}) — context length unchanged.\n{out[:400]}")
            return
        # Re-read rather than assume: the request is what we asked for, the
        # readout is what we got, and LM Studio may clamp to what fits in VRAM.
        self._fetch_ctx_window()

    @work(exclusive=True, group="ctx")
    async def _fetch_ctx_window(self) -> None:
        """Ask LM Studio's native API for the active model's context window."""
        if not self.model_id:
            return
        mid = self.model_id

        def _get() -> int | None:
            req = urllib.request.Request(
                config.API_URL,
                headers={"User-Agent": "LiteTUI"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.load(r)
            models = (
                data if isinstance(data, list) else (data.get("models") or data.get("data"))
            )
            for m in models or []:
                if m.get("id") == mid:
                    return (
                        int(m.get("loaded_context_length") or m.get("max_context_length")),
                        m.get("type"),
                    )
            return None

        try:
            got = await asyncio.to_thread(_get)
        except Exception:
            got = None  # server hiccup — footer just shows "?" for the window size
        val, self.model_type = got if got else (None, None)
        self.ctx_max = val
        self._refresh_ctx_label()

    # ── Message display ──────────────────────────────────────────────────────────

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

    def _maybe_autocompact(self) -> None:
        """Compact by itself once the window passes the configured percent.

        Checked after a turn settles, never mid-stream: compaction rewrites
        self.conversation, and doing that while a response is still being
        appended would race the very messages it is summarising.

        Needs headroom by design. A threshold near 100 leaves no room for the
        compaction request itself to produce a summary, which is the failure it
        exists to prevent.
        """
        if not self.settings.autocompact_enabled:
            return
        if not self.ctx_max or not self.ctx_used:
            return  # window size unknown - never guess a threshold
        pct = self.ctx_used * 100 // self.ctx_max
        if pct < self.settings.autocompact_at_percent:
            return
        if getattr(self, "_autocompact_running", False):
            return
        self._autocompact_running = True
        self._system(
            f"Auto-compacting - context at {pct}% of "
            f"{self.ctx_max:,} (threshold {self.settings.autocompact_at_percent}%)."
        )
        try:
            self._handle_command("/compact")
        finally:
            self._autocompact_running = False

    def _system(self, text: str) -> None:
        log = self.query_one("#chat-log")
        log.mount(ChatMessage(Text(text), classes="system-msg"))
        self._scroll_down()

    def _user_bubble(self, text: str, has_image: bool) -> None:
        log = self.query_one("#chat-log")
        parts: list[str] = []
        if has_image:
            parts.append("[Image attached]")
        if text:
            parts.append(text)
        w = ChatMessage("\n".join(parts), classes="user-msg")
        w.border_title = "You"
        log.mount(w)
        self._scroll_down()

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
        bubble plus the block grows the log by more than _at_bottom's 2-line
        slack in a single frame, so by the time the first reasoning token calls
        in, the reader is judged to have scrolled up -- by the app's OWN newly
        mounted content -- and following is refused for the whole turn. The
        guard written to protect a reader who scrolled up was firing on content
        nobody had scrolled away from.
        """
        # The setting gates the STREAM path only. `only_if_following` is what the
        # stream passes, so guarding on it keeps discrete events (new bubble,
        # tool call, final render) scrolling as before.
        if only_if_following and not self.settings.autoscroll:
            return
        log = self.query_one("#chat-log")
        if only_if_following and not _at_bottom(log):
            return
        log.scroll_end(animate=False)

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

        Returns a short CONFIRMATION, not the image. See VIEW_IMAGE_TOOL_SPEC
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

    @on(Input.Submitted, "#message-input")
    def handle_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text and not self.pending_image:
            return
        event.input.value = ""

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
        self._user_bubble(text, has_image)

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

        self._append({"role": "user", "content": content})
        self.pending_image = None
        self._stream()

    @work(exclusive=True, group="chat")
    async def _stream(self) -> None:
        """Agent loop: stream a turn; if the model called tools, execute them,
        feed results back, and stream again until a plain answer arrives."""
        self._stop_requested = False
        for _iteration in range(self.settings.tool_iterations):
            if self._stop_requested:
                break
            widget = self._assistant_bubble()
            thinking: ThinkingBlock | None = None
            text_full = ""
            reasoning = ""
            tool_acc: dict[int, dict] = {}
            tool_msgs: dict[int, ToolMessage] = {}

            kwargs: dict = {
                "model": self.model_id or "local-model",
                # Live store merged in here, not stored on self.conversation.
                "messages": self._request_messages(),
                "stream": True,
                "max_tokens": (
                    self.settings.max_tokens_tools
                    if self.tools_enabled
                    else self.settings.max_tokens_chat
                ),
                "stream_options": {"include_usage": True},
            }
            # Optional LM Studio sampling flags. Only the ones actually SET are
            # sent: an unset knob must leave the server's own default in charge,
            # which sending an invented zero would not.
            kwargs.update(sampling_kwargs(self.settings))
            if self.tools_enabled:
                kwargs["tools"] = self._all_tools()
            # Sent via extra_body so the value lands in the JSON verbatim: the
            # OpenAI client types reasoning_effort as a fixed Literal, and two
            # of LM Studio's six ("none", "xhigh") are not in it.
            if self.thinking_level:
                kwargs["extra_body"] = {
                    "reasoning_effort": "none"
                    if self.thinking_level == "off"
                    else self.thinking_level
                }

            self._tps_start()
            try:
                stream = await self.client.chat.completions.create(**kwargs)
            except Exception as e:
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
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    # LM Studio streams the thinking trace as `reasoning_content`
                    # (some other OpenAI-compatible servers use `reasoning`).
                    token = getattr(delta, "reasoning_content", None) or getattr(
                        delta, "reasoning", None
                    )
                    if token:
                        self._tps_tick()
                        reasoning += token
                        if thinking is None and self.settings.show_thinking:
                            thinking = ThinkingBlock()
                            widget.mount(thinking, before=widget.body)
                            # Discrete event -> unconditional. See _scroll_down.
                            self._scroll_down()
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
                        text_full += delta.content
                        widget.body.content = text_full + " \u258c"
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
                    for tc in getattr(delta, "tool_calls", None) or []:
                        idx = tc.index
                        slot = tool_acc.setdefault(
                            idx, {"id": None, "name": "", "arguments": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        fn = tc.function
                        if fn is not None:
                            if fn.name:
                                slot["name"] += fn.name
                                if idx not in tool_msgs:
                                    msg = ToolMessage(fn.name)
                                    tool_msgs[idx] = msg
                                    self.query_one("#chat-log").mount(msg)
                                self._scroll_down()
                            if fn.arguments:
                                slot["arguments"] += fn.arguments
                                if idx in tool_msgs:
                                    tool_msgs[idx].set_args(slot["arguments"])
                                    self._scroll_down()
            except Exception as e:
                widget.body.content = Text(f"Error: {e}", style="bold red")
                widget.border_title = "Error"
                self._scroll_down()
                return

            # Final render of this turn's bubble.
            if thinking is not None:
                thinking.finalize()
            if text_full:
                try:
                    widget.body.content = Markdown(text_full)
                except Exception:
                    widget.body.content = text_full
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
                self.call_after_refresh(self._maybe_autocompact)
                return

            if not tool_acc:
                # Turn is over. Check the window AFTER this worker exits:
                # _compact shares group="chat" and would cancel us mid-frame.
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
                    fn = self._dispatch_for(name)
                    if fn is None:
                        result, ok = f"[error] unknown tool: {name}", False
                    else:
                        try:
                            # Run blocking tools (shell, disk, network) off-loop.
                            result = await asyncio.to_thread(fn, args)
                            ok = True
                        except Exception as e:
                            result, ok = f"[error] {type(e).__name__}: {e}", False
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
                self._scroll_down()
                self._append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": name,
                        "content": result,
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

    @work(exclusive=True, group="chat")
    async def _compact(self, extra: str = "") -> None:
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
        self._system(f"Compacting {len(head)} messages…")

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
            for _ in range(self.settings.compact_max_tool_iters):
                kwargs: dict = {
                    "model": self.model_id or "local-model",
                    "messages": ask,
                    "stream": False,
                    "max_tokens": self.settings.compact_max_tokens,
                    # 🔴 THE OLD MITIGATION WAS DEFEATED BY A MECHANISM THIS
                    # FILE ALREADY DOCUMENTS ELSEWHERE. It forced "none" and cut
                    # the budget to 2048 after a 12k budget was eaten by the
                    # thinking trace. But see _warn_reasoning_ignored: a VIRTUAL
                    # MODEL whose own level set lacks "none" DROPS the field with
                    # a 200 and reasons at ITS default (xhigh) anyway. So forcing
                    # "none" bought nothing and the smaller budget guaranteed the
                    # failure -- reasoning consumed all 2048 and the summary was
                    # never written. That is the observed
                    #   "Compact failed - no summary produced".
                    # Send a level the model actually accepts, and give it room.
                    "extra_body": {
                        "reasoning_effort": (
                            "none"
                            if self.settings.compact_thinking_level == "off"
                            else self.settings.compact_thinking_level
                        )
                    },
                }
                if self.tools_enabled:
                    kwargs["tools"] = self._all_tools()

                resp = await self.client.chat.completions.create(**kwargs)
                msg = resp.choices[0].message
                calls = list(getattr(msg, "tool_calls", None) or [])
                if not calls:
                    summary = (msg.content or "").strip()
                    break

                try:
                    ask.append(msg.model_dump(exclude_none=True))
                except Exception:
                    ask.append(
                        {
                            "role": "assistant",
                            "content": msg.content,
                            "tool_calls": [
                                {
                                    "id": c.id,
                                    "type": "function",
                                    "function": {
                                        "name": c.function.name,
                                        "arguments": c.function.arguments,
                                    },
                                }
                                for c in calls
                            ],
                        }
                    )

                for c in calls:
                    fname = c.function.name
                    try:
                        fargs = json.loads(c.function.arguments or "{}")
                        if not isinstance(fargs, dict):
                            raise ValueError("arguments must be a JSON object")
                        fn = self._dispatch_for(fname)
                        result = (
                            await asyncio.to_thread(fn, fargs)
                            if fn
                            else f"[error] unknown tool: {fname}"
                        )
                        if fn is not None and fname == "write":
                            writes.append(str(fargs.get("path", "?")))
                    except Exception as e:
                        result = f"[error] {type(e).__name__}: {e}"
                    ask.append(
                        {
                            "role": "tool",
                            "tool_call_id": c.id,
                            "name": fname,
                            "content": result,
                        }
                    )
        except Exception as e:
            self._system(f"Compact failed — conversation unchanged.\n{type(e).__name__}: {e}")
            return

        if not summary:
            self._system(
                "Compact failed — no summary produced "
                f"(gave up after {self.settings.compact_max_tool_iters} tool rounds). "
                "The usual cause is the reasoning trace consuming the whole token budget\n"
                "before any summary is written. Raise Compact max tokens, or lower Compact\n"
                "thinking level, in /settings.\n"
                "Conversation unchanged."
                + (f"\nStore writes that DID land: {', '.join(writes)}" if writes else "")
            )
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
        after_chars = self._msg_chars(self.conversation)
        pct = (100 - after_chars * 100 // before_chars) if before_chars else 0
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
            cfg = _Path(__file__).resolve().parent / "mcp.json"
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
        try:
            path = settings_mod.save(new)
        except OSError as e:
            self._system(f"Settings applied for this session but NOT saved: {e}")
            path = None

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

    def _handle_command(self, cmd: str) -> None:
        parts = cmd.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if name in ("/clear-screen", "/clearscreen", "/cls"):
            # The DISPLAY only. /clear resets the conversation; this does not.
            self._clear_screen(
                note="Screen cleared. The conversation is unchanged - the model "
                     "still has everything it had a moment ago."
            )
            return

        if name in ("/settings", "/config", "/set"):
            self.push_screen(
                SettingsScreen(
                    self.settings,
                    models=self.available_models,
                    mcp_servers=self._mcp_server_names(),
                ),
                self._on_settings_saved,
            )
            return

        if name in ("/clear", "/reset", "/new"):
            self.conversation.clear()
            self._new_convo()  # a fresh file — never reuse the old one
            self._load_system_prompt()
            self.query_one("#chat-log").remove_children()
            self._system(
                f"New conversation — {self.convo_id}\n"
                f"  store: {self.convo_dir}\n"
                f"  memory.md · soul.md · handoff.md · {MEMORIES_DIR}/"
            )

        elif name == "/system":
            if arg:
                if self.conversation and self.conversation[0]["role"] == "system":
                    self.conversation[0]["content"] = arg
                else:
                    self.conversation.insert(
                        0, {"role": "system", "content": arg}
                    )
                self._edit(0, "system prompt changed")
                preview = arg[:80] + ("..." if len(arg) > 80 else "")
                self._system(f"System prompt set: {preview}")
            else:
                self._system("Usage: /system <prompt>")

        elif name in ("/think", "/thinking"):
            if not arg:
                current = self.thinking_level or "unset"
                note = (
                    "\nunset means the field is not sent at all — LM Studio then "
                    "applies its OWN default, which is xhigh. 'unset' is not 'off'."
                )
                self._system(
                    f"Thinking level: {current}\n"
                    f"Levels: {', '.join(THINKING_LEVELS)}, or 'unset'\n"
                    f"Usage: /think <level>{note}"
                )
            elif arg.lower() in ("unset", "default", "server"):
                self.thinking_level = None
                self._update_header()
                self._system("Thinking level unset — LM Studio's default (xhigh) applies.")
            elif arg.lower() in THINKING_LEVELS:
                self.thinking_level = arg.lower()
                self._update_header()
                wire = "none" if self.thinking_level == "off" else self.thinking_level
                self._system(f"Thinking level: {self.thinking_level} (sends reasoning_effort={wire!r})")
            else:
                self._system(
                    f"Unknown level: {arg}\nValid: {', '.join(THINKING_LEVELS)}, unset"
                )

        elif name == "/compact":
            self._compact(arg)

        elif name in ("/convos", "/conversations", "/list"):
            rows = self._list_convos()
            if not rows:
                self._system(f"No saved conversations yet.\nThey land in {CONVO_DIR}")
                return
            lines = []
            total = 0
            for i, (p, meta, msgs) in enumerate(rows[:30], 1):
                mark = ">" if p == self.convo_path else " "
                stamp = time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime))
                turns = sum(1 for m in msgs if m.get("role") in ("user", "assistant"))
                mem = p.parent / MEMORIES_DIR
                n_mem = len(list(mem.glob("*.md"))) if mem.exists() else 0
                badge = f" ✎{n_mem}" if n_mem else "   "
                total += p.stat().st_size
                lines.append(
                    f" {mark} {i:>2}. {p.parent.name[:8]}  {stamp}  {turns:>3} msg{badge}  "
                    f"{self._fmt_size(p.stat().st_size):>7}  {self._convo_title(msgs)}"
                )
            extra = f"\n(+{len(rows) - 30} older)" if len(rows) > 30 else ""
            warn = (
                f"\n\n[!] saving is BROKEN this session: {self._persist_error}"
                if self._persist_error
                else ""
            )
            self._system(
                "Saved conversations (newest first):\n"
                + "\n".join(lines)
                + extra
                + f"\n{self._fmt_size(total)} of transcript across {len(rows)} conversation(s)"
                + "\nUse /resume <number> to load one."
                + warn
            )

        elif name == "/resume":
            rows = self._list_convos()
            if not rows:
                self._system("Nothing to resume.")
                return
            target = None
            if arg.isdigit():
                idx = int(arg) - 1
                if 0 <= idx < len(rows):
                    target = rows[idx]
            elif arg:
                # Match on the uuid FOLDER, not the transcript stem — every
                # transcript is named convo.jsonl, so stems no longer identify.
                for row in rows:
                    uid = row[0].parent.name
                    if uid == arg or uid.startswith(arg):
                        target = row
                        break
            if target is None and arg:
                self._system(
                    f"No conversation matches {arg!r}. Run /resume with no argument to pick one."
                )
                return
            if target is None:
                # Same picker as /model, so the two interactions cannot drift.
                items = []
                for path_, meta_, msgs_ in rows[:40]:
                    stamp = time.strftime("%m-%d %H:%M", time.localtime(path_.stat().st_mtime))
                    turns = sum(1 for x in msgs_ if x.get("role") in ("user", "assistant"))
                    memdir = path_.parent / MEMORIES_DIR
                    nmem = len(list(memdir.glob("*.md"))) if memdir.exists() else 0
                    badge = f" ✎{nmem}" if nmem else "   "
                    # The conversation's own uuid — on disk all along, never shown.
                    cid = path_.parent.name[:8]
                    # The owning seat, only for conversations written since v3
                    # meta. Older ones show blanks rather than a fabricated name.
                    who = str(meta_.get("agent_name") or "")[:10]
                    aid = str(meta_.get("agent_id") or "")[:8]
                    owner = f"{who} {aid}".strip() or "—"
                    items.append(
                        (
                            str(path_),
                            f"{stamp}  {cid}  {owner:<19}  {turns:>3} msg{badge}  "
                            f"{self._fmt_size(path_.stat().st_size):>7}  "
                            f"{self._convo_title(msgs_)}",
                        )
                    )
                self.push_screen(
                    PickerScreen(
                        "Resume a conversation",
                        items,
                        current=str(self.convo_path) if self.convo_path else None,
                    ),
                    self._on_convo_picked,
                )
                return
            self._resume(target[0])

        elif name in ("/model", "/models"):
            if arg:
                # Switch by number or name
                if arg.isdigit():
                    idx = int(arg) - 1
                    if 0 <= idx < len(self.available_models):
                        self.model_id = self.available_models[idx]
                        self._update_header()
                        self._fetch_ctx_window()
                        self._system(f"Switched to: {self.model_id}")
                    else:
                        self._system(f"Invalid number. Use 1-{len(self.available_models)}")
                elif arg in self.available_models:
                    self.model_id = arg
                    self._update_header()
                    self._fetch_ctx_window()
                    self._system(f"Switched to: {self.model_id}")
                else:
                    self._system(f"Model not found: {arg}")
            elif not self.available_models:
                self._system("No models discovered — try /reconnect")
            else:
                # Clickable picker. `/model <n>` and `/model <name>` are handled
                # above and still work, so scripting and muscle memory survive.
                rows = [
                    (m, ("▸ " if m == self.model_id else "  ") + m)
                    for m in self.available_models
                ]
                self.push_screen(
                    PickerScreen("Select a model", rows, current=self.model_id),
                    self._on_model_picked,
                )

        elif name == "/reconnect":
            self._connect()

        elif name in ("/quit", "/exit"):
            self.exit()

        elif name in ("/help", "/?"):
            # Scrollable modal with a Close button; the text is unchanged.
            self.push_screen(HelpScreen(
                "/settings        open the settings panel (every knob, scrollable)\n"
                "/new /clear      start a new conversation (new file on disk)\n"
                "/clear-screen    clear the DISPLAY only \u2014 conversation untouched\n"
                "/system <text>   set the system prompt\n"
                "/model [n]       show or switch model\n"
                "/think [level]   thinking level: "
                + ", ".join(THINKING_LEVELS)
                + ", unset\n"
                "/compact [hint]  summarise older messages, keep the last "
                f"{self.settings.compact_keep_recent}\n"
                "/convos          list saved conversations\n"
                "/resume <n|id>   load a saved conversation (id = uuid prefix)\n"
                "/reconnect       reconnect   |   /quit  exit\n"
                "Esc     stop the current turn (asks first; Esc again = force)\n"
                "Ctrl+T  toggle agent tools (bash, read, write, web_fetch)\n"
                "drag    select text  |  Ctrl+Shift+C  copy the selection\n"
                "Shift+drag  select with the TERMINAL instead (system clipboard) \u2014\n"
                "        the app captures the mouse, so a plain drag never reaches it\n"
                f"store: {CONVO_DIR.name}/<uuid>/ holds convo.jsonl, memory.md,\n"
                f"       soul.md, handoff.md and {MEMORIES_DIR}/ \u2014 the agent is told\n"
                "       its own path in the system prompt and manages them itself\n"
                "footer: live context usage \u2014 ctx used / window"
            ))

        else:
            self._system(f"Unknown: {name} — try /help")

    def action_clear_chat(self) -> None:
        self._handle_command("/clear")


def main():
    LiteTUI().run()


if __name__ == "__main__":
    main()
