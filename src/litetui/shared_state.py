"""OS-owned leases and coordinated writes for terminal and desktop clients.

Locks live on persistent siblings, never on an atomically replaced data file.
There is no age-based stale eviction: the kernel releases locks when the owning
process exits. An inaccessible lock is occupied, never permission to steal it.
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

DATA_VERSION = 1


class OwnershipError(OSError):
    """Another live process owns the requested mutable resource."""


class Lease:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise OwnershipError(f"Resource is owned by another process: {self.path.name}") from exc
        self.handle = handle
        return self

    def release(self):
        if self.handle is not None:
            self.handle.close()  # kernel releases the byte/flock lock
            self.handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()

    def __del__(self):
        self.release()


@contextmanager
def coordinated_write(path: Path, timeout: float = 10):
    lease = Lease(Path(str(path) + ".lock"))
    deadline = time.monotonic() + timeout
    while True:
        try:
            lease.acquire()
            break
        except OwnershipError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
    try:
        yield
    finally:
        lease.release()


def check_data_version(root: Path) -> dict:
    """Refuse unknown data writers; legacy unmarked data is version 1."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".litetui-data.json"
    with coordinated_write(marker):
        if marker.exists():
            data = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != DATA_VERSION:
                raise ValueError("Incompatible LiteTUI data version; open with a matching runtime")
            if type(data.get("writer_protocol")) is not int or data["writer_protocol"] != 1:
                raise ValueError("Incompatible LiteTUI writer protocol; open with a matching runtime")
        else:
            data = {"version": DATA_VERSION, "writer_protocol": 1}
            temporary = marker.with_name(f".litetui-data-{os.getpid()}.tmp")
            temporary.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temporary, marker)
    return data
