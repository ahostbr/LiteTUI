"""Fail-closed calibration envelope/identity gate for WS3 admission.

Pure unit fixtures — a temp JSON calibration file, a fake identity resolver, and
fake coordinators. NO live measurement, NO backend, NO model load. Proves:
- an uncalibrated identity yields None -> ModelResourceSession.load BLOCKS with no
  reservation and no loader call;
- a calibrated identity yields the exact measured ModelDemand;
- cross-host / cross-build / cross-device / cross-fingerprint / cross-endpoint
  numbers never match (no calibration from another machine or build);
- malformed, partial, duplicate/ambiguous, unknown-schema and unreadable inputs
  all fail closed;
- the envelope is immutable against input-map and returned-demand mutation.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from litetui.resource_calibration import (
    SCHEMA,
    CalibrationStore,
    LoadEnvelope,
    calibration_path,
    make_demand_for,
)
from litetui.model_resource_session import AdmissionBlocked, ModelResourceSession


def _identity(**over) -> dict:
    base = dict(
        host="HOST-1", backend="ninfer", build="ninfer-serve-9.9",
        endpoint="http://127.0.0.1:9000", model="qwen3-27b",
        context=32768, concurrency=1, artifact="qwen3.ninfer",
        artifact_fingerprint="sha256:abc", load_shape="nvfp4",
        device_set=["GPU-uuid-A"],
    )
    base.update(over)
    return base


def _record(identity: dict, ram_peak, vram) -> dict:
    return {**identity, "ram_peak": ram_peak, "vram_peak_by_device": vram}


def _write_store(path, records, schema=SCHEMA):
    path.write_text(json.dumps({"schema": schema, "records": records}), encoding="utf-8")


def _store_with(tmp_path, record_identity, ram_peak=1, vram=None):
    p = tmp_path / "cal.json"
    _write_store(p, [_record(record_identity, ram_peak, vram or {"GPU-uuid-A": 2})])
    return CalibrationStore(p)


# ── envelope validation + immutability ──────────────────────────────────────

@pytest.mark.parametrize("bad", [
    dict(ram_peak=-1),
    dict(ram_peak=True),                              # bool is not int
    dict(vram_peak_by_device={"GPU-uuid-A": -1}),
    dict(vram_peak_by_device={"GPU-uuid-A": 1.5}),
    dict(vram_peak_by_device={}),
    dict(vram_peak_by_device={" ": 1}),
    dict(vram_peak_by_device={"GPU-uuid-B": 1}),      # placement != device_set
    dict(concurrency=True),
    dict(concurrency=0),
    dict(context=0),
    dict(host=" "),
    dict(build=""),
    dict(artifact_fingerprint=" "),
    dict(device_set=[]),
    dict(device_set=["GPU-uuid-A", "GPU-uuid-A"]),
])
def test_envelope_rejects_malformed_input(bad):
    args = dict(_identity(), ram_peak=1, vram_peak_by_device={"GPU-uuid-A": 2})
    args.update(bad)
    with pytest.raises(ValueError):
        LoadEnvelope(**args)


def test_envelope_is_frozen_and_vram_is_read_only():
    env = LoadEnvelope(**_identity(), ram_peak=1, vram_peak_by_device={"GPU-uuid-A": 2})
    with pytest.raises(Exception):
        env.ram_peak = 5
    with pytest.raises(TypeError):
        env.vram_peak_by_device["GPU-uuid-A"] = 9


def test_input_map_mutation_does_not_leak_into_envelope():
    vram = {"GPU-uuid-A": 2}
    env = LoadEnvelope(**_identity(), ram_peak=1, vram_peak_by_device=vram)
    vram["GPU-uuid-A"] = 999
    vram["GPU-uuid-B"] = 5
    assert dict(env.vram_peak_by_device) == {"GPU-uuid-A": 2}


def test_returned_demand_is_isolated_from_envelope():
    env = LoadEnvelope(**_identity(), ram_peak=7, vram_peak_by_device={"GPU-uuid-A": 2})
    demand = env.to_demand()
    demand.vram_peak_by_device["GPU-uuid-A"] = 999
    assert dict(env.vram_peak_by_device) == {"GPU-uuid-A": 2}
    assert env.to_demand().vram_peak_by_device == {"GPU-uuid-A": 2}
    assert env.to_demand().ram_peak == 7


# ── store lookup: exact match, provenance mismatch, fail-closed inputs ───────

def test_exact_identity_match_returns_envelope(tmp_path):
    store = _store_with(tmp_path, _identity(), ram_peak=8, vram={"GPU-uuid-A": 20})
    env = store.envelope_for(_identity())
    assert env is not None
    assert env.ram_peak == 8
    assert dict(env.vram_peak_by_device) == {"GPU-uuid-A": 20}


@pytest.mark.parametrize("mismatch", [
    dict(host="HOST-2"),
    dict(build="ninfer-serve-OTHER"),
    dict(artifact_fingerprint="sha256:zzz"),
    dict(device_set=["GPU-uuid-B"]),
    dict(endpoint="http://127.0.0.1:9999"),
    dict(context=65536),
    dict(model="other-model"),
    dict(concurrency=2),
])
def test_cross_identity_mismatch_is_uncalibrated(tmp_path, mismatch):
    store = _store_with(tmp_path, _identity())
    assert store.envelope_for(_identity(**mismatch)) is None


def test_duplicate_matching_records_are_ambiguous(tmp_path):
    ident = _identity()
    p = tmp_path / "cal.json"
    _write_store(p, [_record(ident, 1, {"GPU-uuid-A": 2}), _record(ident, 3, {"GPU-uuid-A": 4})])
    assert CalibrationStore(p).envelope_for(ident) is None


def test_malformed_matching_record_is_uncalibrated(tmp_path):
    ident = _identity()
    p = tmp_path / "cal.json"
    _write_store(p, [_record(ident, -5, {"GPU-uuid-A": 2})])   # matches identity, bad peak
    assert CalibrationStore(p).envelope_for(ident) is None


def test_absent_file_is_uncalibrated(tmp_path):
    assert CalibrationStore(tmp_path / "nope.json").envelope_for(_identity()) is None


def test_unknown_schema_is_uncalibrated(tmp_path):
    p = tmp_path / "cal.json"
    _write_store(p, [_record(_identity(), 1, {"GPU-uuid-A": 2})], schema="something.else")
    assert CalibrationStore(p).envelope_for(_identity()) is None


def test_invalid_json_is_uncalibrated(tmp_path):
    p = tmp_path / "cal.json"
    p.write_text("{ not json", encoding="utf-8")
    assert CalibrationStore(p).envelope_for(_identity()) is None


def test_partial_query_identity_is_rejected(tmp_path):
    store = _store_with(tmp_path, _identity())
    incomplete = _identity()
    del incomplete["build"]
    assert store.envelope_for(incomplete) is None


def test_calibration_path_reuses_resource_store_root():
    from litetui.resource_store import coordinator_path
    assert calibration_path().parent == coordinator_path().parent
    assert calibration_path().name == "resource_calibration.json"


# ── integration: demand_for + ModelResourceSession, fail-closed ─────────────

class _NoAdmitCoordinator:
    def reserve(self, *a, **k):
        raise AssertionError("reserve() called despite uncalibrated demand")


class _AdmitCoordinator:
    def __init__(self):
        self.reserved = []

    def reserve(self, demand, owner, *, reload_lease=None):
        self.reserved.append(demand)
        return SimpleNamespace(status="admitted", reservation_id="r1", reason="ok",
                               snapshot=SimpleNamespace(ram_available=1, vram_available={}))

    def acquire_lease(self, reservation, owner, *, owned=False, keep_warm=False):
        return "lease-1"


@pytest.mark.asyncio
async def test_uncalibrated_blocks_with_no_reservation_or_loader(tmp_path):
    store = CalibrationStore(tmp_path / "absent.json")
    demand_for = make_demand_for(store, lambda key: _identity())
    session = ModelResourceSession(_NoAdmitCoordinator(), "inst", demand_for=demand_for)
    ran = []
    with pytest.raises(AdmissionBlocked):
        async with session.load("qwen3-27b"):
            ran.append(True)
    assert ran == []


@pytest.mark.asyncio
async def test_calibrated_demand_reaches_coordinator_and_loader(tmp_path):
    ident = _identity()
    store = _store_with(tmp_path, ident, ram_peak=8 * 1024**3, vram={"GPU-uuid-A": 20 * 1024**3})
    coord = _AdmitCoordinator()
    session = ModelResourceSession(coord, "inst", demand_for=make_demand_for(store, lambda key: ident))
    ran = []
    async with session.load("qwen3-27b"):
        ran.append(True)
    assert ran == [True]
    assert len(coord.reserved) == 1
    demand = coord.reserved[0]
    assert demand.ram_peak == 8 * 1024**3
    assert demand.vram_peak_by_device == {"GPU-uuid-A": 20 * 1024**3}
    assert demand.backend == "ninfer" and demand.model == "qwen3-27b" and demand.load_shape == "nvfp4"


@pytest.mark.asyncio
async def test_unresolvable_key_blocks(tmp_path):
    store = _store_with(tmp_path, _identity())
    for resolver in (lambda key: None, lambda key: (_ for _ in ()).throw(RuntimeError("boom"))):
        session = ModelResourceSession(_NoAdmitCoordinator(), "inst",
                                       demand_for=make_demand_for(store, resolver))
        with pytest.raises(AdmissionBlocked):
            async with session.load("qwen3-27b"):
                pass
