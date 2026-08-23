"""The floor tools: bash, read, write, web_fetch.

The ONLY critical plugin — a chat harness that boots without the ability to
run a command or read a file is a broken checkout pretending to work, so a
registration failure here aborts boot instead of degrading. Everything in
this module moved VERBATIM out of app.py in the plugin split; limits mirror
pi's defaults (2000 lines / 50KB).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from litetui import ttyguard
from litetui.fmt import fmt_dur
from litetui.plugins import PluginManifest
from litetui import tool_schemas
from litetui.tool_policy import NETWORK_READ_POLICY, READ_POLICY, SHELL_POLICY, WRITE_POLICY

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
    killed = ttyguard.kill_tree(proc.pid)
    try:
        out, err = proc.communicate(timeout=10)
    except Exception:
        out, err = "", ""
    partial = _truncate_tail((out or "") + (err or ""))
    head = f"[timed out after {timeout}s]"
    if not killed:
        # The model reads this. Telling it the command was stopped when the
        # tree may still be running is the same lie the spec was written to
        # eliminate, one layer down.
        head += (
            " — the process could NOT be confirmed killed and may still be"
            " running; do not assume it stopped"
        )
    return f"{head}\n{partial}".strip()


def _bash_cancelled_result(out: str, err: str, t0: float) -> str:
    """Format the result when the user cancelled a still-running command."""
    partial = _truncate_tail((out or "") + (("\n[stderr]\n" + err) if err else ""))
    note = f"[cancelled by user after {fmt_dur(time.monotonic() - t0)}]"
    if not ttyguard.CANCELLABLE.get("kill_confirmed", True):
        # Same honesty as the timeout arm. A model told the command was
        # cancelled will reason as though it stopped; if we could not confirm
        # the kill, it has to know that.
        note += (
            " — but the kill could NOT be confirmed; the process may still be"
            " running"
        )
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


#: PowerShell 7 first, Windows PowerShell 5.1 second. Resolved ONCE — a
#: which-shell lookup per tool call is a filesystem hit on every command.
_PS_EXE: str | None = None
_PS_RESOLVED = False


def powershell_exe() -> str | None:
    """The best available PowerShell, or None when there is none."""
    global _PS_EXE, _PS_RESOLVED
    if not _PS_RESOLVED:
        _PS_RESOLVED = True
        for cand in ("pwsh", "powershell"):
            found = shutil.which(cand)
            if found:
                _PS_EXE = found
                break
    return _PS_EXE


#: EVERY CLAUSE HERE WAS MEASURED, not reasoned about. Plain `pwsh -Command`
#: gets two things wrong that matter:
#:
#:  1. IT COLLAPSES NATIVE EXIT CODES TO 1. `cmd /c exit 3` and a python
#:     sys.exit(4) both surface as 1, destroying every distinction a caller
#:     reads from an exit code -- grep's 1-means-no-match vs 2-means-error is
#:     the classic. $LASTEXITCODE holds the real number, so it is re-exited.
#:  2. A FAILING CMDLET EXITS 0. It is a non-terminating error, not a failed
#:     exit. $ErrorActionPreference='Stop' would fix it by changing how the
#:     USER'S command behaves, which is too high a price; the $Error.Count
#:     delta detects the same thing and changes nothing.
#:
#: `$?` is deliberately NOT used: it is reset by the very `if` that reads it,
#: and it does not go False for a non-terminating error anyway. Both were
#: tried and both failed the table.
#:
#: OutputRendering kills ANSI at the source rather than stripping it later.
PS_WRAPPER = (
    "$PSStyle.OutputRendering='PlainText'; $global:LASTEXITCODE=0; "
    "$__e=$Error.Count; "
    "& {{ {command} }}; "
    "if ($LASTEXITCODE) {{ exit $LASTEXITCODE }} "
    "elseif ($Error.Count -gt $__e) {{ exit 1 }} else {{ exit 0 }}"
)


def _run_shell(argv, *, shell: bool, timeout: int) -> str:
    """The shared body: spawn, stay cancellable, report honestly.

    bash and powershell differ ONLY in what gets spawned. Two copies of the
    truncation, cancellation and exit-code reporting would be two things to
    keep in step, and this repo has been bitten by a second copy today already.
    """
    t0 = time.monotonic()
    try:
        proc = ttyguard.popen(
            argv,
            shell=shell,
            stdin=subprocess.DEVNULL,
            cwd=str(Path.cwd()),
        )
    except OSError as e:
        return f"[error] {type(e).__name__}: {e}"
    ttyguard.CANCELLABLE["proc"], ttyguard.CANCELLABLE["cancelled"] = proc, False
    # Reset beside "cancelled": a False left over from a PREVIOUS command
    # would attach its warning to this one's result.
    ttyguard.CANCELLABLE["kill_confirmed"] = True
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return _bash_timeout_result(proc, timeout)
    finally:
        ttyguard.CANCELLABLE["proc"] = None
    if ttyguard.CANCELLABLE["cancelled"]:
        ttyguard.CANCELLABLE["cancelled"] = False
        return _bash_cancelled_result(out, err, t0)
    return _bash_completed_result(out, err, proc.returncode)


def _timeout_arg(args: dict) -> int:
    try:
        return int(args.get("timeout") or BASH_DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        return BASH_DEFAULT_TIMEOUT_S


def tool_powershell(args: dict) -> str:
    command = (args.get("command") or "").strip()
    if not command:
        return "[error] missing 'command'"
    exe = powershell_exe()
    if exe is None:
        return "[error] no PowerShell found on PATH (looked for pwsh, powershell)"
    return _run_shell(
        [exe, "-NoProfile", "-NonInteractive", "-Command",
         PS_WRAPPER.format(command=command)],
        shell=False,
        timeout=_timeout_arg(args),
    )


def tool_bash(args: dict) -> str:
    command = (args.get("command") or "").strip()
    if not command:
        return "[error] missing 'command'"
    # Through the envelope, which owns errors="replace", CREATE_NO_WINDOW and
    # the terminal repair. popen (not run) so the HANDLE survives: run() blocks
    # with the Popen trapped inside it, which is why a runaway bash could not
    # be cancelled — the process existed and nothing could reach it.
    return _run_shell(command, shell=True, timeout=_timeout_arg(args))


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


def powershell_spec() -> dict:
    """THE ONE SCHEMA THAT IS STILL DERIVED, and the reason templating exists.

    tools/powershell.json carries `{exe}`, filled here with the interpreter
    ACTUALLY found on this box — pwsh or powershell. Freezing the resolved name
    into the file would make it wrong on any machine with the other one, which
    is the same drift moving schemas out of the source is meant to end. The
    file stays editable; the machine-specific half stays computed."""
    return tool_schemas.load("powershell", exe=powershell_exe() or "powershell")


BASH_SPEC = tool_schemas.load("bash")

READ_SPEC = tool_schemas.load("read")

WRITE_SPEC = tool_schemas.load("write")

WEB_FETCH_SPEC = tool_schemas.load("web_fetch")


def _register(ctx) -> None:
    # Windows first, and registered BEFORE bash so it leads the offered list.
    # Only when a PowerShell actually exists: a tool that cannot run is worse
    # than an absent one, because the model spends a call finding out.
    if powershell_exe() is not None:
        ctx.tool(powershell_spec(), tool_powershell, policy=SHELL_POLICY)
    ctx.tool(BASH_SPEC, tool_bash, policy=SHELL_POLICY)
    ctx.tool(READ_SPEC, tool_read, policy=READ_POLICY)
    ctx.tool(WRITE_SPEC, tool_write, policy=WRITE_POLICY)
    ctx.tool(WEB_FETCH_SPEC, tool_web_fetch, policy=NETWORK_READ_POLICY)


PLUGIN = PluginManifest(id="core-tools", critical=True, register=_register)
