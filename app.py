"""LM Studio Chat — TUI client with image support."""

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

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Static
from textual.worker import WorkerState
from textual import work, on
from openai import AsyncOpenAI
from rich.markdown import Markdown
from rich.text import Text


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_DIM = 1536
SYSTEM_PROMPT_FILE = Path(__file__).parent / "systemprompt.md"

# ── Conversation persistence ─────────────────────────────────────
# .convos/<uuid>/
#     convo.jsonl   append-only transcript
#     memory.md     INDEX the agent maintains — one line per memory
#     soul.md       who this agent is; persists across resumes
#     handoff.md    what is in flight, for whoever picks this up
#     memories/     the actual notes: i-learned-this.md, uncapped
CONVO_DIR = Path(__file__).parent / ".convos"
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
        "This file is injected into every prompt, so a long line costs you on\n"
        "every turn and crowds out other entries.\n\n"
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

1. THE THREE FILES BELOW ARE ALREADY IN THIS PROMPT — you are reading their
   current contents, refreshed every turn. Do not re-read them with a tool
   just to see what they say. DO open a file in `{MEMORIES_DIR}/` when an
   index line suggests it holds what you need; those are not injected.
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
TOOL_MAX_ITERATIONS = int(os.environ.get("LM_TOOL_ITERS", "48"))  # safety cap on the tool loop (env-overridable; was 12)
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
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
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
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; LMStudioChat)"}
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
        # NOTE: use the `content` setter, not .update() — in textual 8.0.2
        # update() does not invalidate the content-size cache, so an
        # auto-height parent would freeze at the first (small) height.
        self.text.content = Text(self._buffer + " \u258c")

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
        label = Static("", id="ctx-label")
        app = self.app
        if hasattr(app, "ctx_label_text"):
            label.content = app.ctx_label_text
        yield label


