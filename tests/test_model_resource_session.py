import time
import pytest


@pytest.mark.asyncio
async def test_unknown_demand_blocks_body(tmp_path):
    from litetui.model_resource_session import ModelResourceSession, AdmissionBlocked
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot
    coordinator = ResourceCoordinator(tmp_path / 'session.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    session = ModelResourceSession(coordinator, 'owner', demand_for=lambda key: None)
    ran = []
    with pytest.raises(AdmissionBlocked):
        async with session.load('model'): ran.append(True)
    assert not ran


@pytest.mark.asyncio
async def test_success_keeps_lease_until_explicit_release(tmp_path):
    from litetui.model_resource_session import ModelResourceSession
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'session.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 80, {})
    session = ModelResourceSession(coordinator, 'owner', demand_for=lambda key: demand)
    async with session.load('model'): pass
    assert coordinator.reserve(demand, 'other').status == 'blocked'
    assert session.release('model') is False  # borrowed by default, never unload
    assert coordinator.reserve(demand, 'other').status == 'admitted'


@pytest.mark.asyncio
async def test_second_load_does_not_bypass_peak_reservation(tmp_path):
    from litetui.model_resource_session import ModelResourceSession, AdmissionBlocked
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'reload.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 80, {})
    session = ModelResourceSession(coordinator, 'owner', demand_for=lambda key: demand)
    async with session.load('model'): pass
    called = []
    with pytest.raises(AdmissionBlocked):
        async with session.load('model'): called.append(True)
    assert not called


@pytest.mark.asyncio
async def test_owned_release_retains_acknowledgement_claim(tmp_path):
    from litetui.model_resource_session import ModelResourceSession
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'owned.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 10, {})
    session = ModelResourceSession(coordinator, 'owner', demand_for=lambda key: demand, owned=True)
    async with session.load('model'): pass
    assert session.release('model')
    assert session.complete_unload('model', success=True)
    async with session.load('model'): pass

@pytest.mark.asyncio
async def test_explicit_reload_reserves_peak_and_retains_original_lease(tmp_path):
    from litetui.model_resource_session import ModelResourceSession
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'reload-ok.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 40, {})
    session = ModelResourceSession(coordinator, 'owner', demand_for=lambda key: demand)
    async with session.load('model'): pass
    original = session.leases['model']
    async with session.load('model', reload=True):
        assert coordinator.reserve(demand, 'other').status == 'blocked'
    assert session.leases['model'] == original
    assert coordinator.reserve(demand, 'other').status == 'admitted'

@pytest.mark.asyncio
async def test_explicit_reload_refuses_shared_model(tmp_path):
    from litetui.model_resource_session import ModelResourceSession, AdmissionBlocked
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'shared.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 1000, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 40, {})
    a = ModelResourceSession(coordinator, 'a', demand_for=lambda key: demand)
    b = ModelResourceSession(coordinator, 'b', demand_for=lambda key: demand)
    async with a.load('model'): pass
    async with b.load('model'): pass
    with pytest.raises(AdmissionBlocked):
        async with a.load('model', reload=True): pytest.fail('shared model mutated')

@pytest.mark.asyncio
async def test_reload_cancel_retains_peak_until_settled(tmp_path):
    import asyncio
    from litetui.model_resource_session import ModelResourceSession
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'cancel.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 40, {})
    session = ModelResourceSession(coordinator, 'owner', demand_for=lambda key: demand)
    async with session.load('model'): pass
    original = session.leases['model']
    with pytest.raises(asyncio.CancelledError):
        async with session.load('model', reload=True):
            with pytest.raises(ValueError, match='reload is pending'):
                session.release('model')
            assert session.leases['model'] == original
            raise asyncio.CancelledError()
    assert session.leases['model'] == original
    assert session.settle_failed_load('model', absent=True)
    async with session.load('model', reload=True): pass

@pytest.mark.asyncio
async def test_failed_unload_acknowledgement_can_be_released_again(tmp_path):
    from litetui.model_resource_session import ModelResourceSession
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'retry.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 10, {})
    session = ModelResourceSession(coordinator, 'owner', demand_for=lambda key: demand, owned=True)
    async with session.load('model'): pass
    assert session.release('model')
    assert session.complete_unload('model', success=False)
    assert not session.complete_unload('model', success=True)
    async with session.load('model'): pass
    assert session.release('model')
    assert session.complete_unload('model', success=True)

@pytest.mark.asyncio
async def test_cancelled_loader_retains_capacity_until_settlement(tmp_path):
    import asyncio
    from litetui.model_resource_session import ModelResourceSession
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'unsettled.sqlite', telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    demand = ModelDemand('cpu', 'endpoint', 'model', 80, {})
    session = ModelResourceSession(coordinator, 'owner', demand_for=lambda key: demand)
    with pytest.raises(asyncio.CancelledError):
        async with session.load('model'):
            raise asyncio.CancelledError()
    assert coordinator.reserve(demand, 'other').status == 'blocked'
    assert not session.settle_failed_load('model', absent=None)
    assert coordinator.reserve(demand, 'other').status == 'blocked'
    assert session.settle_failed_load('model', absent=True)
    assert coordinator.reserve(demand, 'other').status == 'admitted'
