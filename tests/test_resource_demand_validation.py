from dataclasses import replace
import time
import pytest
from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand


@pytest.mark.parametrize('changes', [
    {'backend': ''}, {'endpoint': ''}, {'model': ''},
    {'concurrency': 0}, {'concurrency': True}, {'context': -1}, {'context': True},
    {'vram_peak_by_device': {'': 10}},
])
def test_malformed_identity_or_shape_never_dispatches(tmp_path, changes):
    coordinator = ResourceCoordinator(tmp_path / 'invalid.sqlite',
        telemetry=lambda: ResourceSnapshot(time.time(), 100, {'gpu': 100, '': 100}, True))
    demand = replace(ModelDemand('local', 'endpoint', 'model', 10, {'gpu': 10}), **changes)
    calls = []
    decision, _ = coordinator.load_guarded(demand, 'owner', lambda: calls.append(True))
    assert decision.status == 'blocked'
    assert not calls


@pytest.mark.parametrize('owner', ['', None, 42])
def test_missing_owner_never_dispatches(tmp_path, owner):
    coordinator = ResourceCoordinator(tmp_path / 'owner.sqlite',
        telemetry=lambda: ResourceSnapshot(time.time(), 100, {'gpu': 100}, True))
    calls = []
    decision, _ = coordinator.load_guarded(ModelDemand('local', 'endpoint', 'model', 10, {'gpu': 10}),
                                           owner, lambda: calls.append(True))
    assert decision.status == 'blocked'
    assert not calls
