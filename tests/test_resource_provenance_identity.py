"""Calibration provenance carried through ModelDemand + persisted coordinator identity.

Real ResourceCoordinator over a temp SQLite, fake telemetry, NO backend/model
load. Proves:
- provenance validates as all-blank (legacy) OR all-three-nonblank; partial/bool
  is BLOCKED before any reservation;
- equal footprint + a new build/fingerprint/host is incompatible with a prior
  lease (blocked, no lease granted), and stays so across a store reopen;
- an OLD persisted row (provenance keys absent) blocks a calibrated reserve AND
  reload with no crash and no ownership grant;
- provenance participates in ModelDemand equality (session lease compatibility).
"""
from __future__ import annotations

import json
import time

import pytest

from litetui.resource_admission import (
    ModelDemand,
    ResourceCoordinator,
    ResourceSnapshot,
    _identity_dict,
)

OWNER = "inst-1"


def _telemetry():
    return ResourceSnapshot(timestamp=time.time(), ram_available=10**12,
                            vram_available={"GPU-A": 10**12}, reliable=True)


def _coord(tmp_path):
    return ResourceCoordinator(tmp_path / "res.sqlite3", telemetry=_telemetry)


def _demand(**over) -> ModelDemand:
    base = dict(backend="ninfer", endpoint="http://h:9000", model="qwen", ram_peak=1,
                vram_peak_by_device={"GPU-A": 1}, context=32768, concurrency=1,
                artifact="q.ninfer", load_shape="nvfp4",
                host="HOST-1", build="build-9", artifact_fingerprint="sha:abc")
    base.update(over)
    return ModelDemand(**base)


def _lease(coord, demand):
    decision = coord.reserve(demand, OWNER)
    assert decision.status == "admitted", decision.reason
    return coord.acquire_lease(decision.reservation_id, OWNER, owned=True)


# ── provenance validation ───────────────────────────────────────────────────

@pytest.mark.parametrize("prov", [
    dict(host="HOST-1", build="", artifact_fingerprint="sha:abc"),   # partial
    dict(host="", build="build-9", artifact_fingerprint=""),          # partial
    dict(host="HOST-1", build=True, artifact_fingerprint="sha:abc"),  # bool
    dict(host=" ", build="build-9", artifact_fingerprint="sha:abc"),  # blank-ish
])
def test_partial_or_malformed_provenance_is_blocked(tmp_path, prov):
    coord = _coord(tmp_path)
    decision = coord.reserve(_demand(**prov), OWNER)
    assert decision.status == "blocked"
    assert "provenance" in decision.reason


def test_all_blank_legacy_and_all_named_calibrated_both_admit(tmp_path):
    coord = _coord(tmp_path)
    legacy = coord.reserve(_demand(host="", build="", artifact_fingerprint=""), OWNER)
    assert legacy.status == "admitted"
    calibrated = coord.reserve(_demand(model="qwen2"), OWNER)   # distinct model, full prov
    assert calibrated.status == "admitted"


# ── cross-provenance incompatibility against a prior lease ──────────────────

@pytest.mark.parametrize("drift", [
    dict(build="build-10"),
    dict(artifact_fingerprint="sha:different"),
    dict(host="HOST-2"),
])
def test_new_build_fingerprint_or_host_is_incompatible_with_prior_lease(tmp_path, drift):
    coord = _coord(tmp_path)
    _lease(coord, _demand())                       # HOST-1 / build-9 / sha:abc leased
    blocked = coord.reserve(_demand(**drift), OWNER)
    assert blocked.status == "blocked"
    assert "Incompatible" in blocked.reason


def test_identical_provenance_shares_the_lease(tmp_path):
    coord = _coord(tmp_path)
    _lease(coord, _demand())
    same = coord.reserve(_demand(), OWNER)          # identical identity => compatible
    assert same.status == "admitted"


# ── persisted across a store reopen ─────────────────────────────────────────

def test_persisted_identity_requires_provenance_after_reopen(tmp_path):
    coord1 = _coord(tmp_path)
    _lease(coord1, _demand())
    coord2 = _coord(tmp_path)                        # fresh coordinator, same file
    blocked = coord2.reserve(_demand(build="build-10"), OWNER)
    assert blocked.status == "blocked" and "Incompatible" in blocked.reason
    ok = coord2.reserve(_demand(), OWNER)            # same provenance still matches
    assert ok.status == "admitted"


# ── legacy persisted row (provenance keys ABSENT) ───────────────────────────

def _insert_legacy_lease(coord):
    """A pre-change row: identity + demand JSON with NO provenance keys."""
    legacy_demand = dict(backend="ninfer", endpoint="http://h:9000", model="qwen",
                         ram_peak=1, vram_peak_by_device={"GPU-A": 1}, context=32768,
                         concurrency=1, artifact="q.ninfer", load_shape="nvfp4")
    identity = json.dumps({k: legacy_demand[k] for k in
                           ('backend', 'endpoint', 'model', 'context', 'concurrency',
                            'artifact', 'load_shape', 'vram_peak_by_device')}, sort_keys=True)
    with coord.store.transaction() as db:
        db.execute('INSERT INTO models VALUES (?,?,?,?)', (identity, 1, 0, 'resident'))
        db.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                   ('r-old', OWNER, json.dumps(legacy_demand), 'leased', time.time()))
        db.execute('INSERT INTO leases VALUES (?,?,?,?,1)', ('l-old', 'r-old', OWNER, identity))
    return identity


def test_calibrated_reserve_against_legacy_row_is_blocked_no_crash(tmp_path):
    coord = _coord(tmp_path)
    _insert_legacy_lease(coord)
    decision = coord.reserve(_demand(), OWNER)      # calibrated, same backend/endpoint/model
    assert decision.status == "blocked" and "Incompatible" in decision.reason


def test_reload_against_legacy_lease_is_blocked_no_crash(tmp_path):
    coord = _coord(tmp_path)
    _insert_legacy_lease(coord)
    decision = coord.reserve(_demand(), OWNER, reload_lease="l-old")
    assert decision.status == "blocked"
    assert "Reload cannot change the leased load shape" in decision.reason


def test_calibrated_load_does_not_migrate_legacy_row_into_owned(tmp_path):
    coord = _coord(tmp_path)
    legacy_identity = _insert_legacy_lease(coord)
    # A calibrated demand for a DIFFERENT model can lease freely; the legacy row
    # must still be its own distinct identity, never rewritten/adopted.
    _lease(coord, _demand(model="qwen-new"))
    with coord.store.transaction() as db:
        rows = {r[0] for r in db.execute("SELECT identity FROM models")}
    assert legacy_identity in rows                  # untouched
    assert _identity_dict(_demand(model="qwen-new")) != json.loads(legacy_identity)


# ── session demand equality carries provenance ──────────────────────────────

def test_provenance_participates_in_demand_equality():
    assert _demand(build="a") != _demand(build="b")
    assert _demand(host="HOST-1") != _demand(host="HOST-2")
    assert _demand() == _demand()
