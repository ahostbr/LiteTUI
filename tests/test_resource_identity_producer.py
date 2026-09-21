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
    stats = iter([_Stat(5), after])          # before, after
    monkeypatch.setattr(os, "fstat", lambda fd: next(stats))
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
