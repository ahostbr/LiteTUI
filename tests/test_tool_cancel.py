"""The tool cancel button — verified against the PROCESS TABLE, not the UI.

Docs/spec-tool-cancel.md names the two traps this feature must not fall into,
and each has a test arm here:

  1. asyncio.to_thread cannot be cancelled — so cancel must reach the PROCESS.
     The positive arm asserts the pid is GONE from the process table, not that
     a bubble stopped updating.
  2. shell=True makes cmd.exe the child and the real work its GRANDCHILD —
     proc.kill() orphans it. The tree arm asserts the grandchild died too.

Without the negative arm (same command, no cancel, still alive then completes
normally) the positive arm would pass for a process that merely exits.

🔴 HOW THESE TESTS IDENTIFY THE GRANDCHILD, AND WHY IT CHANGED (2026-08-23).
They used to find it by scanning the whole process table for a marker in the
command line. That instrument cost 5.8-25.6 s per call, the tests poll it, and
its 30 s subprocess budget was not caught — so on a loaded box the
TimeoutExpired escaped the poll and the test died at ~31 s having NEVER REACHED
the assertion it exists to make. Measured 4 failures in 5 runs. A test that
cannot reach its assertion reports the product as broken when the instrument
is.

The probe now ANNOUNCES ITS OWN PID and the tests hold an open HANDLE to it:

    handle open + WaitForSingleObject      < 1 ms
    Get-CimInstance Win32_Process        5800 - 25600 ms

Three things that buys, beyond speed:
  * The pid comes from the process itself, which proves it reached its first
    statement — better evidence than a string matching its command line.
  * A pidfile SURVIVES the process. The old table poll could only observe a
    LIVE probe, which is why the timeout arm carried a documented latent race
    (the probe was observable for ~2 s before the tool's own timeout killed
    it). A file cannot be missed by arriving late. That race is now closed.
  * Holding the handle pins the pid: Windows will not recycle a pid while a
    handle to that process object is open, so "this pid exited" can never be
    confused with "something else took the pid".

The old query also had to exclude ITS OWN powershell process, because a query
for MARKER matches the process making the query — the same trap as the grep
that matched our own conversation. That trap is gone with the query, but it is
worth remembering the next time something searches for a string it is itself
carrying.
"""
import ast
import ctypes
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import ttyguard
from litetui.plugins import core_tools
from litetui.app import LiteTUI

_SRC = Path(__file__).resolve().parent.parent / "src" / "litetui"
# The subjects moved in the plugin split: tool_bash lives in the core-tools
# plugin, kill_tree in the envelope. A gate reads the source that HOLDS its subject.
CORE_TOOLS_SRC = (_SRC / "plugins" / "core_tools.py").read_text(encoding="utf-8")
TTYGUARD_SRC = (_SRC / "ttyguard.py").read_text(encoding="utf-8")
# app.py holds action_cancel_tool — the guard under test in this file.
APP_SRC = (_SRC / "app.py").read_text(encoding="utf-8")


# ── the instrument ───────────────────────────────────────────────────────────

_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0x00000000

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


