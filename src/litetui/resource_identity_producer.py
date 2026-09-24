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
    if ((path_stat.st_dev, path_stat.st_ino, path_stat.st_size, path_stat.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        # All four, not just (dev, ino): an ordinary append/mtime bump on the SAME
        # inode between the final fstat and this stat keeps dev/ino but changes
        # size/mtime — a dev/ino-only compare would certify the stale digest.
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

    ``backend_type`` is the CANONICAL backend id (the same token an identity's
    ``backend`` field carries, e.g. "ninfer"), NOT a class name — validate_identity
    compares them for equality, so both sides must be the one schema token.
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

    def __post_init__(self):
        # The snapshot is used as a dict KEY, so every container field must be
        # hashable. A caller passing a list (despite the tuple annotation) is
        # canonicalized to a tuple rather than crashing the cache with an
        # "unhashable type" at lookup time.
        for field in ("exe_stat", "artifact_stat", "gpu_uuids"):
            value = getattr(self, field)
            if isinstance(value, list):
                object.__setattr__(self, field, tuple(value))


def _snapshot_stable(snapshot) -> bool:
    """True iff the snapshot's executable and artifact still match their captured
    stat-identities — the file is not mid-write/replaced right now.

    A malformed stat-identity (not a tuple, e.g. None) fails CLOSED: an unusable
    snapshot is never treated as stable, so nothing is published or returned for it.
    """
    if not isinstance(snapshot.exe_stat, tuple) or _stat_identity(snapshot.exe_path) != snapshot.exe_stat:
        return False
    if snapshot.artifact_path:
        if not isinstance(snapshot.artifact_stat, tuple) or _stat_identity(snapshot.artifact_path) != snapshot.artifact_stat:
            return False
    return True


def validate_identity(identity, snapshot):
    """Canonicalize a producer result, or None if it is not a COMPLETE identity
    BOUND to this snapshot.

    A `{}` / partial dict / extra-key dict / any missing-or-invalid field is
    rejected. host/build/artifact_fingerprint must be non-blank (real evidence);
    device_set a non-empty tuple of non-blank strings; and the config fields must
    MATCH the snapshot — backend (canonical id == snapshot.backend_type), endpoint,
    model, context, concurrency, load_shape, artifact (== snapshot.artifact_path) —
    AND the device_set must equal the snapshot's EXPLICIT placement (so a producer
    cannot return an identity for another backend/artifact/config, and an unknown
    device_index — placement None — can never validate). ``artifact_fingerprint``
    stays the trusted producer's responsibility (the cache cannot re-derive a hash
    from a string); it is only checked non-blank. Returns a fresh canonical dict
    (device_set a tuple) that shares no mutable state with the input.
    """
    from litetui.resource_calibration import IDENTITY_FIELDS
    if not isinstance(identity, dict) or set(identity) != set(IDENTITY_FIELDS):
        return None
    for key in ("host", "build", "artifact_fingerprint", "backend", "endpoint", "model"):
        if not isinstance(identity.get(key), str) or not identity[key].strip():
            return None
    if type(identity.get("concurrency")) is not int or identity["concurrency"] <= 0:
        return None
    context = identity.get("context")
    if context is not None and (type(context) is not int or context <= 0):
        return None
    if not isinstance(identity.get("artifact"), str) or not isinstance(identity.get("load_shape"), str):
        return None
    devices = identity.get("device_set")
    if not isinstance(devices, (list, tuple)) or not devices:
        return None
    if any(not isinstance(d, str) or not d.strip() for d in devices):
        return None
    device_set = tuple(devices)
    if (identity["backend"] != snapshot.backend_type
            or identity["endpoint"] != snapshot.endpoint
            or identity["model"] != snapshot.model
            or context != snapshot.context
            or identity["concurrency"] != snapshot.concurrency
            or identity["load_shape"] != snapshot.load_shape
            or identity["artifact"] != snapshot.artifact_path):
        return None
    if device_set != explicit_device_uuid(snapshot.device_index, tuple(snapshot.gpu_uuids)):
        return None
    canonical = dict(identity)
    canonical["device_set"] = device_set
    return canonical


class IdentityPreparationCache:
    """Backend-owned: async prepare (off-loop) + sync resolve (fast, no hashing).

    prepare_async runs the injected slow `produce(snapshot)` off the event loop,
    dedupes concurrent requests to one shielded compute, and publishes ONLY a value
    that `validate` accepts as a COMPLETE identity bound to the snapshot AND that
    passes a post-compute stat recheck. A producer error fails closed (nothing
    published). Cancellation only DETACHES the awaiter: `shield` keeps the shared
    `to_thread` compute running (a thread cannot be force-stopped), and Python does
    not preempt it — so a cancelled SOLE awaiter leaves nothing validated or stored
    (no waiter reaches the publish), while a concurrent awaiter of the same compute
    still publishes. The producer may thus run to completion (its side effects are
    not cancelled) even when its only awaiter is gone. resolve is a pure lookup that
    re-stats before returning and hands back a FRESH copy, so a config change is a
    miss and a caller can never mutate the cached identity.
    """

    def __init__(self):
        self._done: dict = {}
        self._inflight: dict = {}

    def _reap(self, fut, snapshot):
        self._inflight.pop(snapshot, None)
        if not fut.cancelled():
            fut.exception()          # retrieve so an all-cancelled producer failure
                                     # is not an unretrieved-exception warning

    async def prepare_async(self, snapshot, *, produce, validate=None):
        validate = validate or validate_identity
        cached = self.resolve(snapshot)              # cached path re-stats + copies
        if cached is not None:
            return cached
        future = self._inflight.get(snapshot)
        if future is None:
            future = asyncio.ensure_future(asyncio.to_thread(produce, snapshot))
            self._inflight[snapshot] = future
            future.add_done_callback(lambda fut, snap=snapshot: self._reap(fut, snap))
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            raise
        except Exception:                            # noqa: BLE001 - producer error fails closed
            return None
        canonical = validate(result, snapshot)
        if canonical is not None and _snapshot_stable(snapshot):
            self._done.setdefault(snapshot, dict(canonical))   # store a defensive copy
        return self.resolve(snapshot)

    def resolve(self, snapshot):
        stored = self._done.get(snapshot)
        if stored is None:
            return None
        if not _snapshot_stable(snapshot):
            self._done.pop(snapshot, None)           # exe/artifact changed since prepare
            return None
        return dict(stored)                          # fresh copy; caller cannot mutate the cache
