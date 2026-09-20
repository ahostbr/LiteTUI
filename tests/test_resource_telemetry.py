def test_smi_inventory_uses_physical_uuid_and_bytes():
    from litetui.resource_telemetry import parse_gpu_inventory
    assert parse_gpu_inventory('GPU-abc, 1024\nGPU-def, 2048\n') == {'GPU-abc': 1024 * 1024**2, 'GPU-def': 2048 * 1024**2}


def test_unavailable_gpu_counter_is_not_zero_capacity():
    import pytest
    from litetui.resource_telemetry import parse_gpu_inventory
    with pytest.raises(ValueError):
        parse_gpu_inventory('GPU-abc, N/A')


def test_snapshot_timestamp_precedes_slow_inventory(monkeypatch):
    from types import SimpleNamespace
    from litetui import resource_telemetry as telemetry, gpu_gate, ttyguard
    clock = [100.0]
    monkeypatch.setattr(telemetry.time, 'time', lambda: clock[0])
    monkeypatch.setattr(telemetry, 'ram_available', lambda: 100)
    monkeypatch.setattr(gpu_gate, 'nvidia_smi', lambda: 'fake')
    def slow(*args, **kwargs):
        clock[0] = 120.0
        return SimpleNamespace(returncode=0, stdout='GPU-test, 100')
    monkeypatch.setattr(ttyguard, 'run', slow)
    assert telemetry.host_snapshot().timestamp == 100.0
