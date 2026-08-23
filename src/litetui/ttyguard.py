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
import time

from litetui import jobkill

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
          encoding="utf-8", errors="replace", shell=False,
          kill_on_close=False) -> "subprocess.Popen":
    """Spawn a LONG-LIVED child under the envelope — the case is the stdio
    MCP server — same window/decode discipline, but the caller owns the
    pipes and the process (the envelope cannot, so it must not, reap it).
    The terminal repair runs once, immediately after the spawn.

    `kill_on_close` puts the child in a Job Object so its whole tree can be
    killed by closing one handle instead of walking the process table. It is
    OPT-IN AND DEFAULTS OFF ON PURPOSE.

    🔴 THIS FUNCTION IS ONE IMPLEMENTATION WITH FOUR CONSUMERS, AND THREE OF
    THEM MUST NOT GET THIS BEHAVIOUR:

        core_tools  the cancellable bash/powershell tool   <- the only opt-in
        mcp_client  the MCP stdio server                   <- must outlive a call
        llm_backend the LM Studio router                   <- must outlive a call
        app         the mark overlay                       <- not cancellable

    A job created with KILL_ON_JOB_CLOSE takes its members with it whenever
    the last handle closes — INCLUDING ON ORDINARY GARBAGE COLLECTION of the
    handle. Turning this on for everyone would kill the MCP server and the
    model router at unpredictable moments, and it would look like anything
    except a cancel bug. A single spawn SITE is not the same as a single set
    of spawn SEMANTICS.
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
    if kill_on_close:
        # ⚠️ THE CHILD, NEVER OURSELVES. Assigning this process to a
        # KILL_ON_JOB_CLOSE job kills the app when the handle closes.
        job = jobkill.create()
        # Attached to the Popen so the handle's lifetime is the child's, and
        # so kill_tree can find it without a second registry to keep in step.
        proc._litetui_job = job if jobkill.assign(job, proc.pid) else None
        if job is not None and proc._litetui_job is None:
            jobkill.close(job)   # assign failed: do not leak the handle
    _repair_terminal()
    return proc


#: The ONE cancellable child. Tools execute sequentially in the agent loop,
#: so a single slot is the honest data structure — a registry keyed by call
#: id would imply a concurrency the loop does not have. It lives in the
#: envelope because child-process lifecycle is the envelope's charter: the
#: bash tool writes the slot, the app's cancel button reads it.
CANCELLABLE: dict = {"proc": None, "cancelled": False, "kill_confirmed": True}


#: taskkill's own exit codes, MEASURED on this machine rather than read from a
#: doc: killing a live tree gives 0, and BOTH "already exited" and "no such
#: pid" give 128. 128 is therefore the outcome we wanted, reached early.
TASKKILL_OK = 0
TASKKILL_NOT_FOUND = 128

#: How long taskkill may take before we stop waiting. Measured against the
#: cmd.exe -> python tree this app actually spawns: min 3.42s, median 6.38s,
#: max 43.06s, and 1 in 10 over 15s on a loaded box.
#:
#: The budget is deliberately NOT raised to cover that tail. It runs on the
#: caller's thread, so a longer budget buys a longer freeze, and no budget can
#: cover a distribution with no upper bound. The bound stays; what changed is
#: that overrunning it is now REPORTED instead of swallowed.
KILL_TREE_TIMEOUT_S = 15

#: How long to wait for the kernel to finish terminating a job's members
#: before falling back. Job termination is asynchronous but was measured at
#: 0.1-0.2 ms across 15 trials, so this is ~10,000x the observed cost and
#: exists only so a pathological case degrades to the walk instead of lying.
JOB_KILL_CONFIRM_S = 2.0


def kill_tree(pid: int, proc=None) -> bool:
    """Kill pid and its DESCENDANTS. True only if the tree is CONFIRMED gone.

    shell=True means the direct child is cmd.exe and the real work is its
    grandchild — proc.kill() would kill cmd.exe and leave python running,
    detached and invisible. taskkill /T walks the tree; /F because a cancel
    that asks nicely is a suggestion.

    🔴 THIS RETURNED None AND SWALLOWED EVERY EXCEPTION, under the comment
    "the process may already be gone — that is success, not failure". That
    sentence is true of most exceptions and FALSE OF A TIMEOUT, which is the
    one case where it decides something. A timeout means the walk is
    UNFINISHED: subprocess kills taskkill mid-walk, so the tree may still be
    running — and every caller went on to tell the user the cancel worked.
    Measured: 1 of 10 kills of this app's own tree exceeded the 15s budget,
    max 43.06s. On a loaded box, cancel silently failed while reporting
    "[cancelled by user after Ns]" to the user AND to the model.

    ⚠️ A SECOND ROUTE TO THE SAME LIE, in the same line: run() is called
    without check=, so a taskkill that RAN AND FAILED (access denied, say)
    raised nothing either. The returncode is now inspected, not ignored.

    Returning False does not mean the tree is alive — it means WE DO NOT KNOW.
    That is the honest state, and it is the one every caller must refuse to
    describe as success.
    """
    # FAST PATH: the child was spawned into a job, so the whole tree dies
    # when the handle closes. 0.1-0.2ms, no walk, nothing to time out.
    job = getattr(proc, "_litetui_job", None) if proc is not None else None
    if job is not None:
        if jobkill.close(job):
            proc._litetui_job = None      # closed exactly once
            # CONFIRM rather than assume: this function promises that True
            # means the tree is gone, and a promise needs a check. Termination
            # is asynchronous, but measured at 0.1-0.2ms, so this bound is
            # enormous by comparison and still cheap — alive() is an
            # OpenProcess, not a process-table enumeration.
            deadline = time.monotonic() + JOB_KILL_CONFIRM_S
            while jobkill.alive(pid) and time.monotonic() < deadline:
                time.sleep(0.002)
            if not jobkill.alive(pid):
                return True
        # The job path did not confirm the kill. Fall through and walk.

    # FALLBACK, DELIBERATELY CONDITIONAL. Running this after every successful
    # job kill would be simpler and has a branch fewer — and it would cost a
    # median 6,568ms EVERY TIME. taskkill on an ALREADY-DEAD pid measured
    # 3,678 / 6,568 / 10,393 ms (n=6), statistically the same as on a live
    # tree, because the expense is enumerating the process table rather than
    # killing anything. An unconditional fallback would throw away the whole
    # benefit of the fast path.
    try:
        completed = run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            timeout=KILL_TREE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False   # unfinished walk; taskkill was killed mid-tree
    except Exception:
        return False   # no taskkill on PATH, no permission: not confirmed
    return completed.returncode in (TASKKILL_OK, TASKKILL_NOT_FOUND)
