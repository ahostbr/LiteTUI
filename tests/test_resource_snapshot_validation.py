from dataclasses import replace
import time
import pytest
from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand


@pytest.mark.parametrize('changes', [
    {'reliable': 'yes'}, {'reliable': 1}, {'timestamp': None}, {'timestamp': 'today'},
    {'vram_available': None}, {'vram_available': []}, {'vram_available': {'': 100}},
])
def test_invalid_snapshot_returns_blocked_without_dispatch(tmp_path, changes):
    snapshot = replace(ResourceSnapshot(time.time(), 100, {'gpu': 100}, True), **changes)
    coordinator = ResourceCoordinator(tmp_path / 'resources.sqlite', telemetry=lambda: snapshot)
    calls = []
    result, _ = coordinator.load_guarded(ModelDemand('local', 'endpoint', 'model', 10, {}),
                                          'owner', lambda: calls.append(True))
    assert result.status == 'blocked'
    assert calls == []
    with coordinator.store.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0] == 0
