"""Verified identity PRIMITIVES for WS3 calibration provenance (host / build / device).

Each returns a stable identifier or None on any unknown or unstable input — never a
fabricated, partial, or inferred value. These are the reusable pieces an async
evidence cache will compose OFF the event loop; they do NOT wire into
make_demand_for here (a sync resolver cannot await the hashing) and they never
fabricate calibration, so loads stay blocked until real evidence is cached.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass

_CHUNK = 1024 * 1024  # 1 MiB streaming, so a large artifact never loads into memory


def stable_file_fingerprint(path) -> str | None:
    """sha256 of a file's ACTUAL bytes, only if it did not change during the hash.

    A manifest's digest LIST is not proof of the bytes: this reads and hashes the
    real content. Opens one handle and fstats it BEFORE and AFTER the streaming
    hash, comparing size, mtime_ns and (dev, ino). A replace / delete / truncate /
    append during the read yields None — an unstable file is not a stable identity.

    LIMITATION (documented, not hidden): a malicious in-place edit preserving size
    AND mtime AND dev/ino is NOT detected — that is the ceiling of a stat-based
    TOCTOU guard without a trusted content-addressed store. It guards ordinary
    replacement and concurrent writers, not a same-stat forgery.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    if (before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
        return None
    # An open handle SURVIVES a rename/replace of the path — fstat alone would then
    # certify the ORIGINAL bytes while `path` now names a different file. Compare
    # the path's identity to the handle's: a mismatch means the path was replaced.
    try:
        path_stat = os.stat(path)
    except OSError:
        return None
    if (path_stat.st_dev, path_stat.st_ino) != (after.st_dev, after.st_ino):
        return None
    return digest.hexdigest()


_MACHINE_GUID_KEY = r"SOFTWARE\Microsoft\Cryptography"


def hashed_machine_guid(namespace: str) -> str | None:
    """A stable, namespaced HOST id from the Windows MachineGuid.

    Read-only registry read; the raw GUID is hashed with a namespace and NEVER
    returned or logged. None on non-Windows, a non-string/blank value, or any
    read failure.
    """
    if os.name != "nt" or not isinstance(namespace, str) or not namespace:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MACHINE_GUID_KEY) as key:
            guid, kind = winreg.QueryValueEx(key, "MachineGuid")
    except OSError:
        return None
    if kind != winreg.REG_SZ or not isinstance(guid, str) or not guid.strip():
        return None
    return hashlib.sha256(f"{namespace}\x00{guid}".encode("utf-8")).hexdigest()


def explicit_device_uuid(selected_index, gpu_uuids) -> tuple | None:
    """Map an EXPLICIT selected device index to its UUID -> a one-tuple device_set.

    `selected_index` must come from a VERIFIED backend load configuration that
    names the device (e.g. 0). Placement is NEVER inferred from the installed GPU
    count: a single installed GPU with no explicit selection is still None (the
    engine may CPU-offload or honour a device env var). A bool, a negative,
    out-of-range, or a blank/absent UUID => None.
    """
    if type(selected_index) is not int or selected_index < 0:
        return None
    if not isinstance(gpu_uuids, (list, tuple)) or selected_index >= len(gpu_uuids):
        return None
    uuid = gpu_uuids[selected_index]
    if not isinstance(uuid, str) or not uuid.strip():
        return None
    return (uuid,)


def _stat_identity(path):
    """(dev, ino, size, mtime_ns) of `path`, or None if it cannot be stat'd."""
    try:
        s = os.stat(path)
    except OSError:
        return None
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)


@dataclass(frozen=True)
class BackendSnapshot:
    """The immutable key a prepared identity is bound to.

    It is the config context AND the file stat-identities captured at snapshot
    time. A different backend/type/endpoint/model/shape/device OR a changed
    executable/artifact stat is a different snapshot, so resolve() cannot return a
    cached identity across any of those without a fresh prepare.
    """
    backend_type: str
    endpoint: str
    model: str
    context: int | None
    concurrency: int
    load_shape: str
    device_index: int | None
    exe_path: str
    exe_stat: tuple
    artifact_path: str
    artifact_stat: tuple
    gpu_uuids: tuple


class IdentityPreparationCache:
    """Backend-owned: async prepare (off-loop) + sync resolve (fast, no hashing).

    prepare_async runs the injected slow `produce(snapshot)` off the event loop,
    dedupes concurrent requests for the same snapshot, and publishes ONLY a
    complete non-None identity (a cancelled awaiter never publishes a partial one).
    resolve is a pure lookup that revalidates the file stats before returning; a
    config change is already a cache miss because the config is in the key.
    """

    def __init__(self):
        self._done: dict = {}
        self._inflight: dict = {}

    async def prepare_async(self, snapshot, *, produce):
        if snapshot in self._done:
            return self._done[snapshot]
        future = self._inflight.get(snapshot)
        if future is None:
            future = asyncio.ensure_future(asyncio.to_thread(produce, snapshot))

            def _publish(fut, snap=snapshot):
                self._inflight.pop(snap, None)
                if not fut.cancelled() and fut.exception() is None and fut.result() is not None:
                    self._done[snap] = fut.result()

            future.add_done_callback(_publish)
            self._inflight[snapshot] = future
        # shield: a cancelled awaiter must not cancel the shared compute (which
        # other callers may still be awaiting) nor leave a half-done publish.
        return await asyncio.shield(future)

    def resolve(self, snapshot):
        identity = self._done.get(snapshot)
        if identity is None:
            return None
        if _stat_identity(snapshot.exe_path) != tuple(snapshot.exe_stat):
            self._done.pop(snapshot, None)     # executable changed since prepare
            return None
        if snapshot.artifact_path and _stat_identity(snapshot.artifact_path) != tuple(snapshot.artifact_stat):
            self._done.pop(snapshot, None)     # artifact changed since prepare
            return None
        return identity
