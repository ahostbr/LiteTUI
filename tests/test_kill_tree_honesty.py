"""A cancel that did not work must never be reported as one.

🔴 THE DEFECT THIS PINS. `ttyguard.kill_tree` ran taskkill under a 15s budget
and swallowed every exception under the comment *"the process may already be
gone - that is success, not failure"*. That sentence is true of most
exceptions and FALSE OF A TIMEOUT, which is the one case where it decides
anything: a timeout means the walk is UNFINISHED, subprocess kills taskkill
mid-tree, and the tree may still be running. The function returned None, so no
caller could tell, and all three report sites went on to announce a successful
cancel.

Measured against the cmd.exe -> python tree this app actually spawns:
min 3.42s, median 6.38s, max 43.06s, 1 in 10 over the 15s budget. On a loaded
box, pressing cancel silently failed while the user was told
"[cancelled by user after Ns]" - and so was the model.

⏱️ WHY THERE IS NO `n` IN THIS FILE, AND WHY THAT IS THE POINT.
The natural test - kill a real tree and see - CANNOT SEE THIS DEFECT.
`test_timeout_now_kills_the_tree_too` is 10/10 green in isolation at
6.75-10.08s and fails inside the full suite, because taskkill only overruns
when the box is BUSY. Sampling it means sampling a load distribution that
changes under you: an AFTER run on a quiet box certifies nothing.

So this file does not sample. It INJECTS the timeout and asserts the reported
outcome - binary, deterministic, load-independent. Same move as forcing the
budget to 0.001s to prove the defect existed, and the same escape OpenBolt
used when his interleaved 2/20 vs 0/20 came out at Fisher p=0.24: when the
statistics cannot reach significance, stop arguing about the sample and prove
the mechanism.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from litetui import jobkill, ttyguard
from litetui.plugins import core_tools


class _Completed:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _fake_run(result):
    """Stand in for ttyguard.run. `result` is a returncode or an exception."""
    def run(cmd, **kw):
        if isinstance(result, BaseException):
            raise result
        return _Completed(result)
    return run


# ── the door itself ─────────────────────────────────────────────────────

def test_a_TIMEOUT_is_not_reported_as_a_successful_kill(monkeypatch):
    """THE DEFECT, STATED AS AN ASSERTION. The walk did not finish, so the
    tree may be alive. Anything other than False here is a claim we cannot
    support."""
    monkeypatch.setattr(
        ttyguard, "run",
        _fake_run(subprocess.TimeoutExpired(cmd="taskkill", timeout=15)),
    )
    assert ttyguard.kill_tree(1234) is False


def test_a_clean_kill_is_reported_as_one(monkeypatch):
    """CONTROL. Without this, a function that returned False unconditionally
    would satisfy the test above - and every successful cancel would start
    warning the user about a process that really did stop."""
    monkeypatch.setattr(ttyguard, "run", _fake_run(ttyguard.TASKKILL_OK))
    assert ttyguard.kill_tree(1234) is True


def test_an_ALREADY_GONE_process_is_success_not_failure(monkeypatch):
    """The original comment was RIGHT about this case, and it is the reason
    the blanket except looked reasonable for so long. taskkill returns 128
    for both 'already exited' and 'no such pid' - measured on this machine -
    and that is the outcome we wanted, reached early."""
    monkeypatch.setattr(ttyguard, "run", _fake_run(ttyguard.TASKKILL_NOT_FOUND))
    assert ttyguard.kill_tree(1234) is True


def test_a_taskkill_that_RAN_AND_FAILED_is_not_success(monkeypatch):
    """THE SECOND SILENT HOLE, in the same line. run() is called without
    check=, so a non-zero exit (access denied, for one) raised nothing and was
    swallowed by the same blanket except. Exit 1 is not a kill."""
    monkeypatch.setattr(ttyguard, "run", _fake_run(1))
    assert ttyguard.kill_tree(1234) is False


def test_a_missing_taskkill_is_not_success(monkeypatch):
    monkeypatch.setattr(ttyguard, "run", _fake_run(FileNotFoundError("taskkill")))
    assert ttyguard.kill_tree(1234) is False


# ── the three sites that could lie ──────────────────────────────────────

class _Proc:
    pid = 4321

    def communicate(self, timeout=None):
        return ("partial output", "")


def test_the_TIMEOUT_result_warns_the_MODEL_when_the_kill_was_not_confirmed(monkeypatch):
    """The model reads this string. Told the command was stopped, it reasons
    as though it stopped - and may re-run something that is still running."""
    monkeypatch.setattr(ttyguard, "kill_tree", lambda pid, proc=None: False)
    out = core_tools._bash_timeout_result(_Proc(), 30)
    assert "[timed out after 30s]" in out
    assert "could NOT be confirmed" in out, out


def test_CONTROL_the_timeout_result_does_NOT_warn_when_the_kill_worked(monkeypatch):
    """Without this arm, a version that always warns would pass the test above
    and cry wolf on every clean timeout."""
    monkeypatch.setattr(ttyguard, "kill_tree", lambda pid, proc=None: True)
    out = core_tools._bash_timeout_result(_Proc(), 30)
    assert "[timed out after 30s]" in out
    assert "could NOT be confirmed" not in out


def test_the_CANCEL_result_warns_the_MODEL_when_the_kill_was_not_confirmed():
    ttyguard.CANCELLABLE["kill_confirmed"] = False
    try:
        out = core_tools._bash_cancelled_result("some output", "", 0.0)
        assert "[cancelled by user" in out
        assert "could NOT be confirmed" in out, out
    finally:
        ttyguard.CANCELLABLE["kill_confirmed"] = True


def test_CONTROL_the_cancel_result_is_unchanged_when_the_kill_was_confirmed():
    ttyguard.CANCELLABLE["kill_confirmed"] = True
    out = core_tools._bash_cancelled_result("some output", "", 0.0)
    assert "[cancelled by user" in out
    assert "could NOT be confirmed" not in out


def test_a_failed_kill_does_not_leak_its_warning_into_the_NEXT_command():
    """`kill_confirmed` is reset at spawn beside `cancelled`. Without that, one
    unconfirmed cancel would attach its warning to every later result for the
    life of the process - a true warning in the wrong place, which trains the
    reader to ignore it."""
    ttyguard.CANCELLABLE["kill_confirmed"] = False
    src = Path(core_tools.__file__).read_text(encoding="utf-8")
    body = src.split("def _run_shell(", 1)[1].split("\ndef ", 1)[0]
    assert 'CANCELLABLE["kill_confirmed"] = True' in body, (
        "_run_shell does not reset kill_confirmed at spawn"
    )
    ttyguard.CANCELLABLE["kill_confirmed"] = True


# ── structural: the blanket swallow must not come back ──────────────────

def test_kill_tree_no_longer_swallows_every_exception_alike():
    """Asserted on the AST, not on text.

    A string search for `except Exception` would match this module's own
    docstring quoting the old code, and a search for the old comment matches
    the retraction that replaced it. That trap has fired repeatedly in this
    repo today - most memorably when a gate counting `asyncio.to_thread(fn`
    started counting the docstring that explained why the gate was unreliable.
    So: walk the handlers and require that TimeoutExpired is treated
    separately from the catch-all.
    """
    src = Path(ttyguard.__file__).read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "kill_tree"
    )
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    assert handlers, "kill_tree has no exception handling at all"

    def names(h):
        t = h.type
        if t is None:
            return {"<bare>"}
        if isinstance(t, ast.Tuple):
            return {ast.unparse(e) for e in t.elts}
        return {ast.unparse(t)}

    caught = set().union(*(names(h) for h in handlers))
    assert any("TimeoutExpired" in c for c in caught), (
        "a timeout is still handled by the same arm as everything else — that "
        "is the defect: an unfinished kill is not an already-dead process"
    )

    # and no handler may simply discard the outcome
    for h in handlers:
        assert not all(isinstance(st, ast.Pass) for st in h.body), (
            "an exception arm still swallows the failure with a bare pass"
        )


def test_kill_tree_returns_a_value_at_all():
    """The old signature was `-> None`. A caller cannot be honest about an
    outcome it is never told."""
    src = Path(ttyguard.__file__).read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "kill_tree"
    )
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
    assert returns, "kill_tree still returns nothing"


# ── the job-object fast path (Ryan's ruling: option (c) + fallback) ──────

def test_the_DEFAULT_spawn_gets_NO_job_which_protects_the_long_lived_callers():
    """🔴 THE REGRESSION GUARD, and it matters more than the fast path.

    ttyguard.popen is ONE implementation with FOUR consumers. Only
    core_tools wants a KILL_ON_JOB_CLOSE job; mcp_client (the MCP stdio
    server), llm_backend (the LM Studio router) and the mark overlay must
    OUTLIVE a tool call. A job takes its members with it whenever the last
    handle closes -- including on ordinary garbage collection -- so turning
    this on by default would kill those subsystems at unpredictable moments
    and would look like anything except a cancel bug.

    Default OFF is the whole safety property. Assert it directly.
    """
    proc = ttyguard.popen(f'"{sys.executable}" -c "pass"', shell=True,
                          stdin=subprocess.DEVNULL)
    try:
        assert getattr(proc, "_litetui_job", None) is None, (
            "a default spawn was put in a kill-on-close job — the MCP server "
            "and the model router would die when the handle closes"
        )
    finally:
        proc.wait(timeout=30)


def test_only_the_TOOL_opts_in():
    """Structural. The three long-lived callers must not acquire the keyword
    by copy-paste later; this fails loudly if one does."""
    root = Path(ttyguard.__file__).parent
    optins = []
    for py in root.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call)
                    and any(k.arg == "kill_on_close" for k in node.keywords)):
                optins.append(py.name)
    assert optins == ["core_tools.py"], (
        f"kill_on_close is passed from {optins}; only the cancellable tool may"
    )


def test_a_real_tree_dies_by_HANDLE_CLOSE_and_the_grandchild_goes_with_it():
    """END TO END, with a real cmd.exe -> python tree.

    shell=True means the direct child is cmd.exe and the work is its
    grandchild -- the exact case taskkill /T existed for and the exact case
    that timed out. The grandchild announces its pid so we assert on the
    process that actually matters, not on cmd.exe.
    """
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="jobkill-test-"))
    pidfile = tmp / "gc.pid"
    script = tmp / "probe.py"
    script.write_text(
        "import os,time,pathlib\n"
        f"pathlib.Path(r'{pidfile}').write_text(str(os.getpid()))\n"
        "time.sleep(300)\n"
    )
    proc = ttyguard.popen(f'"{sys.executable}" "{script}"', shell=True,
                          stdin=subprocess.DEVNULL, kill_on_close=True)
    try:
        assert getattr(proc, "_litetui_job", None) is not None, "no job attached"
        deadline = time.monotonic() + 30
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists(), "the grandchild never started"
        gc_pid = int(pidfile.read_text())
        assert jobkill.alive(gc_pid), "the grandchild was not running"

        t0 = time.monotonic()
        assert ttyguard.kill_tree(proc.pid, proc) is True
        elapsed = time.monotonic() - t0

        assert not jobkill.alive(gc_pid), (
            "THE GRANDCHILD SURVIVED — this is the runaway the whole change "
            "exists to prevent"
        )
        # Not a performance assertion dressed as a correctness one: the point
        # is that the JOB path ran, not the 15s taskkill walk. Anything under
        # a second cannot have been the walk (measured min 3,420ms).
        assert elapsed < 2.0, (
            f"took {elapsed:.2f}s — that is the taskkill walk, so the job "
            "path did not run"
        )
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def test_the_taskkill_fallback_still_runs_when_there_is_no_job(monkeypatch):
    """A proc with no job must still go through taskkill. Without this, the
    three default-spawn callers would lose their kill path entirely."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = ttyguard.TASKKILL_OK
        return R()

    monkeypatch.setattr(ttyguard, "run", fake_run)
    assert ttyguard.kill_tree(4321) is True
    assert calls and calls[0][0] == "taskkill", "the fallback did not run"


def test_a_job_that_fails_to_confirm_FALLS_BACK_rather_than_claiming_success(monkeypatch):
    """The fast path must not become a new way to lie. If the job closes but
    the tree is somehow still alive, we walk — we do not report success."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = ttyguard.TASKKILL_OK
        return R()

    monkeypatch.setattr(ttyguard, "run", fake_run)
    monkeypatch.setattr(jobkill, "close", lambda job: True)
    monkeypatch.setattr(jobkill, "alive", lambda pid: True)   # never dies
    monkeypatch.setattr(ttyguard, "JOB_KILL_CONFIRM_S", 0.05)

    holder = types.SimpleNamespace(_litetui_job=1234)
    assert ttyguard.kill_tree(4321, holder) is True
    assert calls, "the job path claimed success without confirming the kill"
