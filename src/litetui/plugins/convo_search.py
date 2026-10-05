"""convo_search — fleet-ranked search over every LiteHarness conversation.

The engine is the standalone CLI `tools/convo_search.py` (T888): a stdlib-only
SQLite FTS5 index over owned .agents/<Name>/conversations transcripts plus
read-only legacy archives, maintained incrementally under the configured data
root. Every lookup refreshes the catalog once; source files are never mutated.
This plugin dispatches into that CLI without inventing a second storage root.
Administrative rebuilds (--reindex) are not exposed as tool parameters.
"""
import subprocess
import sys

from litetui import paths, tool_schemas, ttyguard
from litetui.plugins import PluginManifest
from litetui.tool_policy import READ_POLICY

CLI = paths.ROOT / "tools" / "convo_search.py"
TIMEOUT = 120  # a first-run index build is ~10s; steady-state searches are ~1s
MAX_LINES = 2000
MAX_CHARS = 50_000


def _truncate(out: str) -> str:
    lines = out.splitlines()
    cut = ""
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        cut = f"\n…(truncated at {MAX_LINES} lines)"
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS]
        cut = f"\n…(truncated at {MAX_CHARS // 1000}KB)"
    return out + cut


def _run(args: dict) -> str:
    if not CLI.exists():
        return f"[error] convo_search: CLI missing at {CLI}"
    cmd = [sys.executable, str(CLI), '--root', str(paths.data_root()), '--index']
    if args.get("stats"):
        cmd.append("--stats")
    elif args.get("show"):
        cmd += ["--show", str(args["show"])]
    elif args.get("raw"):
        pattern = str(args.get("query") or "")
        if not pattern:
            return "[error] convo_search: raw=<convo> requires query=<regex>"
        cmd += ["--raw", str(args["raw"]), pattern]
    else:
        query = str(args.get("query") or "").strip()
        if not query:
            return "[error] convo_search: a query is required (or show=/raw=/stats=true)"
        cmd.append(query)
        if args.get("agent"):
            cmd += ["--agent", str(args["agent"])]
        if args.get("since"):
            cmd += ["--since", str(args["since"])]
        if args.get("limit"):
            cmd += ["--limit", str(args["limit"])]
        if args.get("messages"):
            cmd.append("--messages")
    try:
        # THE ENVELOPE, NOT A RAW SPAWN. ttyguard.run captures and decodes
        # (errors="replace") exactly as the call it replaces did, and the
        # child cannot inherit the TUI's console. test_ttyguard scans every
        # plugin for a bare subprocess.run - it caught this one on the first
        # full-suite run after the merge (c8d74f6).
        proc = ttyguard.run(cmd, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"[error] convo_search: timed out after {TIMEOUT}s"
    out = proc.stdout or ""
    if proc.returncode != 0:
        out = (out + "\n" + (proc.stderr or "")).strip()
        return f"[error] convo_search exit {proc.returncode}: {out}"
    return _truncate(out) or "(no output)"


def _register(ctx) -> None:
    ctx.tool(tool_schemas.load("convo_search"), _run, policy=READ_POLICY)


PLUGIN = PluginManifest(id="convo_search", register=_register)
