"""grep + edit — the surgical file tools (Sentinel/Ryan design, 2026-08-31).

Two token sinks measured on a real port run drove this pair: whole-file
rewrites where an exact-string edit would do, and shell-quoting failures when
searching from bash-on-Windows. So grep invokes ripgrep DIRECTLY — argv list,
no shell layer to get wrong — and edit is old_string -> new_string with a
uniqueness check that errors instead of guessing.

The read-before-edit guard lives in file_state (fed by core_tools' read/write
handlers): a file must have been seen since its last change before it may be
edited, because an edit made against content the model has not looked at is a
blind write with extra steps.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from litetui import file_state, tool_schemas, ttyguard

GREP_TOOL_SPEC = tool_schemas.load("grep")
EDIT_TOOL_SPEC = tool_schemas.load("edit")

#: Default is a token budget; the ceiling exists so a model asking for
#: "everything" still gets a bounded answer.
GREP_DEFAULT_MAX_RESULTS = 200
GREP_HARD_CAP = 1000


def _resolve_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def tool_grep(args: dict) -> str:
    pattern = args.get("pattern") or ""
    if not pattern:
        return "[error] missing 'pattern'"
    mode = args.get("mode") or "content"
    if mode not in ("content", "files"):
        return f"[error] mode must be 'content' or 'files', got {mode!r}"
    try:
        max_results = int(args.get("max_results") or GREP_DEFAULT_MAX_RESULTS)
    except (TypeError, ValueError):
        max_results = GREP_DEFAULT_MAX_RESULTS
    max_results = min(max(1, max_results), GREP_HARD_CAP)

    raw_path = args.get("path") or ""
    target = _resolve_path(raw_path) if raw_path else Path.cwd()
    if not target.exists():
        return f"[error] path does not exist: {target}"

    rg = shutil.which("rg")
    if rg is None:
        return "[error] ripgrep (rg) was not found on PATH — install it or search with bash"

    # -n explicitly: ripgrep 15.x no longer prints line numbers by default
    # with this flag combo (verified against rg 15.1.0 on this box).
    argv = [rg, "-H", "--no-heading", "-n", "-e", pattern]
    glob = args.get("glob")
    if glob:
        argv += ["-g", str(glob)]
    if mode == "files":
        argv.append("--files-with-matches")
    argv.append(str(target))

    # Through the envelope: DEVNULL stdin, no console window, terminal
    # repair after spawn, and the decode discipline (UTF-8 + replace) is
    # owned by ttyguard rather than re-implemented here. text=True means
    # the pipes hand back str — readline's EOF sentinel is "" not b"".
    try:
        proc = ttyguard.popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except OSError as e:
        return f"[error] failed to start ripgrep: {type(e).__name__}: {e}"

    lines: list[str] = []
    truncated = False
    for line in iter(proc.stdout.readline, ""):
        if not line:
            break
        lines.append(line.rstrip("\r\n"))
        if len(lines) >= max_results:
            # The cap bites at EXACTLY max_results result lines. Kill rather
            # than drain: rg on a big tree would otherwise keep matching long
            # after the answer is already bounded.
            truncated = True
            proc.kill()
            break

    err = ""
    try:
        err = proc.stderr.read() or ""   # text mode: the envelope decoded it
    except Exception:  # noqa: BLE001 — stderr is best-effort after a kill
        pass
    rc = proc.wait()

    if truncated:
        # Checked BEFORE the exit code: on Windows a killed rg reports rc=1,
        # which is also ripgrep's "no matches" code. The cap already decided
        # this call found enough; the code cannot overrule it.
        out = "\n".join(lines)
        out += (f"\n\n[truncated at {max_results} results — output was cut off;"
                " narrow the pattern or raise max_results]")
        return out
    if rc >= 2:
        detail = (err or "no stderr captured").strip()
        return f"[error] ripgrep failed (exit {rc}):\n{detail}"
    if rc == 1 or not lines:
        # Exit 1 is ripgrep's way of saying "nothing matched" — a result, not
        # a failure. Reporting it as an error would make the model retry a
        # search that already answered its question.
        return f"[no matches for {pattern!r} under {target}]"

    return "\n".join(lines)


def tool_edit(args: dict) -> str:
    raw = args.get("path") or ""
    if not raw:
        return "[error] missing 'path'"
    old = args.get("old_string")
    new = args.get("new_string")
    if old is None or old == "":
        return "[error] missing 'old_string' — it must be non-empty"
    if new is None:
        return "[error] missing 'new_string'"
    replace_all = bool(args.get("replace_all", False))

    p = _resolve_path(raw)
    if not p.exists():
        return f"[error] file not found: {p}"

    # The guard runs BEFORE any content is touched: an edit against a file the
    # model has not read since its last change is a blind write.
    if not file_state.is_fresh_read(p):
        return (f"[refused] {p} has not been read since its last change — "
                "call the `read` tool on it first, then retry this edit")

    data = p.read_bytes()
    try:
        text = data.decode("utf-8")  # strict: a binary file is not an edit target
    except UnicodeDecodeError as e:
        return f"[error] {p.name} is not valid UTF-8 (byte {e.start}) — edit works on text files only"

    count = text.count(old)
    if count == 0:
        return f"[error] old_string not found in {p.name}"
    if count > 1 and not replace_all:
        return (f"[error] old_string occurs {count} times in {p.name} — "
                "make it unique (include surrounding lines) or pass replace_all=true")

    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    out_bytes = new_text.encode("utf-8")

    # Atomic: write a sibling temp file then os.replace over the target. A
    # crash mid-write leaves either the old or the new content, never a tear.
    tmp = p.with_name(p.name + ".edit-tmp")
    try:
        tmp.write_bytes(out_bytes)
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001 — report whatever the disk said
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return f"[error] write failed: {type(e).__name__}: {e}"

    # The edit's own write counts as having seen the file — demanding a re-read
    # for the very next edit would make sequential edits need a roundtrip.
    file_state.record_read(p)
    n = count if replace_all else 1
    return f"Edited {p.name}: replaced {n} occurrence(s); {len(data)} -> {len(out_bytes)} bytes"
