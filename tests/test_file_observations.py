"""Timestamp domains retain file-identity and during-read mutation checks."""
from __future__ import annotations

import hashlib
import os
import stat
import time
from types import SimpleNamespace

import pytest

from litetui import (
    engine_review_plan,
    file_observations,
    native_history,
    native_skills,
    native_work,
)


def stamp(info, *, birthtime=False):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_nlink,
            info.st_mtime_ns, info.st_birthtime_ns if birthtime else info.st_ctime_ns)


def observations():
    common = {"st_dev": 1, "st_ino": 2, "st_mode": stat.S_IFREG | 0o600, "st_size": 18,
              "st_nlink": 1, "st_mtime_ns": 200, "st_birthtime_ns": 100}
    return [SimpleNamespace(**common, st_ctime_ns=value) for value in (100, 200, 200, 100)]


def test_windows_uses_birthtime_only_across_apis(monkeypatch):
    monkeypatch.setattr(file_observations, "_WINDOWS", True)
    values = observations()
    assert file_observations.stable_read(*values, stamp=stamp)
    assert stamp(values[0]) != stamp(values[1])  # Default receipt stamps are untouched.


@pytest.mark.parametrize("index,field", [(0, "st_ctime_ns"), (3, "st_ctime_ns"),
    (1, "st_ctime_ns"), (2, "st_ctime_ns"), (2, "st_birthtime_ns"),
    (1, "st_ino"), (2, "st_dev"), (3, "st_size"), (1, "st_mtime_ns"),
    (3, "st_mode"), (1, "st_nlink")])
def test_mutation_or_replacement_never_becomes_timestamp_compatibility(monkeypatch, index, field):
    monkeypatch.setattr(file_observations, "_WINDOWS", True)
    values = observations()
    setattr(values[index], field, getattr(values[index], field) + 1)
    assert not file_observations.stable_read(*values, stamp=stamp)


@pytest.mark.parametrize("missing", [(0, 1, 2, 3), (1,)])
@pytest.mark.parametrize("matching_ctime", [False, True])
def test_missing_or_mixed_birthtime_keeps_original_full_comparison(monkeypatch, missing, matching_ctime):
    monkeypatch.setattr(file_observations, "_WINDOWS", True)
    values = observations()
    for index in missing:
        del values[index].st_birthtime_ns
    if matching_ctime:
        for value in values:
            value.st_ctime_ns = 100
    assert file_observations.stable_read(*values, stamp=stamp) is matching_ctime


@pytest.mark.parametrize("invalid", [None, True, 100.0, "100"])
def test_malformed_birthtime_cannot_enable_compatibility(monkeypatch, invalid):
    monkeypatch.setattr(file_observations, "_WINDOWS", True)
    values = observations()
    for value in values:
        value.st_ctime_ns = 100
    values[1].st_birthtime_ns = invalid
    assert not file_observations.stable_read(*values, stamp=stamp)


def test_non_windows_keeps_full_cross_api_ctime_comparison(monkeypatch):
    monkeypatch.setattr(file_observations, "_WINDOWS", False)
    assert not file_observations.stable_read(*observations(), stamp=stamp)


@pytest.mark.parametrize("reader", ["history", "work", "skill", "engine"])
@pytest.mark.parametrize("write", ["append", "truncate"])
def test_actual_rewritten_files_remain_readable_without_executing_them(tmp_path, reader, write):
    path = tmp_path / "source.jsonl"
    path.write_bytes(b"\n")
    time.sleep(0.025)  # Separate creation from the next Windows metadata change.
    payload = b'{"type":"snapshot","messages":[{"role":"user","content":"private"}]}\n'
    if write == "append":
        with path.open("ab") as stream:
            stream.write(payload)
        payload = b"\n" + payload
    else:
        path.write_bytes(payload)
    before = path.stat()
    if reader == "history":
        assert native_history.capture_file(path).raw == payload
    elif reader == "work":
        assert native_work._read(path) == (payload, hashlib.sha256(payload).hexdigest())
    elif reader == "skill":
        raw, observed, resolved = native_skills._read(path)
        assert raw == payload and resolved == str(path.resolve())
        assert native_skills._identity(observed) == native_skills._identity(before)
    else:
        evidence = engine_review_plan.file_evidence(path, complete=True)
        assert evidence[2] == engine_review_plan._identity(before)
        assert evidence[3] == hashlib.sha256(payload).hexdigest() and evidence[5] == before.st_mode
    assert path.read_bytes() == payload


@pytest.mark.parametrize("suffix", [".exe", ".txt"])
def test_engine_preserves_filename_mode_policy_without_running_private_binary(tmp_path, suffix):
    path = tmp_path / ("private" + suffix)
    path.write_bytes(b"private original")
    time.sleep(0.025)
    path.write_bytes(b"private updated")
    with path.open("rb") as stream:
        by_path, by_handle = path.stat(), os.fstat(stream.fileno())
    evidence = engine_review_plan.file_evidence(path)
    assert evidence[2] == engine_review_plan._identity(by_path)
    assert evidence[5] == by_path.st_mode
    assert stat.S_IFMT(by_path.st_mode) == stat.S_IFMT(by_handle.st_mode)
    # Whether a platform infers executable bits from the suffix is independent
    # from the read's authority; the original caller checks those path-to-path.
    assert path.read_bytes() == b"private updated"


@pytest.mark.parametrize("reader", ["history", "work", "skill", "engine"])
def test_each_reader_rejects_handle_change_time_drift_with_same_size_and_mtime(tmp_path, monkeypatch, reader):
    path = tmp_path / "changing.jsonl"
    path.write_bytes(b'{"type":"snapshot","messages":[]}\n')
    selected = path.stat()
    real_fstat = os.fstat
    calls = 0
    def changing_fstat(descriptor):
        nonlocal calls
        info = real_fstat(descriptor)
        if (info.st_dev, info.st_ino) != (selected.st_dev, selected.st_ino):
            return info
        calls += 1
        if calls != 2:
            return info
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        fields["st_ctime_ns"] += 1
        return SimpleNamespace(**fields)
    monkeypatch.setattr(os, "fstat", changing_fstat)
    with pytest.raises(ValueError, match="changed"):
        if reader == "history":
            native_history.capture_file(path)
        elif reader == "work":
            native_work._read(path)
        elif reader == "skill":
            native_skills._read(path)
        else:
            engine_review_plan.file_evidence(path)
    assert calls == 2
