"""Opt-in: does a STUCK close leak the owned claude.exe, and does close() return?

Sentinel's card asks for proof before any fix, so this probe MEASURES and never
asserts a verdict: it records what happened. Run it before and after a change to
claude_session.close() and compare the two artifacts.

WHAT "STUCK" MEANS HERE, AND WHY IT IS THE RIGHT WEDGE. `claude_session.close()`
asks the owner to close, waits 12s, then cancels it and awaits an UNBOUNDED
gather. The owner's teardown awaits `client.disconnect()`. So the failure the card
is about needs exactly one condition: a disconnect that does not complete and does
not honour cancellation. Not hypothetical — the pinned SDK's own transport says so
at subprocess_cli.py:972, where keeping a still-running child in `_ACTIVE_CHILDREN`
is described as the fix for what "turned a cancelled close() into a leaked child".

WHY THE LEAK MATTERS DESPITE THE SDK'S ATEXIT REAPER. That reaper
(subprocess_cli.py:58) runs only at interpreter exit and only sends SIGTERM. In a
long-running TUI that is not cleanup — it means the stray child dies when the user
quits, after every backend switch and failed reconnect has left one behind. When
disconnect DOES run, the SDK escalates properly (wait, terminate, wait, kill) at
subprocess_cli.py:1016-1032, so this probe isolates one thing: whether that
escalation is reached at all when the owner is cancelled.

THREE THINGS THIS PROBE LEARNED THE HARD WAY, recorded because each one silently
produces a meaningless run:

  1. `asyncio.wait_for(close(), t)` CANNOT BOUND THIS. On timeout it cancels the
     inner coroutine and then AWAITS it, so a cancellation-resistant target hangs
     the caller's own bound. Two 9-minute runs died to this. `asyncio.wait({task},
     timeout=t)` observes without cancelling — the same conclusion
     claude_backend.settle_close reached independently.
  2. THE WEDGE MUST BE RELEASED BY A SIGNAL, NOT BY CANCELLATION. asyncio.run's
     shutdown cancels every pending task and then awaits it, so a wedge that only
     yields to cancellation hangs the interpreter instead of the code under test.
     It is released by an Event once the measurement is done.
  3. THE ARTIFACT IS WRITTEN AT EVERY MILESTONE, not once at the end. If teardown
     hangs, an artifact written only in a final `finally` never exists, and the run
     costs its whole timeout while telling you nothing.

🔴 NEVER A NAME SCAN. Only the pid this probe recorded from its OWN session's
transport is examined or killed, and only while its creation time still matches —
a pid alone is not an identity, as the restart probe measured when Windows reused
one 6.4s later. 17 claude.exe can be live on this box, including the operator's own
sessions. The probe force-kills its one recorded tree before exiting.

RUN:  set LITETUI_CLAUDE_LIVE=1  and  python e2e/claude_close_escalation_probe.py
No user turn is made: the session is started for metadata only, the same connection
ClaudeBackend.ensure_running uses, so this costs no model call.
Writes artifacts/claude-close-escalation.json.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts" / "claude-close-escalation.json"
CLOSE_BOUND = 25          # close() budgets 12s itself, then gathers unbounded
GRACE_AFTER_RELEASE = 5   # let a released wedge unwind before giving up on it
_T0 = time.monotonic()

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def log(phase):
    """Timestamped phase line. A probe that can hang must say where it got to."""
    print(f"[{time.monotonic() - _T0:7.1f}s] {phase}", file=sys.stderr, flush=True)


def creation_filetime(pid):
    """Creation time of a LIVE pid, or None. Native, sub-second, no PowerShell.

    Identity, not liveness: Windows reuses pids, so the creation time is what makes
    "the same process" answerable at all.
    """
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        created, exited = wintypes.FILETIME(), wintypes.FILETIME()
        kernel, user = wintypes.FILETIME(), wintypes.FILETIME()
        if not _k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel), ctypes.byref(user)):
            return None
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        if code.value != STILL_ACTIVE:
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        _k32.CloseHandle(handle)


def alive(pid, created):
    """Still running AND still the same process. Never pid alone."""
    return created is not None and creation_filetime(pid) == created


def kill_recorded_tree(pid, created):
    """Force-kill one recorded tree, only while it is still the same process."""
    if not alive(pid, created):
        return "already gone"
    done = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                          capture_output=True, text=True, timeout=60, check=False)
    return (done.stdout or done.stderr or "").strip()[:200]


class Wedged:
    """The real client, with a disconnect that models the stuck case.

    Everything else is delegated, so the CLI child is spawned, configured and torn
    down by the real transport. Only the one await the card is about is replaced.
    """

    def __init__(self, inner, recorded, release):
        self._inner = inner
        self._recorded = recorded
        self._release = release

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def connect(self):
        await self._inner.connect()
        transport = getattr(self._inner, "_transport", None)
        process = getattr(transport, "_process", None)
        pid = getattr(process, "pid", None)
        if pid is not None:
            self._recorded["pid"] = int(pid)
            self._recorded["created"] = creation_filetime(int(pid))

    async def disconnect(self):
        self._recorded["disconnect_entered"] = True
        while not self._release.is_set():
            try:
                await self._release.wait()
            except asyncio.CancelledError:
                # Cancellation-resistant on purpose: this is the shape that makes
                # close()'s unbounded gather wait forever. Released by the Event,
                # never by a cancel — see note 2 in the module docstring.
                self._recorded["disconnect_swallowed_cancel"] = True
        self._recorded["disconnect_released"] = True


def options():
    """The production option set, copied from ClaudeBackend._options.

    Mirrored rather than imported because that method needs an app's Settings;
    every key here is one the pinned SDK already accepts in production.
    """
    from claude_agent_sdk import ClaudeAgentOptions
    return ClaudeAgentOptions(
        cli_path=None, setting_sources=[], skills=[], strict_mcp_config=True,
        mcp_servers={}, tools=[], permission_mode="dontAsk",
        include_partial_messages=True, verbatim_prompts=True,
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": "Close-escalation probe. Do not act."},
        extra_args={"no-chrome": None, "disable-slash-commands": None,
                    "replay-user-messages": None},
        cwd=str(ROOT),
    )


def save(evidence):
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")


async def run(evidence) -> dict:
    from claude_agent_sdk import ClaudeSDKClient

    from litetui.claude_session import ClaudeSession

    recorded: dict = {}
    release = asyncio.Event()
    session = ClaudeSession(
        options(), lambda opts: Wedged(ClaudeSDKClient(opts), recorded, release))
    log("session.start() begin")
    info = await session.start()
    log("session.start() returned")
    evidence["started"] = bool(info)
    evidence["recorded_child"] = dict(recorded)
    save(evidence)
    pid, created = recorded.get("pid"), recorded.get("created")
    if pid is None:
        evidence["result"] = "INCONCLUSIVE"
        evidence["error"] = ("could not read the owned child pid from "
                             "client._transport._process; SDK internals moved")
        release.set()
        save(evidence)
        return evidence
    evidence["child_alive_before_close"] = alive(pid, created)
    save(evidence)
    closing = None
    try:
        log("close task created")
        started = time.monotonic()
        # NOT wait_for — see note 1 in the module docstring.
        closing = asyncio.ensure_future(session.close())
        done, pending = await asyncio.wait([closing], timeout=CLOSE_BOUND)
        log(f"asyncio.wait returned done={len(done)} pending={len(pending)}")
        evidence["close_returned"] = bool(done)
        evidence["close_seconds"] = round(time.monotonic() - started, 1)
        if closing in done and closing.exception() is not None:
            evidence["close_error"] = repr(closing.exception())
        evidence["disconnect_entered"] = recorded.get("disconnect_entered", False)
        evidence["disconnect_swallowed_cancel"] = recorded.get(
            "disconnect_swallowed_cancel", False)
        evidence["owner_done"] = bool(session._owner and session._owner.done())
        evidence["cleanup_errors"] = list(session.lifecycle.cleanup_errors)
        evidence["lifecycle_failure"] = session.lifecycle.failure
        # The measurement the card asks for.
        evidence["child_alive_after_close"] = alive(pid, created)
        evidence["leaked"] = evidence["child_alive_after_close"]
        evidence["result"] = "LEAKS" if evidence["leaked"] else "NO LEAK"
        save(evidence)
        log(f"measured: {evidence['result']}")
    finally:
        log("releasing wedge")
        release.set()
        await asyncio.sleep(GRACE_AFTER_RELEASE)
        evidence["close_returned_after_release"] = bool(closing and closing.done())
        evidence["child_alive_after_release"] = alive(pid, created)
        evidence["probe_cleanup"] = kill_recorded_tree(pid, created)
        evidence["child_alive_after_probe_cleanup"] = alive(pid, created)
        save(evidence)
        log(f"probe cleanup: {evidence['probe_cleanup']}")
    return evidence


def main():
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1; no CLI child started")
    os.environ.setdefault("LITETUI_NO_HARNESS", "1")
    evidence = {"result": "FAIL", "wedge": "disconnect never returns until released",
                "close_bound_s": CLOSE_BOUND}
    try:
        asyncio.run(run(evidence))
        log("asyncio.run returned")
    except BaseException as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        save(evidence)
        raise
    finally:
        save(evidence)
        print(json.dumps({k: v for k, v in evidence.items()
                          if k != "recorded_child"}, indent=2, default=str))
        print(f"{evidence.get('result')} {ARTIFACT}")
        sys.stdout.flush()
        sys.stderr.flush()
        # The owned tree is already killed and every measurement is on disk. A
        # still-pending SDK task must not turn a finished probe into a hang.
        os._exit(0)


if __name__ == "__main__":
    main()
