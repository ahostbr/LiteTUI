"""The tool cancel button — verified against the PROCESS TABLE, not the UI.

Docs/spec-tool-cancel.md names the two traps this feature must not fall into,
and each has a test arm here:

  1. asyncio.to_thread cannot be cancelled — so cancel must reach the PROCESS.
     The positive arm asserts the pid is GONE from the process table, not that
     a bubble stopped updating.
  2. shell=True makes cmd.exe the child and the real work its GRANDCHILD —
     proc.kill() orphans it. The tree arm asserts the grandchild died too, by
     finding it in the table by a marker in its command line.

Without the negative arm (same command, no cancel, still alive then completes
normally) the positive arm would pass for a process that merely exits.

The marker query filters out its OWN powershell process — a query for MARKER
matches the process making the query, the same trap as the grep that matched
our own conversation.
"""
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app as app_mod
import ttyguard
from plugins import core_tools
from app import LiteTUI

_SRC = Path(__file__).resolve().parent.parent / "src"
# The subjects moved in the plugin split: tool_bash lives in the core-tools
# plugin, kill_tree in the envelope. A gate reads the source that HOLDS its subject.
CORE_TOOLS_SRC = (_SRC / "plugins" / "core_tools.py").read_text(encoding="utf-8")
TTYGUARD_SRC = (_SRC / "ttyguard.py").read_text(encoding="utf-8")


def _pids_with_marker(marker: str) -> list[int]:
    """Pids whose command line carries the marker — excluding the query."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*"
         + marker + "*' -and $_.CommandLine -notlike '*Get-CimInstance*' }).ProcessId"],
        capture_output=True, text=True, timeout=30,
    ).stdout
    return [int(x) for x in out.split() if x.strip().isdigit()]


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


def _wait(cond, tries=100, delay=0.1):
    for _ in range(tries):
        if cond():
            return True
        time.sleep(delay)
    return False


def test_cancel_kills_the_whole_tree_and_the_turn_survives(tmp_path):
    """POSITIVE + TREE ARMS. The grandchild python must be GONE from the
    process table, and tool_bash must RETURN (an honest string) rather than
    hang — the turn carries on, which is the difference from Esc."""
    marker = f"CANCELPROBE_{uuid.uuid4().hex[:12]}"
    script = tmp_path / f"{marker}.py"
    script.write_text("import time\nprint('probe up', flush=True)\ntime.sleep(300)\n")
    th, box = _run_bash_in_thread(
        {"command": f'"{sys.executable}" "{script}"', "timeout": 240})

    assert _wait(lambda: ttyguard.CANCELLABLE["proc"] is not None), \
        "tool_bash never populated the cancellable slot"
    proc = ttyguard.CANCELLABLE["proc"]
    assert _wait(lambda: _pids_with_marker(marker)), \
        "grandchild never appeared in the process table"

    # The exact core of action_cancel_tool, minus the notify.
    ttyguard.CANCELLABLE["cancelled"] = True
    ttyguard.kill_tree(proc.pid)

    th.join(timeout=30)
    assert not th.is_alive(), "tool_bash did not return after the kill"
    assert box["result"].startswith("[cancelled by user after"), box["result"]
    # partial output written before the kill is preserved, not discarded
    assert "probe up" in box["result"]
    assert _wait(lambda: not _pids_with_marker(marker), tries=50), \
        "GRANDCHILD SURVIVED — the tree kill missed the real work"


def test_negative_arm_no_cancel_means_normal_completion(tmp_path):
    """Without this, the test above measures only that processes eventually
    exit. Same shape, no cancel: alive while running, normal result after."""
    marker = f"CANCELPROBE_{uuid.uuid4().hex[:12]}"
    script = tmp_path / f"{marker}.py"
    script.write_text("import time\ntime.sleep(2)\nprint('done cleanly')\n")
    th, box = _run_bash_in_thread(
        {"command": f'"{sys.executable}" "{script}"', "timeout": 240})

    assert _wait(lambda: _pids_with_marker(marker)), "probe never started"
    th.join(timeout=60)
    assert not th.is_alive()
    assert "done cleanly" in box["result"]
    assert "[cancelled" not in box["result"]
    assert ttyguard.CANCELLABLE["proc"] is None  # slot cleared on the way out


def test_timeout_now_kills_the_tree_too(tmp_path):
    """The old run() path timed out and LEFT THE TREE RUNNING (documented in
    the spec as the 3m45s runaway). The popen path tree-kills on timeout."""
    marker = f"CANCELPROBE_{uuid.uuid4().hex[:12]}"
    script = tmp_path / f"{marker}.py"
    script.write_text("import time\ntime.sleep(300)\n")
    th, box = _run_bash_in_thread(
        {"command": f'"{sys.executable}" "{script}"', "timeout": 2})

    assert _wait(lambda: _pids_with_marker(marker)), "probe never started"
    th.join(timeout=60)
    assert not th.is_alive()
    assert box["result"].startswith("[timed out after 2s]")
    assert _wait(lambda: not _pids_with_marker(marker), tries=50), \
        "timeout reported but the tree is still running — the old lie"


# --- the action's guard, no app needed ---------------------------------------
def test_action_with_nothing_running_is_an_honest_no_op():
    notes = []
    ns = SimpleNamespace(notify=lambda msg, timeout=0: notes.append(msg))
    LiteTUI.action_cancel_tool(ns)
    assert notes and "No cancellable tool" in notes[0]
    assert ttyguard.CANCELLABLE["cancelled"] is False   # nothing armed


# --- source gates ------------------------------------------------------------
def test_bash_goes_through_popen_not_run():
    """run() blocks with the Popen trapped inside it — the handle is the
    feature. If bash drifts back to run(), cancel silently dies."""
    body = CORE_TOOLS_SRC.split("def tool_bash(", 1)[1].split("\ndef ", 1)[0]
    assert "ttyguard.popen(" in body
    assert "ttyguard.run(" not in body


def test_the_kill_is_a_tree_kill():
    body = TTYGUARD_SRC.split("def kill_tree(", 1)[1].split("\ndef ", 1)[0]
    assert "/T" in body and "/F" in body
