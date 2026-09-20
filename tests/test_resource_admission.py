"""Fail-closed resource admission before any load callback."""
import time


def test_unknown_telemetry_and_demand_never_load(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    loaded = []
    coordinator = ResourceCoordinator(tmp_path / 'resources.sqlite',
        telemetry=lambda: ResourceSnapshot(time.time(), None, {}, False))
    result = coordinator.reserve(ModelDemand('codex', 'endpoint', 'model', 10, {}), 'owner')
    assert result.status == 'blocked'
    assert loaded == []


def test_reservations_cannot_spend_same_capacity(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(tmp_path / 'resources.sqlite',
        telemetry=lambda: ResourceSnapshot(time.time(), 100, {'gpu0': 100}, True))
    demand = ModelDemand('local', 'endpoint', 'model', 60, {'gpu0': 60})
    first = coordinator.reserve(demand, 'a')
    second = coordinator.reserve(demand, 'b')
    assert first.status == 'admitted'
    assert second.status == 'blocked'
    assert second.snapshot.ram_available == 100


def test_blocked_gate_does_not_call_loader_and_failure_retains_capacity(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    calls = []
    coordinator = ResourceCoordinator(tmp_path / 'gate.sqlite',
        telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
    excessive = ModelDemand('cpu', 'endpoint', 'model', 101, {})
    decision, result = coordinator.load_guarded(excessive, 'owner', lambda: calls.append(True))
    assert decision.status == 'blocked'
    assert calls == []
    small = ModelDemand('cpu', 'endpoint', 'model', 80, {})
    def fail(): raise RuntimeError('allocation failed')
    import pytest
    with pytest.raises(RuntimeError, match='allocation failed'):
        coordinator.load_guarded(small, 'owner', fail)
    assert coordinator.reserve(small, 'next').status == 'blocked'
    with coordinator.store.transaction() as db:
        reservation = db.execute("SELECT id FROM reservations WHERE owner='owner' AND state='reserved'").fetchone()[0]
    assert coordinator.settle_absent_load(reservation, 'owner')
    assert coordinator.reserve(small, 'next').status == 'admitted'


def test_invalid_ram_and_future_or_nonfinite_telemetry_fail_closed(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    import math
    for index, ram in enumerate([float('nan'), float('inf'), True, -1]):
        coordinator = ResourceCoordinator(tmp_path / f'invalid-{index}.sqlite',
            telemetry=lambda: ResourceSnapshot(time.time(), 100, {}, True))
        assert coordinator.reserve(ModelDemand('cpu', 'endpoint', 'model', ram, {}), 'a').status == 'blocked'


def test_invalid_or_stale_snapshots_block_real_callback(tmp_path):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    snapshots = [ResourceSnapshot(float('nan'), 100, {}, True),
                 ResourceSnapshot(time.time() - 60, 100, {}, True),
                 ResourceSnapshot(time.time() + 60, 100, {}, True),
                 ResourceSnapshot(time.time(), float('inf'), {}, True),
                 ResourceSnapshot(time.time(), 100, {'gpu': None}, True)]
    for index, snapshot in enumerate(snapshots):
        calls = []
        coordinator = ResourceCoordinator(tmp_path / f'snapshot-{index}.sqlite', telemetry=lambda: snapshot)
        decision, _ = coordinator.load_guarded(ModelDemand('cpu', 'endpoint', 'model', 10, {}), 'a', lambda: calls.append(True))
        assert decision.status == 'blocked'
        assert not calls
