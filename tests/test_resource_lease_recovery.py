import time


def test_borrowed_model_never_unloaded_and_owner_checked(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'leases.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    reservation = coordinator.reserve(ModelDemand('cpu', 'endpoint', 'model', 10, {}), 'a')
    lease = coordinator.acquire_lease(reservation.reservation_id, 'a', owned=False)
    assert coordinator.release_lease(lease, 'other') is False
    assert coordinator.release_lease(lease, 'a') is False  # no owned unload authorization


def test_only_last_owned_lease_authorizes_unload(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'leases.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    reservation = coordinator.reserve(ModelDemand('cpu', 'endpoint', 'model', 10, {}), 'a')
    lease = coordinator.acquire_lease(reservation.reservation_id, 'a', owned=True)
    assert coordinator.release_lease(lease, 'a') is True
    assert coordinator.release_lease(lease, 'a') is False


def test_shared_owned_model_waits_for_last_user_and_blocks_unload_race(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    import pytest
    coordinator = ResourceCoordinator(tmp_path / 'shared.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 10, {})
    a = coordinator.reserve(demand, 'a')
    la = coordinator.acquire_lease(a.reservation_id, 'a', owned=True)
    # Borrow only after the first load has reached a confirmed lease.
    b = coordinator.reserve(demand, 'b')
    lb = coordinator.acquire_lease(b.reservation_id, 'b', owned=False)
    assert not coordinator.release_lease(la, 'a')
    assert coordinator.release_lease(lb, 'b')
    c = coordinator.reserve(demand, 'c')
    assert c.status == 'blocked'
    with pytest.raises(ValueError, match='Reservation'):
        coordinator.acquire_lease(c.reservation_id, 'c')


def test_keep_warm_never_authorizes_automatic_unload(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'warm.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    reservation = coordinator.reserve(ModelDemand('cpu', 'endpoint', 'model', 10, {}), 'a')
    lease = coordinator.acquire_lease(reservation.reservation_id, 'a', owned=True, keep_warm=True)
    assert not coordinator.release_lease(lease, 'a')


def test_unload_acknowledgement_requires_claim_owner_and_allows_reload(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'ack.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 10, {})
    reservation = coordinator.reserve(demand, 'a')
    lease = coordinator.acquire_lease(reservation.reservation_id, 'a', owned=True)
    assert coordinator.release_lease(lease, 'a')
    assert not coordinator.complete_unload(lease, 'other', success=True)
    assert coordinator.complete_unload(lease, 'a', success=True)
    next_reservation = coordinator.reserve(demand, 'b')
    assert coordinator.acquire_lease(next_reservation.reservation_id, 'b', owned=True)


def test_plain_reservation_release_cannot_bypass_active_lease(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'bypass.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 80, {})
    reservation = coordinator.reserve(demand, 'a')
    coordinator.acquire_lease(reservation.reservation_id, 'a', owned=True)
    assert not coordinator.release(reservation.reservation_id, 'a')
    assert coordinator.reserve(demand, 'b').status == 'blocked'


def test_incompatible_shape_is_blocked_while_model_is_leased(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'shape.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    first = coordinator.reserve(ModelDemand('cpu', 'endpoint', 'model', 10, {}, context=4096), 'a')
    coordinator.acquire_lease(first.reservation_id, 'a', owned=True)
    second = coordinator.reserve(ModelDemand('cpu', 'endpoint', 'model', 10, {}, context=8192), 'b')
    assert second.status == 'blocked'
    assert 'shape' in second.reason


def test_reconcile_never_reclaims_unknown_or_live_owner(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'recovery.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 80, {})
    reservation = coordinator.reserve(demand, 'a')
    assert coordinator.reconcile(owner_alive=lambda owner: None) == []
    assert coordinator.reconcile(owner_alive=lambda owner: True) == []
    assert coordinator.reserve(demand, 'b').status == 'blocked'
    assert coordinator.reconcile(owner_alive=lambda owner: False, model_resident=lambda demand: False, model_quiescent=lambda demand: True) == [reservation.reservation_id]
    assert coordinator.reserve(demand, 'b').status == 'admitted'


def test_crashed_loader_reservation_needs_residency_proof(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'crashed-loader.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    reservation = coordinator.reserve(ModelDemand('cpu', 'endpoint', 'model', 80, {}), 'a')
    assert coordinator.reconcile(owner_alive=lambda owner: False, model_resident=lambda demand: True) == []


def test_pending_unload_blocks_loader_before_lease_acquisition(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'unloading.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 10, {})
    first = coordinator.reserve(demand, 'a')
    lease = coordinator.acquire_lease(first.reservation_id, 'a', owned=True)
    assert coordinator.release_lease(lease, 'a')
    calls = []
    decision, _ = coordinator.load_guarded(demand, 'b', lambda: calls.append(True))
    assert decision.status == 'blocked'
    assert calls == []


def test_pending_reservation_prevents_last_user_unload(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'reserved-user.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 10, {})
    first = coordinator.reserve(demand, 'a')
    lease = coordinator.acquire_lease(first.reservation_id, 'a', owned=True)
    second = coordinator.reserve(demand, 'b')
    assert second.status == 'admitted'
    assert not coordinator.release_lease(lease, 'a')
    assert coordinator.acquire_lease(second.reservation_id, 'b')


def test_dead_loader_empty_catalogue_does_not_prove_quiescence(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'still-loading.sqlite',
        telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 10, {})
    reservation = coordinator.reserve(demand, 'dead-client')
    assert coordinator.reconcile(owner_alive=lambda owner: False,
                                 model_resident=lambda demand: False) == []
    assert coordinator.reserve(demand, 'next').status == 'blocked'
    assert coordinator.reconcile(owner_alive=lambda owner: False,
                                 model_resident=lambda demand: False,
                                 model_quiescent=lambda demand: True) == [reservation.reservation_id]
