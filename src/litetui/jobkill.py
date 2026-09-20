"""Kill a process tree by CLOSING A HANDLE instead of by walking it.

WHY THIS EXISTS. `taskkill /PID n /T /F` walks the process table to find
descendants, and the walk is the cost. Measured on this machine against the
cmd.exe -> python tree the bash tool spawns:

    taskkill on a LIVE tree     3,420 / 6,380 / 43,060 ms   (min/median/max, n=10)
    taskkill on a DEAD pid      3,678 / 6,568 / 10,393 ms   (n=6)
    job handle close            0.1 - 0.2 ms                (n=15)

🔴 READ THE MIDDLE ROW AGAIN: killing a pid that is ALREADY DEAD costs the
same as killing a live tree. The expense is enumerating the process table to
find children, and that happens whether or not there is anything to kill. It
is also load-dependent -- one run against a dead pid exceeded 30 SECONDS on a
busy box. That single measurement is why the taskkill fallback below is
CONDITIONAL and not unconditional: running it after every successful job kill
would add seconds to every cancel and throw away the entire benefit.

⚛️ AND THERE IS A CORRECTNESS ARGUMENT, NOT ONLY A SPEED ONE (Anvil). A job
kill is ATOMIC OVER THE TREE. taskkill's walk never was: it enumerates and
then kills IN SEQUENCE, so a process that FORKS WHILE THE WALK IS IN PROGRESS
can be missed entirely — at any speed, on an idle box, with any budget. That
is a class of missed-kill taskkill cannot fix. The job object does not make it
faster; IT MAKES IT NOT EXIST. It also means some historical "the cancel
worked but something survived" reports may have been this rather than the
timeout, and nobody would have attributed them correctly.

HOW A JOB SOLVES IT. A process assigned to a Job Object with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE takes its whole tree with it when the last
handle to that job closes -- every descendant inherits the job, so there is
nothing to enumerate and nothing to time out. The failure mode we are removing
is not "the kill is slow", it is "the kill has a deadline it can miss".

⚠️ NEVER ASSIGN OUR OWN PROCESS TO ONE OF THESE JOBS. A KILL_ON_JOB_CLOSE job
containing this process kills the app the moment the handle closes -- including
on ordinary garbage collection of the handle. Only ever assign a CHILD. This
is not hypothetical: it was hit while probing and required deliberately leaking
a handle to survive.

🔴 EVERY ctypes SIGNATURE BELOW IS DECLARED, AND THAT IS LOAD BEARING. The
default restype is c_int, which TRUNCATES A 64-BIT HANDLE on Win64. An
undeclared `IsProcessInJob` in an early version of this work reported that a
process was not in a job when it was, and the false answer was only caught
because a second call disagreed with it. `liteharness/hooks.py` carries the
same warning in a comment -- someone paid for this lesson before us.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import os

WINDOWS = sys.platform == "win32"

if WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _k32.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
    _k32.CreateJobObjectW.restype = wt.HANDLE
    _k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]
    _k32.SetInformationJobObject.restype = wt.BOOL
    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.OpenProcess.restype = wt.HANDLE
    _k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    _k32.AssignProcessToJobObject.restype = wt.BOOL
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.CloseHandle.restype = wt.BOOL
    _k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    _k32.GetExitCodeProcess.restype = wt.BOOL
else:  # pragma: no cover - the tool path is Windows-only today
    _k32 = None

_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_ALL_ACCESS = 0x1F0FFF
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wt.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wt.DWORD),
                ("SchedulingClass", wt.DWORD)]


class _EXT_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def available() -> bool:
    """Whether this platform can do the job-object kill at all."""
    return WINDOWS and _k32 is not None


def create() -> int | None:
    """A job whose members die when the last handle to it closes.

    Returns the handle, or None if the platform cannot or the call failed.
    None is not an error to report — it means the caller falls back.
    """
    if not available():
        return None
    h = _k32.CreateJobObjectW(None, None)
    if not h:
        return None
    info = _EXT_LIMIT()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _k32.SetInformationJobObject(
        h, _JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info)
    ):
        # A job without the flag would NOT kill its members on close, so it is
        # worse than none: it would look like protection and provide none.
        _k32.CloseHandle(h)
        return None
    return h


def assign(job: int | None, pid: int) -> bool:
    """Put ONE CHILD process into the job. Descendants inherit it.

    ⚠️ NEVER PASS THIS PROCESS'S OWN PID. See the module docstring: a
    KILL_ON_JOB_CLOSE job containing us kills the app when the handle closes.
    """
    if job is None or not available():
        return False
    if pid == os.getpid() or pid <= 0:
        return False
    ph = _k32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
    if not ph:
        return False
    try:
        return bool(_k32.AssignProcessToJobObject(job, ph))
    finally:
        _k32.CloseHandle(ph)


def close(job: int | None) -> bool:
    """Close the handle. If it was the last one, the kernel kills every member.

    This IS the kill. There is no walk, no deadline and nothing to time out —
    which is the whole reason this module exists.
    """
    if job is None or not available():
        return False
    return bool(_k32.CloseHandle(job))


def alive(pid: int) -> bool:
    """Cheap liveness check — microseconds, no process-table enumeration.

    Used to CONFIRM a kill rather than assume one. `kill_tree` promises that
    True means the tree is confirmed gone, and a promise needs a check.
    """
    if not available():
        return False
    ph = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not ph:
        return False   # cannot open it: it is gone, or not ours to see
    try:
        code = wt.DWORD()
        if not _k32.GetExitCodeProcess(ph, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        _k32.CloseHandle(ph)
