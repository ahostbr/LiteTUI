"""The floor tools: bash, read, write, web_fetch.

The ONLY critical plugin — a chat harness that boots without the ability to
run a command or read a file is a broken checkout pretending to work, so a
registration failure here aborts boot instead of degrading. Everything in
this module moved VERBATIM out of app.py in the plugin split; limits mirror
pi's defaults (2000 lines / 50KB).
"""
from __future__ import annotations

import re
import subprocess
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import ttyguard
from fmt import fmt_dur
from plugins import PluginManifest

TOOL_MAX_LINES = 2000
TOOL_MAX_BYTES = 50 * 1024  # 50KB
WEB_FETCH_MAX_CHARS = 20_000
WEB_FETCH_TIMEOUT_S = 20
BASH_DEFAULT_TIMEOUT_S = 120


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


def _bash_timeout_result(proc: subprocess.Popen, timeout: int) -> str:
    """Format the result when the command outran its timeout budget."""
    ttyguard.kill_tree(proc.pid)
    try:
        out, err = proc.communicate(timeout=10)
    except Exception:
        out, err = "", ""
    partial = _truncate_tail((out or "") + (err or ""))
    return f"[timed out after {timeout}s]\n{partial}".strip()


def _bash_cancelled_result(out: str, err: str, t0: float) -> str:
    """Format the result when the user cancelled a still-running command."""
    partial = _truncate_tail((out or "") + (("\n[stderr]\n" + err) if err else ""))
    note = f"[cancelled by user after {fmt_dur(time.monotonic() - t0)}]"
    return (note + (("\n" + partial) if partial.strip() else "")).strip()


def _bash_completed_result(out: str, err: str, returncode: int) -> str:
    """Format the result of a command that ran to completion."""
    out = out or ""
    err = err or ""
    if err:
        out = (out + "\n[stderr]\n" + err) if out else "[stderr]\n" + err
    out = _truncate_tail(out)
    if returncode != 0:
        suffix = f"\n\n[command exited with code {returncode}]"
        out = (out + suffix) if out else suffix.strip()
    return out or "(no output)"


def tool_bash(args: dict) -> str:
    command = (args.get("command") or "").strip()
    if not command:
        return "[error] missing 'command'"
    try:
        timeout = int(args.get("timeout") or BASH_DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = BASH_DEFAULT_TIMEOUT_S
    # Through the envelope, which owns errors="replace", CREATE_NO_WINDOW and
    # the terminal repair. popen (not run) so the HANDLE survives: run() blocks
    # with the Popen trapped inside it, which is why a runaway bash could not
    # be cancelled — the process existed and nothing could reach it.
    t0 = time.monotonic()
    try:
        proc = ttyguard.popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            cwd=str(Path.cwd()),
        )
    except OSError as e:
        return f"[error] {type(e).__name__}: {e}"
    ttyguard.CANCELLABLE["proc"], ttyguard.CANCELLABLE["cancelled"] = proc, False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return _bash_timeout_result(proc, timeout)
    finally:
        ttyguard.CANCELLABLE["proc"] = None
    if ttyguard.CANCELLABLE["cancelled"]:
        # The kill closed the pipes, so communicate() returned with whatever
        # the tree wrote before dying — the model sees the partial output and
        # an honest verdict, and the TURN CARRIES ON. That is the difference
        # between this and Esc, which stops the whole turn.
        ttyguard.CANCELLABLE["cancelled"] = False
        return _bash_cancelled_result(out, err, t0)
    return _bash_completed_result(out, err, proc.returncode)


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


BASH_SPEC = {
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
}

READ_SPEC = {
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
}

WRITE_SPEC = {
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
}

WEB_FETCH_SPEC = {
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
}


def _register(ctx) -> None:
    ctx.tool(BASH_SPEC, tool_bash)
    ctx.tool(READ_SPEC, tool_read)
    ctx.tool(WRITE_SPEC, tool_write)
    ctx.tool(WEB_FETCH_SPEC, tool_web_fetch)


PLUGIN = PluginManifest(id="core-tools", critical=True, register=_register)