class LMStudioChat(App):
    """TUI chat client for LM Studio."""

    TITLE = "LM Studio Chat"
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

    #ctx-label {
        dock: right;
        padding-right: 1;
        background: $footer-background;
    }

    ConfirmStop {
        align: center middle;
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
        Binding("escape", "stop_turn", "Stop turn", priority=True),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+o", "paste_image", "Paste Image"),
        Binding("ctrl+x", "clear_image", "Clear Image"),
        Binding("ctrl+l", "clear_chat", "Clear Chat"),
        Binding("ctrl+t", "toggle_tools", "Tools on/off"),
    ]

    pending_image: reactive[str | None] = reactive(None)
    ctx_used: reactive[int | None] = reactive(None)

    def __init__(self):
        super().__init__()
        self.conversation: list[dict] = []
        self.model_id: str = ""
        self.available_models: list[str] = []
        self.tools_enabled = True
        self.ctx_max: int | None = None  # effective context window (tokens), from LM Studio
        # None = send no reasoning_effort at all, which is what this app did
        # before /think existed. LM Studio's own default for an ABSENT value is
        # xhigh, so "unset" is not "off" — /think off is a different thing and
        # sends "none" explicitly.
        self.thinking_level: str | None = None
        self.convo_id: str = ""
        self.convo_dir: Path | None = None
        self.convo_path: Path | None = None  # <convo_dir>/convo.jsonl
        self._convo_loading = False  # suppress writes while replaying from disk
        self._stop_requested = False  # Esc-to-stop, checked inside the stream loop
        self._persist_error: str | None = None
        self.client = AsyncOpenAI(
            base_url="http://localhost:1234/v1",
            api_key="lm-studio",
        )
        self._new_convo()
        self._load_system_prompt()

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat-log")
        yield Static(
            "  Image attached — Ctrl+X to remove", id="image-indicator"
        )
        yield Input(
            placeholder="Message... (Ctrl+O paste image | /help for commands)",
            id="message-input",
        )
        yield ContextFooter()

    def on_mount(self) -> None:
        self.query_one("#message-input", Input).focus()
        self._connect()

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

    def _store_block(self) -> str:
        # memory.md is capped hardest ON PURPOSE: it is an index, and an index
        # that needs more than this has stopped being one.
        parts = []
        for name, cap in (("memory.md", 6000), ("soul.md", 8000), ("handoff.md", 8000)):
            body = self._read_store_file(name, cap)
            if body:
                parts.append(f"### {name} (current contents)\n\n{body}")
        if not parts:
            return ""
        return (
            "\n\n## Your store, as it stands right now\n\n"
            "Read from disk this turn. If you change these files, the change "
            "appears here on your next turn.\n\n" + "\n\n".join(parts) + "\n"
        )

    def _request_messages(self) -> list[dict]:
        """self.conversation with the live store merged into the system message."""
        msgs = list(self.conversation)
        block = self._store_block()
        if not block:
            return msgs
        if msgs and msgs[0].get("role") == "system":
            msgs[0] = {**msgs[0], "content": msgs[0].get("content", "") + block}
        else:
            msgs.insert(0, {"role": "system", "content": block.strip()})
        return msgs

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
        self._write_record(
            {
                "type": "meta",
                "v": 2,
                "id": self.convo_id,
                "created": time.time(),
                "model": self.model_id,
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
                if not self.model_id or self.model_id not in self.available_models:
                    self.model_id = self.available_models[0]
                self._update_header()
                self._fetch_ctx_window()
                self._system(f"Connected — model: {self.model_id}")
                if self.tools_enabled:
                    self._system(f"agent loop: up to {TOOL_MAX_ITERATIONS} tool iterations per turn (env LM_TOOL_ITERS)")
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
            self._system(f"Could not connect to localhost:1234 — {e}")

    # ── Context window readout (footer) ───────────────────────

    @property
    def ctx_label_text(self) -> Text:
        used, mx = self.ctx_used, self.ctx_max
        if used is None and mx is None:
            return Text("ctx \u2014", "dim")
        u = f"{used:,}" if used is not None else "\u2014"
        m = f"{mx:,}" if mx is not None else "?"
        pct = (used / mx) if (used is not None and mx) else 0.0
        style = "bold #e5534b" if pct >= 0.9 else ("#e8a33d" if pct >= 0.7 else "#7d8799")
        return Text(f"ctx {u} / {m}", style)

    def watch_ctx_used(self, value: int | None) -> None:
        self._refresh_ctx_label()

    def _refresh_ctx_label(self) -> None:
        try:
            label = self.query_one("#ctx-label", Static)
        except Exception:
            return  # footer not composed yet; it picks up the value when it composes
        label.content = self.ctx_label_text

    @work(exclusive=True, group="ctx")
    async def _fetch_ctx_window(self) -> None:
        """Ask LM Studio's native API for the active model's context window."""
        if not self.model_id:
            return
        mid = self.model_id

        def _get() -> int | None:
            req = urllib.request.Request(
                "http://localhost:1234/api/v0/models",
                headers={"User-Agent": "LMStudioChat"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.load(r)
            models = (
                data if isinstance(data, list) else (data.get("models") or data.get("data"))
            )
            for m in models or []:
                if m.get("id") == mid:
                    return int(m.get("loaded_context_length") or m.get("max_context_length"))
            return None

        try:
            val = await asyncio.to_thread(_get)
        except Exception:
            val = None  # server hiccup — footer just shows "?" for the window size
        self.ctx_max = val
        self._refresh_ctx_label()

    # ── Message display ──────────────────────────────────────────────────────────

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

    def _scroll_down(self) -> None:
        self.query_one("#chat-log").scroll_end(animate=False)

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
        for _iteration in range(TOOL_MAX_ITERATIONS):
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
                "max_tokens": 16384 if self.tools_enabled else 4096,
                "stream_options": {"include_usage": True},
            }
            if self.tools_enabled:
                kwargs["tools"] = TOOLS
            # Sent via extra_body so the value lands in the JSON verbatim: the
            # OpenAI client types reasoning_effort as a fixed Literal, and two
            # of LM Studio's six ("none", "xhigh") are not in it.
            if self.thinking_level:
                kwargs["extra_body"] = {
                    "reasoning_effort": "none"
                    if self.thinking_level == "off"
                    else self.thinking_level
                }

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
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    # LM Studio streams the thinking trace as `reasoning_content`
                    # (some other OpenAI-compatible servers use `reasoning`).
                    token = getattr(delta, "reasoning_content", None) or getattr(
                        delta, "reasoning", None
                    )
                    if token:
                        reasoning += token
                        if thinking is None:
                            thinking = ThinkingBlock()
                            widget.mount(thinking, before=widget.body)
                        thinking.append(token)
                    if delta.content:
                        text_full += delta.content
                        widget.body.content = text_full + " \u258c"
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
                return

            if not tool_acc:
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
                    fn = TOOL_DISPATCH.get(name)
                    if fn is None:
                        result, ok = f"[error] unknown tool: {name}", False
                    else:
                        try:
                            # Run blocking tools (shell, disk, network) off-loop.
                            result = await asyncio.to_thread(fn, args)
                            ok = True
                        except Exception as e:
                            result, ok = f"[error] {type(e).__name__}: {e}", False
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

        self._system(
            f"[stopped \u2014 reached {TOOL_MAX_ITERATIONS} tool iterations in one turn]"
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

        tail = self._safe_tail(body, COMPACT_KEEP_RECENT)
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
            block = self._store_block()
            ask.insert(0, {**system, "content": system.get("content", "") + block})

        # Tools are passed so STEP 1 of COMPACT_PROMPT can actually happen.
        # Without them the instruction to persist is theatre: the model narrates
        # writing files and nothing reaches disk.
        summary = ""
        writes: list[str] = []
        try:
            for _ in range(COMPACT_MAX_TOOL_ITERS):
                kwargs: dict = {
                    "model": self.model_id or "local-model",
                    "messages": ask,
                    "stream": False,
                    "max_tokens": 2048,
                    # Forced to "none" regardless of /think. Measured on this
                    # box: with reasoning unset (server default xhigh) a 12k
                    # budget was spent entirely on the thinking trace and the
                    # answer came back EMPTY. Summarising is extraction; a
                    # compaction that returns nothing is worse than none.
                    "extra_body": {"reasoning_effort": "none"},
                }
                if self.tools_enabled:
                    kwargs["tools"] = TOOLS

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
                        fn = TOOL_DISPATCH.get(fname)
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
                f"(gave up after {COMPACT_MAX_TOOL_ITERS} tool rounds). "
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
            f"Kept the last {len(tail)}. Scrollback above is untouched."
            + store_note
        )

    # ── Commands ─────────────────────────────────────────────────

    def _handle_command(self, cmd: str) -> None:
        parts = cmd.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

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
            if target is None:
                self._system(
                    f"Usage: /resume <number|id>   (1-{len(rows)}; see /convos)"
                    if arg
                    else f"Usage: /resume <number|id>   (1-{len(rows)}; see /convos)"
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
            else:
                listing = "\n".join(
                    f"  {'> ' if m == self.model_id else '  '}{i+1}. {m}"
                    for i, m in enumerate(self.available_models)
                )
                self._system(f"Current: {self.model_id}\n{listing}\nUse /model <number> to switch")

        elif name == "/reconnect":
            self._connect()

        elif name in ("/quit", "/exit"):
            self.exit()

        elif name in ("/help", "/?"):
            self._system(
                "/new /clear      start a new conversation (new file on disk)\n"
                "/system <text>   set the system prompt\n"
                "/model [n]       show or switch model\n"
                "/think [level]   thinking level: "
                + ", ".join(THINKING_LEVELS)
                + ", unset\n"
                "/compact [hint]  summarise older messages, keep the last "
                f"{COMPACT_KEEP_RECENT}\n"
                "/convos          list saved conversations\n"
                "/resume <n|id>   load a saved conversation (id = uuid prefix)\n"
                "/reconnect       reconnect   |   /quit  exit\n"
                "Esc     stop the current turn (asks first; Esc again = force)\n"
                "Ctrl+T  toggle agent tools (bash, read, write, web_fetch)\n"
                f"store: {CONVO_DIR.name}/<uuid>/ holds convo.jsonl, memory.md,\n"
                f"       soul.md, handoff.md and {MEMORIES_DIR}/ \u2014 the agent is told\n"
                "       its own path in the system prompt and manages them itself\n"
                "footer: live context usage \u2014 ctx used / window"
            )

        else:
            self._system(f"Unknown: {name} — try /help")

    def action_clear_chat(self) -> None:
        self._handle_command("/clear")


def main():
    LMStudioChat().run()


if __name__ == "__main__":
    main()
