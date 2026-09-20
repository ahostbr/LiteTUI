"""Instance model leases around backend load operations; no implicit eviction."""
from contextlib import asynccontextmanager
from litetui.llm_backend import VramRefused


class AdmissionBlocked(VramRefused):
    pass


class ModelResourceSession:
    def __init__(self, coordinator, owner, *, demand_for, owned=False, keep_warm=False):
        self.coordinator = coordinator
        self.owner = owner
        self.demand_for = demand_for
        self.owned = owned
        self.keep_warm = keep_warm
        self.leases = {}
        self.unload_claims = {}

    @asynccontextmanager
    async def load(self, key, *, reload=False):
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
        try:
            yield
        except BaseException:
            self.coordinator.release(decision.reservation_id, self.owner)
            raise
        if reload:
            self.coordinator.release(decision.reservation_id, self.owner)
            return
        try:
            lease = self.coordinator.acquire_lease(decision.reservation_id, self.owner,
                         owned=self.owned, keep_warm=self.keep_warm)
        except BaseException:
            # Successful load with failed bookkeeping must retain its capacity.
            # Reconciliation requires confirmed owner death AND absent residency.
            raise
        self.leases[key] = (lease, demand)

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