class _Pinned:
    """An OPEN HANDLE to a process, so its pid cannot be recycled while we
    assert on it.

    A pid alone is not an identity on Windows — it can be reused the moment
    the process object is freed, and "the pid is still there" would then be
    answered by an unrelated process. An open handle keeps the object alive,
    so exited() is a statement about THIS process and no other.

    A pid that is already gone yields no handle. That is not an error: it is
    the answer `exited() is True`, and it is how the timeout arm can pin a
    probe the tool has already killed.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._h = _k32.OpenProcess(
            _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)

    def exited(self) -> bool:
        if not self._h:
            return True
        return _k32.WaitForSingleObject(self._h, 0) == _WAIT_OBJECT_0

    def close(self) -> None:
        if self._h:
            _k32.CloseHandle(self._h)
            self._h = None


def _probe(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """A probe script that announces its own pid, then does `body`.

    The write is atomic (write-then-replace): the tests poll for this file,
    and a reader that caught a half-written pid would be a NEW flake traded
    for the one being removed.
    """
    name = f"CANCELPROBE_{uuid.uuid4().hex[:12]}"
    script = tmp_path / f"{name}.py"
    pidfile = tmp_path / f"{name}.pid"
    script.write_text(
        "import os\n"
        f"_pf = {str(pidfile)!r}\n"
        "open(_pf + '.tmp', 'w').write(str(os.getpid()))\n"
        "os.replace(_pf + '.tmp', _pf)\n"
        + body,
        encoding="utf-8",
    )
    return script, pidfile


def _reap(*pinned: _Pinned) -> None:
    """Kill anything still running, then release the handles. Every arm's
    `finally`.

    A test that dies mid-way used to leave its probe sleeping out the full
    300s. On a SHARED box that is not untidiness: it inflates the process
    table that everyone else's measurements are taken against, and the
    flakiness it causes looks like the defect under test rather than like
    litter. Measured 2026-08-23 — twelve of these were alive across three
    worktrees at one point, and an instruction to "clean up the leaked
    processes" nearly killed an in-flight measurement instead.

    The pid is exact: the probe reported it itself and the handle has PINNED
    it, so this cannot kill a stranger that inherited the number.

    🔴 Deliberately NOT ttyguard.kill_tree — cleanup must not depend on the
    code under test, and kill_tree's 15s budget is itself the defect this
    file is currently reporting. Failure here is swallowed because nothing
    reads the outcome and the fallback is the probe's own timeout, i.e.
    exactly today's behaviour. That is not the same swallow as kill_tree's:
    no one is being TOLD this succeeded.
    """
    for p in pinned:
        try:
            if not p.exited():
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            p.close()


def _pinned_probe(pidfile: Path) -> _Pinned:
    """Wait for the probe to announce itself, then pin it."""
    box: dict = {}

    def announced() -> bool:
        try:
            box["pid"] = int(pidfile.read_text().strip())
            return True
        except (OSError, ValueError):
            return False

    assert _wait(announced), f"probe never announced its pid at {pidfile.name}"
    return _Pinned(box["pid"])


@pytest.fixture(autouse=True)
def _clean_slot():
    ttyguard.CANCELLABLE["proc"] = None
    ttyguard.CANCELLABLE["cancelled"] = False
    yield
    ttyguard.CANCELLABLE["proc"] = None
    ttyguard.CANCELLABLE["cancelled"] = False


def _run_bash_in_thread(args):
    box = {}

    def target():
        box["result"] = core_tools.tool_bash(args)

    th = threading.Thread(target=target, daemon=True)
    th.start()
    return th, box


def _wait(cond, ceiling=120.0, delay=0.1):
    """Poll cond until it holds or the wall-clock ceiling passes.

    The first draft budgeted 100 tries x 0.1s — a fixed 10-second sleep in
    disguise, assuming Windows schedules the probe inside 10s. A loaded box
    misses that for reasons unrelated to what these tests measure. The ceiling
    is generous because it is only ever PAID on failure — a passing poll
    returns the moment the condition holds, and now that each probe costs
    microseconds rather than seconds, `delay` is the real polling interval
    instead of an aspiration.
    """
    deadline = time.monotonic() + ceiling
    while True:
        if cond():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(delay)


def test_cancel_kills_the_whole_tree_and_the_turn_survives(tmp_path):
    """POSITIVE + TREE ARMS. The grandchild python must be GONE from the
    process table, and tool_bash must RETURN (an honest string) rather than
    hang — the turn carries on, which is the difference from Esc."""
    script, pidfile = _probe(
        tmp_path, "import time\nprint('probe up', flush=True)\ntime.sleep(300)\n")
    th, box = _run_bash_in_thread(
        {"command": f'"{sys.executable}" "{script}"', "timeout": 240})

    assert _wait(lambda: ttyguard.CANCELLABLE["proc"] is not None), \
        "tool_bash never populated the cancellable slot"
    proc = ttyguard.CANCELLABLE["proc"]
    grandchild = _pinned_probe(pidfile)
    shell_child = _Pinned(proc.pid)
    try:
        # The PREMISE of this whole test, asserted instead of assumed: with
        # shell=True the pid we kill is not the pid doing the work. If these
        # were ever the same process, the tree arm below would prove nothing
        # and would still pass.
        assert grandchild.pid != proc.pid, \
            "no intermediate shell — trap 2 did not reproduce, so the tree " \
            "assertion below would be vacuous"
        assert not grandchild.exited(), "probe died before it could be cancelled"

        # The exact core of action_cancel_tool + _cancel_tool_tree, minus the
        # notifies.
        #
        # 🔴 IT MUST PASS proc, NOT JUST THE PID, and this line is why the
        # claim above needs re-checking whenever production moves. It read
        # `kill_tree(proc.pid)` and called itself "the exact core of
        # action_cancel_tool" — true when written, silently false the moment
        # the app started passing the Popen. kill_tree's fast path is the job
        # handle hanging off that object, so a pid-only call took the taskkill
        # walk the app no longer takes: this test was exercising a path
        # production had abandoned, while asserting it was production's.
        ttyguard.CANCELLABLE["cancelled"] = True
        killed = ttyguard.kill_tree(proc.pid, proc)
        ttyguard.CANCELLABLE["kill_confirmed"] = killed
        assert killed is True, \
            "kill_tree could not CONFIRM the tree is gone — False does not " \
            "mean it is alive, it means we do not know, and no caller may " \
            "describe that as success"

        th.join(timeout=30)
        assert not th.is_alive(), "tool_bash did not return after the kill"
        assert box["result"].startswith("[cancelled by user after"), box["result"]
        # partial output written before the kill is preserved, not discarded
        assert "probe up" in box["result"]
        assert _wait(lambda: grandchild.exited()), \
            "GRANDCHILD SURVIVED — the tree kill missed the real work"
        assert _wait(lambda: shell_child.exited()), \
            "the shell itself survived the kill aimed straight at it"
    finally:
        _reap(grandchild, shell_child)


def test_negative_arm_no_cancel_means_normal_completion(tmp_path):
    """Without this, the test above measures only that processes eventually
    exit. Same shape, no cancel: alive while running, normal result after.

    The probe HOLDS until the test releases it. An earlier draft slept a fixed
    2s and the aliveness check lost the race on a loaded box — the probe came
    and went between two observations, and "probe never started" fired for a
    probe that had run fine. The release file makes the alive WINDOW
    deterministic rather than a matter of timing; the pidfile makes the START
    unmissable even if the observation is late. The probe's own deadline only
    bounds a crashed test run, it is never waited out."""
    name = f"release_{uuid.uuid4().hex[:8]}"
    release = tmp_path / name
    script, pidfile = _probe(
        tmp_path,
        "import os, time\n"
        f"release = {str(release)!r}\n"
        "deadline = time.monotonic() + 240\n"
        "while not os.path.exists(release) and time.monotonic() < deadline:\n"
        "    time.sleep(0.1)\n"
        "print('done cleanly')\n",
    )
    th, box = _run_bash_in_thread(
        {"command": f'"{sys.executable}" "{script}"', "timeout": 240})

    probe = _pinned_probe(pidfile)
    try:
        assert not probe.exited(), \
            "the probe exited before the test released it — it is not holding"
        release.write_text("go")
        th.join(timeout=60)
        assert not th.is_alive()
        assert "done cleanly" in box["result"]
        assert "[cancelled" not in box["result"]
        assert ttyguard.CANCELLABLE["proc"] is None  # slot cleared on the way out
    finally:
        _reap(probe)


def test_timeout_now_kills_the_tree_too(tmp_path):
    """The old run() path timed out and LEFT THE TREE RUNNING (documented in
    the spec as the 3m45s runaway). The popen path tree-kills on timeout.

    This arm used to carry a latent race: the probe was observable in the
    process table for only ~2s before the tool's own timeout killed it, so a
    slow observation reported "probe never started" for a probe that started
    fine. The pidfile is written by the probe and OUTLIVES it, so arriving
    late can no longer be mistaken for never arriving."""
    script, pidfile = _probe(tmp_path, "import time\ntime.sleep(300)\n")
    th, box = _run_bash_in_thread(
        {"command": f'"{sys.executable}" "{script}"', "timeout": 2})

    probe = _pinned_probe(pidfile)
    try:
        th.join(timeout=60)
        assert not th.is_alive()
        assert box["result"].startswith("[timed out after 2s]")
        assert _wait(lambda: probe.exited()), \
            "timeout reported but the tree is still running — the old lie"
    finally:
        _reap(probe)


# --- the action's guard, no app needed ---------------------------------------
def test_action_with_nothing_running_is_an_honest_no_op():
    notes = []
    ns = SimpleNamespace(notify=lambda msg, timeout=0: notes.append(msg))
    LiteTUI.action_cancel_tool(ns)
    assert notes and "No cancellable tool" in notes[0]
    assert ttyguard.CANCELLABLE["cancelled"] is False   # nothing armed



# --- the stuck state: shell dead, tree still holding the pipe -----------
def test_cancel_works_when_the_shell_died_but_the_tree_holds_the_pipe(tmp_path):
    """THE BUG AS REPORTED. With shell=True the direct child is cmd.exe; it can
    exit while a grandchild it spawned still holds the stdout/stderr pipes, so
    communicate() stays blocked and proc.poll() says the child is DEAD. The old
    guard (`proc is None or proc.poll() is not None`) answered "No cancellable
    tool is running" about a call that WAS running — with the button visible for
    it, because the ticker keys on the slot, not poll(). This arm builds exactly
    that state and proves cancel still reaches the tree.

    The probe spawns a sleeper that INHERITS stdout (no redirect), announces the
    SLEEPER's pid — its own, atomically — then exits. cmd.exe follows it out; the
    sleeper alone keeps the pipe open. A pidfile written by _probe would race: it
    first holds the probe's own pid, and a late reader could pin the wrong process.
    """
    name = f"CANCELSTUCK_{uuid.uuid4().hex[:12]}"
    script = tmp_path / f"{name}.py"
    pidfile = tmp_path / f"{name}.pid"   # holds the SLEEPER's pid, not the probe's
    script.write_text(
        "import os, subprocess, sys\n"
        f"_pf = {str(pidfile)!r}\n"
        "_s = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "open(_pf + '.tmp', 'w').write(str(_s.pid))\n"
        "os.replace(_pf + '.tmp', _pf)\n",
        encoding="utf-8",
    )
    th, box = _run_bash_in_thread(
        {"command": f'"{sys.executable}" "{script}"', "timeout": 240})

    assert _wait(lambda: ttyguard.CANCELLABLE["proc"] is not None), \
        "tool_bash never populated the cancellable slot"
    proc = ttyguard.CANCELLABLE["proc"]
    shell = _Pinned(proc.pid)
    sleeper = _pinned_probe(pidfile)
    try:
        assert sleeper.pid != proc.pid, \
            "no intermediate shell — the grandchild trap did not reproduce"

        # THE stuck state, asserted instead of assumed: child dead (poll() non-None),
        # slot still populated, thread still blocked in communicate(). If any one of
        # these fails to hold, the scenario under test never formed and every later
        # assertion would pass vacuously.
        assert _wait(
            lambda: proc.poll() is not None and ttyguard.CANCELLABLE["proc"] is not None), (
            "stuck state never formed — cmd.exe died but something unblocked "
            "communicate, so the grandchild trap did not reproduce"
        )
        assert th.is_alive(), \
            "tool_bash returned without a cancel — the pipe was not held"
        assert not sleeper.exited(), "sleeper died before it could hold the pipe"

        notes = []
        calls = []
        killed_box: dict = {}

        def cancel_tree(pid, proc_arg=None):
            """The exact core of _cancel_tool_tree minus the notifies — same
            pattern as the tree arm above, and it must pass proc, not just pid.
            """
            killed_box["v"] = ttyguard.kill_tree(pid, proc_arg)
            ttyguard.CANCELLABLE["kill_confirmed"] = killed_box["v"]
            calls.append((pid, proc_arg))

        ns = SimpleNamespace(
            notify=lambda msg, timeout=0: notes.append(msg), _cancel_tool_tree=cancel_tree)
        LiteTUI.action_cancel_tool(ns)

        assert not any("No cancellable tool" in n for n in notes), \
            f"the guard read child liveness again — the bug this fix removed: {notes}"
        assert ttyguard.CANCELLABLE["cancelled"] is True, "cancel was not armed"
        assert calls and calls[0] == (proc.pid, proc), \
            "the kill did not receive pid AND the Popen object"

        th.join(timeout=60)
        assert not th.is_alive(), "tool_bash did not return after the kill"
        assert box["result"].startswith("[cancelled by user after"), box["result"]
        assert killed_box.get("v") is True, (
            "kill_tree could not CONFIRM the tree is gone — no caller may describe "
            "that as success"
        )
        assert _wait(lambda: sleeper.exited()), \
            "the pipe-holder survived — communicate could never have unblocked"
    finally:
        _reap(sleeper, shell)


def test_a_dead_child_in_the_slot_does_not_block_cancel():
    """REGRESSION PIN for the removed `proc.poll() is not None` clause. A dead
    Popen in the slot is exactly what the old guard rejected with "No cancellable
    tool is running" — about a call it was supposed to cancel. The contract this
    pins: the SLOT is the in-flight signal, whatever is in it gets cancelled.
    Fails under the pre-fix code; that is its whole job.
    """
    notes = []
    calls = []
    ns = SimpleNamespace(
        notify=lambda msg, timeout=0: notes.append(msg),
        _cancel_tool_tree=lambda pid, proc=None: calls.append((pid, proc)))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)   # poll() is now non-None — the old guard's trigger
    ttyguard.CANCELLABLE["proc"] = dead
    LiteTUI.action_cancel_tool(ns)
    assert not any("No cancellable tool" in n for n in notes), \
        f"child liveness is back in the guard: {notes}"
    assert ttyguard.CANCELLABLE["cancelled"] is True, "cancel was not armed"
    assert calls and calls[0][0] == dead.pid and calls[0][1] is dead

# --- the instrument's own gate -----------------------------------------------
def test_the_pin_can_tell_a_live_process_from_a_dead_one():
    """The control that stops this file's speed from being a lie. An
    instrument that answers "exited" for everything is instant AND useless:
    every arm above would pass with the product completely broken. So prove
    both answers against processes whose state we already know."""
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pinned = _Pinned(live.pid)
    try:
        assert pinned.exited() is False, "a running process read as exited"
        live.kill()
        live.wait(timeout=30)
        assert _wait(lambda: pinned.exited()), "a killed process read as alive"
    finally:
        pinned.close()
    # A pid that never existed must not read as alive either.
    assert _Pinned(0x7FFFFFF0).exited() is True


# --- source gates ------------------------------------------------------------
def test_bash_goes_through_popen_not_run():
    """run() blocks with the Popen trapped inside it — the handle is the
    feature. If bash drifts back to run(), cancel silently dies."""
    # The spawn moved into _run_shell (2026-08-22) when the powershell tool
    # landed: two copies of the cancellation, truncation and exit-code
    # reporting would be two things to keep in step. The claim is unchanged --
    # it is asserted on the function that now owns the handle, and on BOTH
    # shells reaching it, which is strictly more than this covered before.
    body = CORE_TOOLS_SRC.split("def _run_shell(", 1)[1].split("\ndef ", 1)[0]
    assert "ttyguard.popen(" in body
    assert "ttyguard.run(" not in body
    for fn in ("def tool_bash(", "def tool_powershell("):
        shell_body = CORE_TOOLS_SRC.split(fn, 1)[1].split(chr(10) + "def ", 1)[0]
        assert "_run_shell(" in shell_body, (
            fn + " does not go through the cancellable spawn"
        )


def test_the_kill_is_a_tree_kill():
    """The kill must be a TREE kill — counted as a STATEMENT, not as a string.

    🔴 WHY THIS IS NOT A GREP. The obvious gate is `"/T" in body`, and it was
    that until 2026-08-23. It passes with the flag REMOVED, as long as
    anything in the function merely MENTIONS it: measured by deleting /T from
    the argv and leaving `# MUTATION: no /T` behind — the gate stayed green
    while the tree kill was gone and both tree arms of this file failed. A
    gate defeated by a comment reports the invariant forever, and the first
    person to run it concludes it holds. (Same shape as tools/
    tool_door_gate.py, where the second "door" was a docstring describing the
    door.)

    The AST carries no comments, so a sentence about the flag cannot be
    mistaken for the flag.
    """
    tree = ast.parse(TTYGUARD_SRC)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "kill_tree"), None)
    assert fn is not None, "ttyguard.kill_tree is gone"

    # Every argv list literal inside kill_tree that is a taskkill invocation.
    argvs = [
        [e.value for e in node.elts
         if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        for node in ast.walk(fn)
        if isinstance(node, ast.List) and node.elts
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == "taskkill"
    ]
    assert argvs, "kill_tree no longer builds a taskkill argv"
    for flag, why in (
        ("/T", "walk the TREE — without it the grandchild is orphaned, "
               "which is trap 2 in this file's docstring"),
        ("/F", "force — a cancel that asks nicely is a suggestion"),
    ):
        assert any(flag in argv for argv in argvs), \
            f"taskkill lost {flag}: {why}"


def test_the_guard_never_reads_child_liveness():
    """AST gate on the guard itself: zero .poll() calls in action_cancel_tool's
    body. The defect this file's new arms pin was exactly that clause — a liveness
    read inside a guard whose only job is "is a call in flight". A string grep
    would pass with the clause commented out; the AST counts calls, and comments
    are not calls.
    """
    tree = ast.parse(APP_SRC)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "action_cancel_tool"), None)
    assert fn is not None, "app.action_cancel_tool is gone"
    polls = [node for node in ast.walk(fn)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "poll"]
    assert not polls, (
        f"{len(polls)} .poll() call(s) in action_cancel_tool — child liveness is "
        "back in the guard; the slot alone decides"
    )
