"""Serialized conservative RAM+VRAM reservations. Unknown means BLOCKED."""
from dataclasses import dataclass, field, asdict
import json
import time
import math
from uuid import uuid4
from litetui.resource_store import ResourceStore


@dataclass(frozen=True)
class ResourceSnapshot:
    timestamp: float
    ram_available: int | None
    vram_available: dict[str, int]
    reliable: bool
    loaded_models: tuple = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelDemand:
    backend: str
    endpoint: str
    model: str
    ram_peak: int | None
    vram_peak_by_device: dict[str, int]
    context: int | None = None
    concurrency: int = 1
    artifact: str = ''
    load_shape: str = ''


@dataclass(frozen=True)
class AdmissionDecision:
    status: str
    reservation_id: str | None
    snapshot: ResourceSnapshot
    required: ModelDemand
    reason: str
    options: tuple[str, ...] = ('reduce request', 'review loaded models', 'cancel')


class ResourceCoordinator:
    def __init__(self, path=None, *, telemetry):
        self.store = ResourceStore(path)
        self.telemetry = telemetry

    def snapshot(self):
        return self.telemetry()

    def reserve(self, request, owner, *, reload_lease=None):
        with self.store.transaction() as db:
            snapshot = self.telemetry()
            def blocked(reason):
                return AdmissionDecision('blocked', None, snapshot, request, reason)
            if not snapshot.reliable or type(snapshot.ram_available) is not int or snapshot.ram_available < 0 or not math.isfinite(snapshot.timestamp):
                return blocked('Reliable RAM/VRAM telemetry unavailable')
            if time.time() - snapshot.timestamp > 5 or snapshot.timestamp > time.time() + 1:
                return blocked('Memory telemetry is stale')
            if type(request.ram_peak) is not int or request.ram_peak < 0:
                return blocked('RAM peak estimate unknown')
            if any(type(v) is not int or v < 0 for v in snapshot.vram_available.values()):
                return blocked('VRAM telemetry invalid')
            if any(type(v) is not int or v < 0 for v in request.vram_peak_by_device.values()):
                return blocked('VRAM peak estimate invalid')
            for (raw_identity,) in db.execute("SELECT identity FROM models WHERE state='unloading'"):
                identity = json.loads(raw_identity)
                if all(identity[k] == getattr(request, k) for k in ('backend', 'endpoint', 'model')):
                    return blocked('Model unload is pending')
            reload_identity = None
            for (raw_identity,) in db.execute('SELECT model FROM reload_claims'):
                identity = json.loads(raw_identity)
                if all(identity[k] == getattr(request, k) for k in ('backend', 'endpoint', 'model')):
                    return blocked('Model reload is pending')
            if reload_lease is not None:
                row = db.execute('SELECT model FROM leases WHERE id=? AND owner=? AND active=1',
                                 (reload_lease, owner)).fetchone()
                if row is None:
                    return blocked('Reload requires an active owned lease')
                reload_identity = row[0]
                identity = json.loads(reload_identity)
                if any(identity[k] != getattr(request, k) for k in identity):
                    return blocked('Reload cannot change the leased load shape')
                users = db.execute('SELECT COUNT(*) FROM leases WHERE model=? AND active=1', (reload_identity,)).fetchone()[0]
                if users != 1:
                    return blocked('Reload cannot mutate a shared model')
                for (raw,) in db.execute("SELECT demand FROM reservations WHERE state='reserved'"):
                    pending = json.loads(raw)
                    if all(pending[k] == getattr(request, k) for k in ('backend', 'endpoint', 'model')):
                        return blocked('Reload conflicts with pending model users')
            ram = 0
            gpu = {}
            for (raw,) in db.execute("SELECT demand FROM reservations WHERE state IN ('reserved','leased')"):
                demand = json.loads(raw)
                same_model = all(demand[k] == getattr(request, k) for k in ('backend', 'endpoint', 'model'))
                if same_model and any(demand.get(k) != getattr(request, k) for k in ('context', 'concurrency', 'artifact', 'load_shape', 'vram_peak_by_device')):
                    return blocked('Incompatible load shape while model reserved or leased')
                ram += demand['ram_peak']
                for device, amount in demand['vram_peak_by_device'].items():
                    gpu[device] = gpu.get(device, 0) + amount
            if request.ram_peak + ram > snapshot.ram_available:
                return blocked('Insufficient unreserved RAM')
            for device, amount in request.vram_peak_by_device.items():
                if device not in snapshot.vram_available:
                    return blocked(f'Unknown GPU capacity: {device}')
                if amount + gpu.get(device, 0) > snapshot.vram_available[device]:
                    return blocked(f'Insufficient unreserved VRAM: {device}')
            reservation = uuid4().hex
            db.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                       (reservation, owner, json.dumps(asdict(request)), 'reserved', time.time()))
            if reload_identity is not None:
                db.execute('INSERT INTO reload_claims VALUES (?,?,?)', (reservation, reload_lease, reload_identity))
            return AdmissionDecision('admitted', reservation, snapshot, request, 'Capacity reserved')

    def release(self, reservation, owner):
        with self.store.transaction() as db:
            cursor = db.execute("UPDATE reservations SET state='released' WHERE id=? AND owner=? AND state='reserved'", (reservation, owner))
            released = cursor.rowcount == 1
            if released:
                db.execute('DELETE FROM reload_claims WHERE reservation=?', (reservation,))
            return released

    def load_guarded(self, request, owner, loader):
        """No load callback without reservation; allocation failure releases it."""
        decision = self.reserve(request, owner)
        if decision.status != 'admitted':
            return decision, None
        try:
            return decision, loader()
        except BaseException:
            self.release(decision.reservation_id, owner)
            raise

    def acquire_lease(self, reservation, owner, *, owned=False, keep_warm=False):
        """Convert admitted capacity to usage; callers must prove managed ownership."""
        with self.store.transaction() as db:
            row = db.execute('SELECT demand,state FROM reservations WHERE id=? AND owner=?',
                             (reservation, owner)).fetchone()
            if row is None or row[1] != 'reserved':
                raise ValueError('Reservation absent, already consumed, or owned by another instance')
            if db.execute('SELECT 1 FROM reload_claims WHERE reservation=?', (reservation,)).fetchone():
                raise ValueError('Reload peak reservation cannot become a new lease')
            demand = json.loads(row[0])
            identity = json.dumps({k: demand[k] for k in ('backend', 'endpoint', 'model', 'context', 'concurrency', 'artifact', 'load_shape', 'vram_peak_by_device')}, sort_keys=True)
            model = db.execute('SELECT owned,keep_warm,state FROM models WHERE identity=?', (identity,)).fetchone()
            if model and model[2] == 'unloading':
                raise ValueError('Model unload is pending; retry admission after reconciliation')
            if model is None:
                db.execute('INSERT INTO models VALUES (?,?,?,?)', (identity, int(owned), int(keep_warm), 'resident'))
            else:
                # A borrowed record cannot be promoted to owned by a later borrower.
                db.execute('UPDATE models SET keep_warm=MAX(keep_warm,?) WHERE identity=?',
                           (int(keep_warm), identity))
            lease = uuid4().hex
            db.execute('INSERT INTO leases VALUES (?,?,?,?,1)', (lease, reservation, owner, identity))
            db.execute("UPDATE reservations SET state='leased' WHERE id=?", (reservation,))
            return lease

    def release_lease(self, lease, owner):
        """Return unload permission only for the last managed, non-warm lease.

        This is a claim, not an unload operation: the model remains marked
        unloading until the backend result is acknowledged. New leases refuse
        that state, preventing a last-user check/acquire race.
        """
        with self.store.transaction() as db:
            row = db.execute('SELECT reservation,model FROM leases WHERE id=? AND owner=? AND active=1',
                             (lease, owner)).fetchone()
            if row is None:
                return False
            reservation, identity = row
            if db.execute('SELECT 1 FROM reload_claims WHERE model=?', (identity,)).fetchone():
                raise ValueError('Cannot release a model while reload is pending')
            db.execute('UPDATE leases SET active=0 WHERE id=?', (lease,))
            db.execute("UPDATE reservations SET state='released' WHERE id=?", (reservation,))
            users = db.execute('SELECT COUNT(*) FROM leases WHERE model=? AND active=1', (identity,)).fetchone()[0]
            owned, warm, state = db.execute('SELECT owned,keep_warm,state FROM models WHERE identity=?', (identity,)).fetchone()
            model_key = json.loads(identity)
            for (pending_raw,) in db.execute("SELECT demand FROM reservations WHERE state='reserved'"):
                pending = json.loads(pending_raw)
                if all(pending[k] == model_key[k] for k in ('backend', 'endpoint', 'model')):
                    users += 1
            if users or not owned or warm or state != 'resident':
                return False
            db.execute("UPDATE models SET state='unloading' WHERE identity=?", (identity,))
            db.execute('INSERT INTO unload_claims VALUES (?,?,?)', (identity, lease, owner))
            return True

    def complete_unload(self, lease, owner, *, success):
        """Acknowledge actual backend outcome; stale claim holders cannot clear it."""
        with self.store.transaction() as db:
            row = db.execute('SELECT model FROM unload_claims WHERE lease=? AND owner=?', (lease, owner)).fetchone()
            if row is None:
                return False
            identity = row[0]
            if success:
                db.execute('DELETE FROM models WHERE identity=?', (identity,))
            else:
                db.execute("UPDATE models SET state='resident' WHERE identity=?", (identity,))
            db.execute('DELETE FROM unload_claims WHERE model=?', (identity,))
            return True

    def reconcile(self, *, owner_alive, model_resident=None):
        """Reclaim only positively dead owners; unknown evidence retains capacity.

        Leased model capacity additionally needs confirmed non-residency.
        Neither heartbeat age nor elapsed time proves a process/model is gone.
        """
        released = []
        with self.store.transaction() as db:
            rows = db.execute("SELECT id,owner,demand,state FROM reservations WHERE state IN ('reserved','leased')").fetchall()
            for reservation, owner, raw, state in rows:
                if owner_alive(owner) is not False:
                    continue
                if model_resident is None or model_resident(json.loads(raw)) is not False:
                    continue
                if state == 'leased':
                    db.execute('UPDATE leases SET active=0 WHERE reservation=?', (reservation,))
                db.execute("UPDATE reservations SET state='released' WHERE id=?", (reservation,))
                db.execute('DELETE FROM reload_claims WHERE reservation=?', (reservation,))
                released.append(reservation)
        return released
