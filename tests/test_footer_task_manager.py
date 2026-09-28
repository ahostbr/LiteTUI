"""Footer telemetry: measured-only meters, switch ownership and idle discipline."""
from __future__ import annotations

import pytest

from litetui import app as app_mod
from litetui import footer_telemetry as telemetry
from litetui.widgets import TaskManagerToggle


def make_app():
    a = app_mod.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.available_models = ["test"]
    a.model_id = "test"
    a.convo_id = "test"
    return a


def test_missing_sensors_are_absent_and_meter_fits_whole_cells():
    reading = telemetry.Reading(cpu=45, ram=60)
    assert [name.split()[0] for name, _ in telemetry.meter(reading, 80)] == ["CPU", "RAM"]
    for width in range(35):
        chunks = telemetry.meter(reading, width)
        assert sum(len(text) for text, _ in chunks) + max(0, len(chunks) - 1) * 3 <= width


def test_sparklines_follow_changing_cpu_gpu_network_series():
    readings = [telemetry.Reading(cpu=0, gpu=0, network=0),
                telemetry.Reading(cpu=25, gpu=50, network=1024),
                telemetry.Reading(cpu=75, gpu=100, network=4096)]
    labels = {text.split()[0]: text for text, _ in telemetry.meter(readings[-1], 180, readings)}
    assert labels["CPU"].endswith("▁▁▁▁▂▆")
    assert labels["GPU"].endswith("▁▁▁▁▄█")
    assert labels["NET"].endswith("▁▁▁▁▂█")
    assert len(telemetry.sparkline(readings, "cpu", percent=True)) == 6


def test_sampler_uses_real_counters_and_rates(monkeypatch):
    from types import SimpleNamespace
    clock = iter([10.0, 12.0])
    disk = iter([SimpleNamespace(read_bytes=100, write_bytes=200),
                 SimpleNamespace(read_bytes=1100, write_bytes=1200)])
    network = iter([SimpleNamespace(bytes_recv=100, bytes_sent=100),
                    SimpleNamespace(bytes_recv=300, bytes_sent=500)])
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(telemetry.psutil, "cpu_percent", lambda interval: 25.0)
    monkeypatch.setattr(telemetry.psutil, "virtual_memory", lambda: SimpleNamespace(percent=50.0))
    monkeypatch.setattr(telemetry.psutil, "disk_io_counters", lambda: next(disk))
    monkeypatch.setattr(telemetry.psutil, "net_io_counters", lambda: next(network))
    monkeypatch.setattr(telemetry, "_gpu", lambda: (None, None))
    sampler = telemetry.Sampler()
    first = sampler.sample()
    second = sampler.sample()
    assert first.disk is None and first.network is None
    assert second.disk == 1000 and second.network == 300
    assert second.gpu is None and second.vram is None


@pytest.mark.asyncio
async def test_switch_owns_its_cells_and_stops_the_timer(monkeypatch):
    monkeypatch.setattr(telemetry.Sampler, "sample", lambda self: telemetry.Reading(cpu=40, ram=50))
    app = make_app()
    saved = []
    # No disk writes in this test; test the control's live transition.
    def apply(new):
        saved.append(new.footer_task_manager)
        old = app.settings
        app.settings = new
        if old.footer_task_manager != new.footer_task_manager:
            app._sync_footer_sampler()
            for control in app.query(".task-manager-toggle"):
                control.sync()
    monkeypatch.setattr(app, "_on_settings_saved", apply)
    async with app.run_test(size=(160, 34)) as pilot:
        await pilot.pause(0.3)
        toggle = app.query_one(TaskManagerToggle)
        assert toggle.content == "meters:on"
        assert app._footer_sample_timer is not None
        # The clickable widget's own region (not a parent Static's on_click).
        assert toggle.region.width >= len("meters:on")
        await pilot.click(TaskManagerToggle)
        assert saved == [False]
        assert toggle.content == "meters:off"
        assert app._footer_sample_timer is None
        assert app._footer_telemetry is None
        await pilot.click(TaskManagerToggle)
        assert saved == [False, True]
        assert app._footer_sample_timer is not None
