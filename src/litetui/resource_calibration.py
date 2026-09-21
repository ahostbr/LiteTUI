"""Immutable, validated model-load envelopes — the only source of admission demand.

WS3 admission (`resource_admission.ModelDemand`) needs a per-model RAM/VRAM peak.
There is deliberately NO default and NO heuristic: a load whose peak has not been
MEASURED must be BLOCKED, never admitted on a guessed number ("no arbitrary '4B
fits'"). This module maps a *fully qualified* model identity to a calibrated
envelope, or to None — and None is exactly what makes `ModelResourceSession.load`
raise `AdmissionBlocked` before any reservation or loader runs.

Cross-machine / cross-build safety: the identity a calibration is keyed on
includes the HOST, the backend BUILD/provenance, the ARTIFACT FINGERPRINT and the
stable DEVICE-SET placement — not the display name. A number measured on another
machine, another engine build, or a different weights file does not match and is
therefore never used.

Trust boundary (documented deliberately): this module trusts the calibration file
as *supplied measured evidence*. It validates SHAPE and IDENTITY MATCH; it does
NOT and cannot prove the numbers were actually measured on this host — that is the
gated live probe's job. A missing file, an unknown schema, invalid JSON, a read
error, a malformed record, a partial match, or DUPLICATE/ambiguous matches all
resolve to None. Fail closed, always.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType

from litetui.resource_admission import ModelDemand
from litetui.resource_store import coordinator_path

#: Bumped when the on-disk record shape changes. An unknown/absent schema string
#: yields zero records (fail closed) rather than a best-effort parse.
SCHEMA = "litetui.resource_calibration.v1"

#: Everything that changes the memory footprint or the provenance of the number,
#: EXCEPT the measured peaks themselves.
IDENTITY_FIELDS = (
    "host", "backend", "build", "endpoint", "model",
    "context", "concurrency", "artifact", "artifact_fingerprint",
    "load_shape", "device_set",
)

#: Identity strings that must be present and non-blank (provenance-bearing).
_REQUIRED_STR = ("host", "backend", "build", "endpoint", "model", "artifact_fingerprint")


def calibration_path() -> Path:
    # Reuse resource_store's single user-wide root (NOT LITETUI_DATA_ROOT, NOT a
    # new competing root): physical memory is shared across workspaces.
    return coordinator_path().parent / "resource_calibration.json"


def _canonical_identity(identity):
    """Validate + normalize an identity, or None if it is not fully well-formed.

    `device_set` is normalized to a sorted tuple so equality does not depend on
    the order a caller or a file listed the devices in. bool is rejected for the
    int fields (``type(True) is int`` is False, so ``type(x) is not int`` catches
    it). Partial identities (missing/extra keys) are rejected outright.
    """
    if not isinstance(identity, dict) or set(identity) != set(IDENTITY_FIELDS):
        return None
    if any(not isinstance(identity[k], str) or not identity[k].strip() for k in _REQUIRED_STR):
        return None
    if not isinstance(identity["artifact"], str) or not isinstance(identity["load_shape"], str):
        return None
    if type(identity["concurrency"]) is not int or identity["concurrency"] <= 0:
        return None
    if identity["context"] is not None and (type(identity["context"]) is not int or identity["context"] <= 0):
        return None
    devices = identity["device_set"]
    if not isinstance(devices, (list, tuple)) or not devices:
        return None
    if any(not isinstance(d, str) or not d.strip() for d in devices):
        return None
    if len(set(devices)) != len(devices):
        return None
    canon = dict(identity)
    canon["device_set"] = tuple(sorted(devices))
    return canon


@dataclass(frozen=True)
class LoadEnvelope:
    """A measured RAM/VRAM peak for one exact, provenance-qualified model identity.

    Construction is the validation gate: an out-of-range, malformed, or
    provenance-inconsistent field raises ValueError rather than silently becoming
    a usable-looking demand. The VRAM map and device set are stored read-only /
    immutable so a holder cannot mutate a calibrated peak after the fact.
    """
    host: str
    backend: str
    build: str
    endpoint: str
    model: str
    artifact_fingerprint: str
    device_set: tuple
    ram_peak: int
    vram_peak_by_device: dict
    context: int | None = None
    concurrency: int = 1
    artifact: str = ""
    load_shape: str = ""

    def __post_init__(self):
        canon = _canonical_identity({k: getattr(self, k) for k in IDENTITY_FIELDS})
        if canon is None:
            raise ValueError("invalid calibration identity")
        if type(self.ram_peak) is not int or self.ram_peak < 0:
            raise ValueError("ram_peak must be a non-negative int (measured, never guessed)")
        vram = self.vram_peak_by_device
        if not isinstance(vram, dict) or not vram:
            raise ValueError("vram_peak_by_device must be a non-empty {device: bytes} map")
        if any(not isinstance(k, str) or not k.strip() for k in vram):
            raise ValueError("blank GPU device identity in calibration")
        if any(type(v) is not int or v < 0 for v in vram.values()):
            raise ValueError("each vram peak must be a non-negative int")
        # The declared placement must match the devices actually measured — a
        # record that claims device set X while measuring Y is self-inconsistent.
        if tuple(sorted(vram)) != canon["device_set"]:
            raise ValueError("device_set does not match the measured vram devices")
        # Defensive immutable copies: neither the caller's original dict nor the
        # returned tuple can mutate what this envelope certifies.
        object.__setattr__(self, "device_set", canon["device_set"])
        object.__setattr__(self, "vram_peak_by_device", MappingProxyType(dict(vram)))

    def identity(self) -> dict:
        ident = {k: getattr(self, k) for k in IDENTITY_FIELDS}
        ident["device_set"] = tuple(self.device_set)
        return ident

    def to_demand(self) -> ModelDemand:
        # Fresh dict every call: the coordinator requires a mutable dict, and it
        # must never be able to write back into this envelope. Provenance
        # (host/build/artifact_fingerprint) is carried through so the persisted
        # coordinator identity distinguishes this exact machine/build/weights.
        return ModelDemand(
            backend=self.backend, endpoint=self.endpoint, model=self.model,
            ram_peak=self.ram_peak, vram_peak_by_device=dict(self.vram_peak_by_device),
            context=self.context, concurrency=self.concurrency,
            artifact=self.artifact, load_shape=self.load_shape,
            host=self.host, build=self.build, artifact_fingerprint=self.artifact_fingerprint,
        )


#: A record must carry EXACTLY these keys — the identity plus the two peaks. An
#: absent key is never synthesized (a missing `context` must not read as None and
#: match a None-context demand), and an extra key is a schema mismatch.
_REQUIRED_RECORD_KEYS = set(IDENTITY_FIELDS) | {"ram_peak", "vram_peak_by_device"}


def _reject_duplicate_keys(pairs):
    """object_pairs_hook: raise on a duplicate key at ANY nesting level.

    json.loads otherwise keeps the last value for a repeated key, which would let
    a record silently override its own ram_peak / device / identity and defeat the
    ambiguity fail-closed guarantee. Raising here makes the whole file unreadable
    -> no calibration.
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _record_identity(record):
    """Canonical identity of a record, or None unless its keys are EXACTLY right.

    Uses record[k] after an exact-key check rather than record.get(k), so a record
    missing or gaining a field is rejected instead of matching on a synthesized
    value.
    """
    if not isinstance(record, dict) or set(record) != _REQUIRED_RECORD_KEYS:
        return None
    return _canonical_identity({k: record[k] for k in IDENTITY_FIELDS})


