"""Instance model leases around backend load operations; no implicit eviction."""
import asyncio
import inspect
from contextlib import asynccontextmanager
from litetui.llm_backend import VramRefused


class AdmissionBlocked(VramRefused):
    pass


async def _maybe_await(value):
    """Let an injected evidence callback be sync OR async without the caller caring."""
    return await value if inspect.isawaitable(value) else value


class ModelResourceSession:
    def __init__(self, coordinator, owner, *, demand_for, owned=False, keep_warm=False):
        self.coordinator = coordinator
        self.owner = owner
        self.demand_for = demand_for
        self.owned = owned
        self.keep_warm = keep_warm
        self.leases = {}
        self.unload_claims = {}
        self.unsettled_loads = {}
        self.active_loads = set()
        self._closing = False
        self._close_lock = asyncio.Lock()

    @asynccontextmanager
    async def load(self, key, *, reload=False):
        if self._closing:
            raise AdmissionBlocked('BLOCKED: session is closing; no new loads')
        if key in self.unsettled_loads:
            raise AdmissionBlocked('BLOCKED: previous loader outcome has not been confirmed')
        demand = self.demand_for(key)
        if demand is None:
            raise AdmissionBlocked('BLOCKED: calibrated RAM/VRAM peak estimate unavailable; review model capacity or cancel')
        prior = self.leases.get(key)
        if prior is not None:
            if prior[1] != demand:
                raise AdmissionBlocked('BLOCKED: release current model lease before changing its load shape')
            if not reload:
                raise AdmissionBlocked('BLOCKED: model already leased; explicit reload requires peak re-admission')
        elif reload:
            raise AdmissionBlocked('BLOCKED: reload requires an existing lease')
        decision = self.coordinator.reserve(demand, self.owner, reload_lease=prior[0] if reload else None)
        if decision.status != 'admitted':
            raise AdmissionBlocked(f'BLOCKED: {decision.reason}; RAM={decision.snapshot.ram_available}; VRAM={decision.snapshot.vram_available}; options={decision.options}')
        # Claim before yielding: another task (or reentrant caller) must not
        # dispatch the same loader while the first outcome is still unknown.
        self.unsettled_loads[key] = decision.reservation_id
        self.active_loads.add(key)
        try:
            yield
        finally:
            self.active_loads.discard(key)
        # Keep the claim on loader cancellation OR failed post-load accounting.
        # Only a fully committed success may clear it; absence settlement is
        # the explicit recovery path for every uncertain outcome.
        if reload:
            if self.coordinator.release(decision.reservation_id, self.owner) is not True:
                raise AdmissionBlocked('BLOCKED: reload capacity accounting was not confirmed')
        else:
            lease = self.coordinator.acquire_lease(decision.reservation_id, self.owner,
                         owned=self.owned, keep_warm=self.keep_warm)
            self.leases[key] = (lease, demand)
        del self.unsettled_loads[key]

    def settle_failed_load(self, key, *, absent):
        """Backend must prove loading has stopped AND model is absent.

        A transient empty catalogue while a worker is still loading is not
        proof. Unknown evidence retains capacity and any reload exclusion.
        """
        if key in self.active_loads:
            return False
        reservation = self.unsettled_loads.get(key)
        if absent is not True or reservation is None:
            return False
        if not self.coordinator.settle_absent_load(reservation, self.owner):
            return False
        self.leases.pop(key, None)
        del self.unsettled_loads[key]
        return True

    def release(self, key):
        prior = self.leases.get(key)
        if prior is None:
            return False
        permitted = self.coordinator.release_lease(prior[0], self.owner)
        del self.leases[key]
        if permitted:
            self.unload_claims[key] = prior[0]
        return permitted

    def complete_unload(self, key, *, success):
        claim = self.unload_claims.get(key)
        if claim is None:
            return False
        acknowledged = self.coordinator.complete_unload(claim, self.owner, success=success)
        if acknowledged:
            del self.unload_claims[key]
        return acknowledged

    async def close(self, *, quiescent, unload):
        """Drive this session's models to a safe terminal state with INJECTED backend
        evidence; return an inspectable {key: outcome} report, NEVER a bool.

        Blocks new loads (``_closing``) and serializes concurrent/repeated calls (a
        retry re-drives whatever was retained, including a prior close's unresolved
        unload claim, under FRESH quiescence). Everything uncertain is retained and
        named — no timeout-as-dead, no mass release. ``quiescent(key)`` and
        ``unload(key)`` may be sync or async; ``quiescent`` returning anything but
        ``True`` retains, a ``quiescent`` error is a structured ``quiescence_unknown``
        (not a discard), and ``unload`` proves success ONLY by returning ``True`` AND
        the coordinator acknowledging — a normal/None return or an exception retains
        the recoverable claim (``complete_unload(success=False)`` is never called on
        an uncertain outcome, since it would reset the model to resident).

        Borrowed and keep-warm models are NEVER unloaded: unload is gated on
        ``self.owned and not self.keep_warm`` — the SESSION's contract — so a coordinator
        permission granted off the model ROW (owned by the original owner while THIS
        caller borrowed) does not become an unload here.

        ponytail: the ``unload`` adapter must be idempotent — a retry re-issues unload
        for a still-claimed key, so unloading an already-gone model must be a no-op.
        """
        self._closing = True
        report = {}
        async with self._close_lock:
            keys = (set(self.leases) | set(self.active_loads)
                    | set(self.unsettled_loads) | set(self.unload_claims))
            for key in keys:
                report[key] = await self._close_key(key, quiescent, unload)
        return report

    async def _close_key(self, key, quiescent, unload):
        if key in self.active_loads or key in self.unsettled_loads:
            return 'in_flight'          # a loader is live/unsettled — retain, never force
        try:
            q = await _maybe_await(quiescent(key))
        except asyncio.CancelledError:
            raise
        except Exception:               # noqa: BLE001 - unknown quiescence is NOT a discard
            return 'quiescence_unknown'
        if q is not True:
            return 'not_quiescent'
        # An outstanding claim from a prior close (already out of leases): re-drive it.
        if key not in self.leases:
            if key in self.unload_claims:
                return await self._drive_unload(key, unload)
            return 'gone'
        try:
            permitted = self.release(key)   # drops OUR lease; stores a claim iff permitted
        except ValueError:
            return 'reload_pending'         # release_lease refuses while a reload is pending
        if not (self.owned and not self.keep_warm):
            # Borrowed or keep-warm: never unload. A permission granted off the model
            # row is not ours to act on — retain the claim for the true owner/reconcile.
            return 'released_unowned_claim' if permitted else 'released'
        if not permitted:
            return 'released_shared'        # another active lease holds it resident
        return await self._drive_unload(key, unload)

    async def _drive_unload(self, key, unload):
        if self.unload_claims.get(key) is None:
            return 'gone'
        # 🔴 UNCONDITIONAL OWNERSHIP GUARD, on EVERY unload path. A borrowed session
        # can hold a claim it must never act on: coordinator.release_lease grants
        # permission off the MODEL ROW (owned by the original owner) even when THIS
        # caller borrowed, so the first close stored a claim and reported
        # released_unowned_claim. Without this guard a SECOND close (key out of
        # leases, claim present) would reach here and unload a borrowed/keep-warm
        # model. The claim is retained for the true owner / reconcile.
        if not (self.owned and not self.keep_warm):
            return 'released_unowned_claim'
        try:
            ok = await _maybe_await(unload(key))
        except asyncio.CancelledError:
            raise                           # retain the claim; a later close re-drives
        except Exception:                   # noqa: BLE001 - failed unload retains the claim
            return 'unload_failed'
        if ok is True and self.complete_unload(key, success=True) is True:
            return 'unloaded'
        return 'unload_failed'
