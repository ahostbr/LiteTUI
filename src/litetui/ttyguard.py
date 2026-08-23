"""The single envelope for every child process the LiteTUI runtime spawns.

The 2026-08-19 incident (a482737) was fixed PER SITE: DEVNULL at tool_bash,
a terminal-mode reset at one dispatch choke point, CREATE_NO_WINDOW at four
harness call sites. Per-site discipline is a convention, and conventions rot:
one new call site — or one copy-paste — away, the bug is back. This module is
the structure: every child goes through here, and test_ttyguard.py
source-scans the runtime to assert that no subprocess.run/Popen exists
anywhere else. THAT assertion is what makes the class of bug impossible
rather than merely discouraged — an envelope that can be bypassed is just a
helper everyone is supposed to remember.

What the envelope owns, and why each part is load-bearing:

  stdin     DEVNULL by default. A child that inherits the TUI's stdin reads
            the terminal itself: with mouse tracking on, SGR reports land in
            the child's reads and echo back into its stdout (the incident),
            and an interactive child steals the user's keystrokes. A child
            that must be fed gets stdin=PIPE explicitly (the MCP server).
  decode    text=True + errors="replace". Under strict decoding, one byte
            undecodable in the active locale kills the reader thread and the
            call silently returns empty output — data loss wearing the mask
            of a successful, plausible, empty result. Undecodable bytes
            become U+FFFD instead; the output survives.
  window    CREATE_NO_WINDOW on Windows: no console flash, no console attach
            for a child of a TUI that owns every cell.
  terminal  sanitize.reset_terminal_modes() AFTER the child exits — in a
            finally, so a child killed by timeout still gets the repair.
            The final terminal state is Textual's, whatever the child left
            behind.

Scope, deliberate: the TUI runtime (the top-level modules of this repo).
chrome-bridge/ and pccontrol/ are standalone tools with their own terminal
context — they are not imported by the runtime, and the scan test documents
that boundary instead of pretending it does not exist.
"""

from __future__ import annotations

import subprocess

from litetui import sanitize

#: Windows: never spawn a console window for a child of a TUI.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: A child that does not declare a timeout gets one anyway — a hung child of
#: a TUI is a frozen UI. 30s matches the harness CLI calls it replaced.
DEFAULT_TIMEOUT_S = 30


def _repair_terminal() -> None:
    """Re-assert Textual's mode set after a child exits. Never raises: a tool
    result must not fail because the terminal refused a mode sequence, and a
    redirected stdout (tests, pipes) makes the write meaningless anyway."""
    try:
        sanitize.reset_terminal_modes()
    except Exception:
        pass


def run(cmd, *, timeout=DEFAULT_TIMEOUT_S, stdin=subprocess.DEVNULL,
        cwd=None, env=None, shell=False) -> "subprocess.CompletedProcess":
    """Spawn a short-lived child under the envelope. See module docstring.

    `cmd` is an argv list (or a string when shell=True). Output is always
    captured and decoded with errors="replace". Raises
    subprocess.TimeoutExpired on timeout — the caller owns what a timeout
    means (tool_bash returns the partial output); the terminal repair still
    runs, because the child was still real.
    """
    try:
        return subprocess.run(
            cmd,
            shell=shell,
            stdin=stdin,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=env,
            creationflags=NO_WINDOW,
        )
    finally:
        _repair_terminal()


def popen(cmd, *, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
          stderr=subprocess.PIPE, cwd=None, env=None, bufsize=-1,
          encoding="utf-8", errors="replace", shell=False) -> "subprocess.Popen":
    """Spawn a LONG-LIVED child under the envelope — the case is the stdio
    MCP server — same window/decode discipline, but the caller owns the
    pipes and the process (the envelope cannot, so it must not, reap it).
    The terminal repair runs once, immediately after the spawn.
    """
    proc = subprocess.Popen(
        cmd,
        shell=shell,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        cwd=cwd,
        env=env,
        text=True,
        bufsize=bufsize,
        encoding=encoding,
        errors=errors,
        creationflags=NO_WINDOW,
    )
    _repair_terminal()
    return proc


#: The ONE cancellable child. Tools execute sequentially in the agent loop,
#: so a single slot is the honest data structure — a registry keyed by call
#: id would imply a concurrency the loop does not have. It lives in the
#: envelope because child-process lifecycle is the envelope's charter: the
#: bash tool writes the slot, the app's cancel button reads it.
CANCELLABLE: dict = {"proc": None, "cancelled": False}


def kill_tree(pid: int) -> None:
    """Kill pid and its DESCENDANTS. shell=True means the direct child is
    cmd.exe and the real work is its grandchild — proc.kill() would kill
    cmd.exe and leave python running, detached and invisible. taskkill /T
    walks the tree; /F because a cancel that asks nicely is a suggestion."""
    try:
        run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=15)
    except Exception:
        pass  # the process may already be gone — that is success, not failure