class CalibrationStore:
    """Read-only, versioned lookup over calibration records. Never writes.

    The file is EXACTLY ``{"schema": SCHEMA, "records": [ {identity..., ram_peak,
    vram_peak_by_device}, ... ]}`` — unknown top-level keys, an unknown schema,
    invalid JSON, a duplicate key anywhere, a record with the wrong key set, and
    any zero-or-many identity match all resolve to None. Exactly-one exact-key,
    exact-identity match is required.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else calibration_path()

    def _records(self) -> list:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"),
                             object_pairs_hook=_reject_duplicate_keys)
        except (FileNotFoundError, OSError, ValueError):
            return []
        # Strict top-level: exactly {schema, records}. A stray key is unknown
        # format, not a best-effort parse.
        if not isinstance(raw, dict) or set(raw) != {"schema", "records"} or raw["schema"] != SCHEMA:
            return []
        return raw["records"] if isinstance(raw["records"], list) else []

    def envelope_for(self, identity) -> LoadEnvelope | None:
        canon = _canonical_identity(identity)
        if canon is None:
            return None
        matches = [record for record in self._records() if _record_identity(record) == canon]
        if len(matches) != 1:
            # 0 = uncalibrated, >1 = ambiguous/duplicate — both fail closed.
            return None
        record = matches[0]
        try:
            return LoadEnvelope(
                ram_peak=record["ram_peak"],
                vram_peak_by_device=record["vram_peak_by_device"],
                **canon,
            )
        except (KeyError, TypeError, ValueError):
            return None


def make_demand_for(store: CalibrationStore, resolve_identity):
    """Build the `demand_for(key)` ModelResourceSession consumes.

    `resolve_identity(key)` -> a fully-qualified identity dict (host, build,
    endpoint, device set, artifact fingerprint, load config for `key`), supplied
    by the App from verified backend introspection — never guessed from a display
    name. A key that will not resolve, or whose identity has no unique
    calibration, yields None, and None makes the session block the load.
    """
    def demand_for(key):
        try:
            identity = resolve_identity(key)
        except Exception:  # noqa: BLE001 - an unresolvable model is uncalibrated
            return None
        if not isinstance(identity, dict):
            return None
        envelope = store.envelope_for(identity)
        return envelope.to_demand() if envelope is not None else None

    return demand_for
