"""Tiny-fixture tests for the WS3 verified identity primitives.

No large artifact hashing, no engine, no real registry value printed. Pins the
byte fingerprint (incl. TOCTOU reject), the namespaced/hidden MachineGuid hash,
and explicit-device-only placement.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from litetui import resource_identity_producer as rip


# ── stable_file_fingerprint ─────────────────────────────────────────────────

def test_fingerprint_matches_real_bytes(tmp_path):
    data = b"the quick brown fox" * 1000
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    assert rip.stable_file_fingerprint(p) == hashlib.sha256(data).hexdigest()


def test_fingerprint_missing_file_is_none(tmp_path):
    assert rip.stable_file_fingerprint(tmp_path / "nope.bin") is None


class _Stat:
    def __init__(self, size, mtime=1, dev=1, ino=1):
        self.st_size, self.st_mtime_ns, self.st_dev, self.st_ino = size, mtime, dev, ino


@pytest.mark.parametrize("after", [
    _Stat(6),               # size grew (append)
    _Stat(5, mtime=2),      # mtime changed
    _Stat(5, ino=999),      # replaced (new inode)
])
def test_fingerprint_rejects_change_during_hash(tmp_path, monkeypatch, after):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    stats = iter([_Stat(5), after])          # before, after (both via os.fstat on the handle)
    monkeypatch.setattr(os, "fstat", lambda fd: next(stats))
    assert rip.stable_file_fingerprint(p) is None


def test_fingerprint_rejects_path_replaced_after_open(tmp_path, monkeypatch):
    # The open handle stays valid on the ORIGINAL file (fstat unchanged), but the
    # PATH is renamed/replaced onto a different file: os.stat(path) resolves to a
    # different (dev, ino) than the handle's, so the fingerprint is rejected.
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    monkeypatch.setattr(os, "stat", lambda path_arg: _Stat(5, ino=424242))
    assert rip.stable_file_fingerprint(p) is None


def test_fingerprint_path_deleted_after_open_is_none(tmp_path, monkeypatch):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")

    def _boom(path_arg):
        raise OSError("path gone")

    monkeypatch.setattr(os, "stat", _boom)
    assert rip.stable_file_fingerprint(p) is None


# ── hashed_machine_guid ─────────────────────────────────────────────────────

def test_hashed_machine_guid_is_namespaced_and_hides_raw(monkeypatch):
    import winreg

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(winreg, "OpenKey", lambda *a: _Key())
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: ("FAKE-GUID-1234", winreg.REG_SZ))
    out = rip.hashed_machine_guid("ns")
    assert out == hashlib.sha256(b"ns\x00FAKE-GUID-1234").hexdigest()
    assert "FAKE-GUID-1234" not in out       # raw never leaks


def test_hashed_machine_guid_none_off_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert rip.hashed_machine_guid("ns") is None


def test_hashed_machine_guid_blank_namespace_is_none():
    assert rip.hashed_machine_guid("") is None


# ── explicit_device_uuid ────────────────────────────────────────────────────

def test_explicit_device_maps_selected_index():
    assert rip.explicit_device_uuid(0, ["GPU-a", "GPU-b"]) == ("GPU-a",)
    assert rip.explicit_device_uuid(1, ["GPU-a", "GPU-b"]) == ("GPU-b",)


@pytest.mark.parametrize("index,uuids", [
    (None, ["GPU-a"]),          # no explicit selection -> cannot infer, even single GPU
    (True, ["GPU-a"]),          # bool is not a device index
    (-1, ["GPU-a"]),
    (0, []),                    # no GPUs
    (2, ["GPU-a"]),             # out of range
    (0, ["  "]),                # blank uuid
])
def test_explicit_device_uuid_none_cases(index, uuids):
    assert rip.explicit_device_uuid(index, uuids) is None


# ── validate_identity + IdentityPreparationCache (async prepare / sync resolve) ─

import asyncio  # noqa: E402

from litetui.resource_identity_producer import (  # noqa: E402
    BackendSnapshot,
    IdentityPreparationCache,
    _stat_identity,
    validate_identity,
)


def _snap(exe_path, **over):
    base = dict(
        backend_type="NInferBackend", endpoint="http://127.0.0.1:9000/v1", model="m",
        context=32768, concurrency=1, load_shape="nvfp4", device_index=0,
        exe_path=str(exe_path), exe_stat=_stat_identity(exe_path),
        artifact_path="", artifact_stat=(), gpu_uuids=("GPU-a",),
    )
    base.update(over)
    return BackendSnapshot(**base)


def _full_identity(snap):
    """A COMPLETE identity bound to `snap` (device_set = explicit device 0)."""
    return {
        "host": "H", "backend": "ninfer", "build": "B", "endpoint": snap.endpoint,
        "model": snap.model, "context": snap.context, "concurrency": snap.concurrency,
        "artifact": "a.ninfer", "artifact_fingerprint": "FP", "load_shape": snap.load_shape,
        "device_set": ("GPU-a",),
    }


# validate_identity: schema completeness + snapshot binding

def test_validate_rejects_partial_empty_mismatch_and_unknown_device():
    snap = BackendSnapshot("NInferBackend", "http://h/v1", "m", 32768, 1, "nvfp4", 0,
                           "e", (1, 2, 3, 4), "", (), ("GPU-a",))
    assert validate_identity({}, snap) is None
    assert validate_identity({"host": "H"}, snap) is None            # partial
    assert validate_identity(_full_identity(snap), snap) is not None  # complete + bound
    wrong = _full_identity(snap); wrong["model"] = "other"
    assert validate_identity(wrong, snap) is None                    # config mismatch
    no_dev = BackendSnapshot("NInferBackend", "http://h/v1", "m", 32768, 1, "nvfp4", None,
                             "e", (1, 2, 3, 4), "", (), ("GPU-a",))
    assert validate_identity(_full_identity(no_dev), no_dev) is None  # placement unknown


def test_validate_returns_a_defensive_canonical_copy():
    snap = BackendSnapshot("NInferBackend", "http://h/v1", "m", 32768, 1, "nvfp4", 0,
                           "e", (1, 2, 3, 4), "", (), ("GPU-a",))
    src = _full_identity(snap)
    src["device_set"] = ["GPU-a"]           # a list is accepted...
    out = validate_identity(src, snap)
    assert out["device_set"] == ("GPU-a",)  # ...and canonicalized to a tuple
    out["host"] = "TAMPERED"
    assert src["host"] == "H"               # mutating the result never touches the input


@pytest.mark.asyncio
async def test_prepare_dedupes_and_returns_fresh_copies(tmp_path):
    exe = tmp_path / "e.exe"; exe.write_bytes(b"x")
    snap = _snap(exe)
    calls = []

    def produce(s):
        calls.append(s)
        return _full_identity(s)

    cache = IdentityPreparationCache()
    a, b = await asyncio.gather(cache.prepare_async(snap, produce=produce),
                                cache.prepare_async(snap, produce=produce))
    assert a == b == _full_identity(snap)
    assert a is not b                       # each caller gets its own copy
    assert len(calls) == 1                  # one shared compute


@pytest.mark.asyncio
async def test_resolve_returns_copy_then_invalidates_on_stat_change(tmp_path):
    exe = tmp_path / "e.exe"; exe.write_bytes(b"x")
    snap = _snap(exe)
    cache = IdentityPreparationCache()
    await cache.prepare_async(snap, produce=_full_identity)
    assert cache.resolve(snap) == _full_identity(snap)
    exe.write_bytes(b"xy")                  # executable changed since prepare
    assert cache.resolve(snap) is None


@pytest.mark.asyncio
async def test_caller_mutation_does_not_corrupt_cache(tmp_path):
    exe = tmp_path / "e.exe"; exe.write_bytes(b"x")
    snap = _snap(exe)
    producer_dict = _full_identity(snap)
    cache = IdentityPreparationCache()
    await cache.prepare_async(snap, produce=lambda s: producer_dict)
    producer_dict["host"] = "TAMPERED"      # mutate the producer's dict after publish
    first = cache.resolve(snap)
    first["host"] = "ALSO-TAMPERED"         # mutate the returned copy
    assert cache.resolve(snap)["host"] == "H"   # cached value is intact through both


@pytest.mark.asyncio
async def test_partial_and_none_results_are_not_published(tmp_path):
    exe = tmp_path / "e.exe"; exe.write_bytes(b"x")
    snap = _snap(exe)
    cache = IdentityPreparationCache()
    assert await cache.prepare_async(snap, produce=lambda s: {}) is None
    assert await cache.prepare_async(snap, produce=lambda s: {"host": "H"}) is None
    assert await cache.prepare_async(snap, produce=lambda s: None) is None
    assert cache.resolve(snap) is None


@pytest.mark.asyncio
async def test_file_mutated_during_compute_is_not_published(tmp_path):
    exe = tmp_path / "e.exe"; exe.write_bytes(b"x")
    snap = _snap(exe)                       # captures the ORIGINAL exe stat
    cache = IdentityPreparationCache()

    def produce(s):
        exe.write_bytes(b"CHANGED-DURING-COMPUTE")   # post-hash instability
        return _full_identity(s)

    assert await cache.prepare_async(snap, produce=produce) is None
    assert cache.resolve(snap) is None


@pytest.mark.asyncio
async def test_prepare_again_after_change_returns_none_not_stale(tmp_path):
    exe = tmp_path / "e.exe"; exe.write_bytes(b"x")
    snap = _snap(exe)
    cache = IdentityPreparationCache()
    await cache.prepare_async(snap, produce=_full_identity)
    assert cache.resolve(snap) is not None
    exe.write_bytes(b"xy")                  # exe changed; snap still holds the OLD stat
    assert await cache.prepare_async(snap, produce=_full_identity) is None
    assert cache.resolve(snap) is None


@pytest.mark.asyncio
async def test_producer_error_fails_closed(tmp_path):
    exe = tmp_path / "e.exe"; exe.write_bytes(b"x")
    snap = _snap(exe)
    cache = IdentityPreparationCache()

    def boom(s):
        raise RuntimeError("producer blew up")

    assert await cache.prepare_async(snap, produce=boom) is None
    assert cache.resolve(snap) is None


def test_resolve_miss_for_unprepared(tmp_path):
    exe = tmp_path / "e.exe"; exe.write_bytes(b"x")
    assert IdentityPreparationCache().resolve(_snap(exe)) is None


@pytest.mark.asyncio
async def test_config_change_is_a_cache_miss(tmp_path):
    exe = tmp_path / "e.exe"; exe.write_bytes(b"x")
    cache = IdentityPreparationCache()
    await cache.prepare_async(_snap(exe, model="a"), produce=_full_identity)
    assert cache.resolve(_snap(exe, model="b")) is None     # different config = different key


@pytest.mark.asyncio
async def test_cancelled_sole_awaiter_does_not_publish(tmp_path):
    exe = tmp_path / "e.exe"; exe.write_bytes(b"x")
    snap = _snap(exe)
    cache = IdentityPreparationCache()

    def produce(s):
        import time
        time.sleep(0.05)
        return _full_identity(s)

    task = asyncio.ensure_future(cache.prepare_async(snap, produce=produce))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.12)
    # the sole awaiter that would publish was cancelled BEFORE the await returned,
    # so the inline publish never ran — nothing is left behind.
    assert cache.resolve(snap) is None
