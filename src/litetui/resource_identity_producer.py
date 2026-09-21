"""Verified identity PRIMITIVES for WS3 calibration provenance (host / build / device).

Each returns a stable identifier or None on any unknown or unstable input — never a
fabricated, partial, or inferred value. These are the reusable pieces an async
evidence cache will compose OFF the event loop; they do NOT wire into
make_demand_for here (a sync resolver cannot await the hashing) and they never
fabricate calibration, so loads stay blocked until real evidence is cached.
"""
from __future__ import annotations

import hashlib
import os

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
