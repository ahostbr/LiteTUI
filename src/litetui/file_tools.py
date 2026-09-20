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
    """Serialize cooperating read-check-replace transactions on a stable sibling."""
    from litetui.shared_state import coordinated_write
    raw = args.get('path') or ''
    if not raw:
        return "[error] missing 'path'"
    try:
        with coordinated_write(_resolve_path(raw)):
            return _edit_locked(args)
    except OSError as exc:
        return f'[error] edit could not acquire or commit file transaction: {exc}'


def _edit_locked(args: dict) -> str:
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

    identity = p.stat()
    data = p.read_bytes()
    try:
        text = data.decode("utf-8")  # strict: a binary file is not an edit target
    except UnicodeDecodeError as e:
        return f"[error] {p.name} is not valid UTF-8 (byte {e.start}) — edit works on text files only"

    count = text.count(old)
    normalized = False
    bare_lf = 0
    if count == 0 and "\r\n" in text:
        # \U0001F534 THE MODEL TYPES WHAT `read` SHOWS IT — LF ENDINGS. A CRLF file therefore
        # never exact-matches an old_string the model composed from what it was shown (T147,
        # measured 2026-09-01: a full session of edits against this repo's pure-CRLF files
        # died with "old_string not found" while every string WAS in the file). Retry the
        # match in normalized LF space; the write-back below restores the file's own ending.
        base = text.replace("\r\n", "\n")
        old_s, new_s = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        bare_lf = text.replace("\r\n", "").count("\n")
        normalized = True
    else:
        base, old_s, new_s = text, old, new

    count = base.count(old_s)
    if count == 0:
        return f"[error] old_string not found in {p.name}"
    if count > 1 and not replace_all:
        return (f"[error] old_string occurs {count} times in {p.name} — "
                "make it unique (include surrounding lines) or pass replace_all=true")

    new_text = base.replace(old_s, new_s) if replace_all else base.replace(old_s, new_s, 1)
    # Write back in the file's own ending. The normalized path only exists for CRLF files:
    # a uniformly-CRLF one (no bare LF anywhere) converts the whole result to CRLF; MIXED
    # endings are refused with both counts — converting would re-encode every bare-LF line
    # the edit did not touch, a silent history rewrite no one asked for. Exact-match edits
    # never reach this: their bytes pass through as-is.
    if normalized and bare_lf:
        return (f"[error] {p.name} has MIXED line endings "
                f"({text.count(chr(13) + chr(10))} CRLF, {bare_lf} bare LF) — edit will not "
                "re-encode lines the edit did not touch; normalize the file to one ending first")
    out_bytes = (new_text.replace("\n", "\r\n") if normalized else new_text).encode("utf-8")

    # Atomic: write a sibling temp file then os.replace over the target. A
    # crash mid-write leaves either the old or the new content, never a tear.
    import tempfile
    tmp = None
    try:
        fd, name = tempfile.mkstemp(dir=p.parent, prefix='.' + p.name + '.edit-', suffix='.tmp')
        tmp = Path(name)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(out_bytes)
        current = p.stat()
        if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (identity.st_dev, identity.st_ino, identity.st_mtime_ns, identity.st_size) or p.read_bytes() != data:
            raise OSError('file changed during edit; read again before retry')
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001 — report whatever the disk said
        return f"[error] write failed: {type(e).__name__}: {e}"
    finally:
        try:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass

    # The edit's own write counts as having seen the file — demanding a re-read
    # for the very next edit would make sequential edits need a roundtrip.
    file_state.record_read(p)
    n = count if replace_all else 1
    return f"Edited {p.name}: replaced {n} occurrence(s); {len(data)} -> {len(out_bytes)} bytes"
