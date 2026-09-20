"""Durable resource owner identity, never a PID alone or heartbeat expiry."""
from dataclasses import asdict, dataclass
import json
import os
from litetui.task_supervisor import process_creation_identity


@dataclass(frozen=True)
class ResourceOwner:
    instance: str
    pid: int
    created: str

    def encode(self):
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def current(cls, instance):
        created = process_creation_identity(os.getpid())
        if not created or not isinstance(instance, str) or not instance:
            raise ValueError('Resource ownership requires instance and kernel creation identity')
        return cls(instance, os.getpid(), created)


def process_exists(pid):
    """True/False/None: access denied is unknown, not proof of death."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        kernel.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None


def owner_alive(encoded, *, probe=process_creation_identity, exists=process_exists):
    try:
        owner = json.loads(encoded)
        if not isinstance(owner, dict):
            return None
        pid, created, instance = owner.get('pid'), owner.get('created'), owner.get('instance')
        if type(pid) is not int or pid <= 0 or not isinstance(created, str) or not created or not isinstance(instance, str) or not instance:
            return None
        current = probe(pid)
        if current is not None:
            return current == created
        return False if exists(pid) is False else None
    except (TypeError, ValueError, OSError):
        return None
