"""Instance-owned process registry; platform probing/termination are injected.

No widget or application object is retained. Unknown/reused process identity
fails closed; a terminal outcome remains queryable after cleanup.
"""
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created: str


@dataclass(frozen=True)
class TaskOutcome:
    task_id: str
    owner: str
    state: str
    reason: str | None = None


class TaskSupervisor:
    def __init__(self, instance_id, *, identity_probe, terminate):
        self.instance_id = instance_id
        self.identity_probe = identity_probe
        self.terminate = terminate
        self._processes = {}
        self._outcomes = {}
        self._lock = RLock()

    def register(self, identity):
        if not isinstance(identity, ProcessIdentity) or identity.pid <= 0 or not identity.created:
            raise ValueError('A positive PID and process creation identity are required')
        with self._lock:
            task_id = uuid4().hex
            while task_id in self._processes:
                task_id = uuid4().hex
            self._processes[task_id] = identity
            return task_id

    def cancel(self, task_id, *, requester=None):
        if requester is not None and requester != self.instance_id:
            raise PermissionError('Task belongs to another instance')
        with self._lock:
            if task_id in self._outcomes:
                return self._outcomes[task_id]
            identity = self._processes[task_id]
            try:
                current = self.identity_probe(identity.pid)
                if current is None:
                    outcome = TaskOutcome(task_id, self.instance_id, 'identity_unknown', 'Cannot prove process ownership')
                elif current != identity.created:
                    outcome = TaskOutcome(task_id, self.instance_id, 'identity_mismatch', 'PID has been reused')
                else:
                    self.terminate(identity.pid)
                    outcome = TaskOutcome(task_id, self.instance_id, 'cancelled')
            except Exception as exc:
                outcome = TaskOutcome(task_id, self.instance_id, 'cleanup_failed', str(exc))
            self._outcomes[task_id] = outcome
            return outcome

    def finish(self, task_id, *, state='completed', reason=None):
        if state not in ('completed', 'failed', 'cancelled'):
            raise ValueError('Invalid terminal state')
        with self._lock:
            if task_id not in self._processes:
                raise KeyError(task_id)
            return self._outcomes.setdefault(task_id, TaskOutcome(task_id, self.instance_id, state, reason))


def process_creation_identity(pid):
    """Kernel creation stamp; None means ownership cannot be established."""
    import os
    if os.name != 'nt':
        try:
            from pathlib import Path
            text = Path(f'/proc/{pid}/stat').read_text()
            return text[text.rfind(')') + 2:].split()[19]
        except (OSError, IndexError):
            return None
    import ctypes
    from ctypes import wintypes as wt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wt.BOOL
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.CloseHandle.restype = wt.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        stamps = [wt.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(stamp) for stamp in stamps)):
            return None
        return str((stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)
