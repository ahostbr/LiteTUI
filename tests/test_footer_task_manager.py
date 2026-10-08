"""Footer telemetry: measured-only meters, switch ownership and idle discipline."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import footer_telemetry as telemetry
from litetui.widgets import TaskManagerToggle


def make_app(meters_on=True):
    # The meters default OFF (T1115c); these arms test them switched on unless told not to.
    a = app_mod.LiteTUI()
    if meters_on is not None:
        a.settings.footer_task_manager = meters_on
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


@pytest.mark.asyncio
async def test_a_missing_sensor_library_hides_meters_instead_of_failing_mount(monkeypatch):
    # A stale env without psutil: the seat must still mount, with no sampler timer.
    import sys
    monkeypatch.setitem(sys.modules, "litetui.footer_telemetry", None)
    app = make_app()
    async with app.run_test(size=(160, 34)) as pilot:
        await pilot.pause(0.2)
        assert app._footer_sample_timer is None
        assert app._footer_telemetry is None


@pytest.mark.parametrize("width", [160, 120, 80])
@pytest.mark.asyncio
async def test_live_shaped_status_keeps_meters_on_second_row(width):
    app = make_app()
    app.seat = SimpleNamespace(name="Carmack", registered=True)
    app.convo_id = "a766a2d6-4c6e-42e7-ae12-f2075825041a"
    app.model_id = "gpt-6-sol"
    app.thinking_level = "high"
    app.ctx_used, app.ctx_max, app.ctx_loaded = 22824, 258400, True
    app.tps = 12.0
    app.settings.footer_show_cache = True
    app.backend = SimpleNamespace(name="codex", shutdown=lambda: None)
    app.last_usage = {"latest_request_usage": {"inputTokens": 1000,
                                               "cachedInputTokens": 990}}
    # No real sampling: each measure and its history are deterministic.
    app._sync_footer_sampler = lambda: None
    history = [telemetry.Reading(cpu=10, gpu=20, network=1024, ram=50,
                                 vram=30, disk=1024),
               telemetry.Reading(cpu=65, gpu=75, network=8192, ram=60,
                                 vram=40, disk=2048)]
    app._footer_telemetry = history[-1]
    app._footer_history = history
    async with app.run_test(size=(width, 34)) as pilot:
        # The footer's layout may lag mount under a loaded CI loop. Bound the
        # wait; keep the geometry assertions independent of scheduler timing.
        for _ in range(40):
            await pilot.pause(0.05)
            status_nodes = list(app.query(".ctx-label"))
            meter_nodes = list(app.query(".footer-meters"))
            if (status_nodes and meter_nodes and status_nodes[0].region.height
                    and meter_nodes[0].region.y):
                break
        assert status_nodes and meter_nodes, "footer did not mount within 2 seconds"
        status = app.ctx_label_text.plain
        permission = app.permission_label_text.plain
        meters = app.footer_meters_text.plain
        print(f"\n{width}: line1 {status}\n{width}: line2 {permission}  meters:on{meters}")
        assert "CPU" not in status and "GPU" not in status
        if width == 160:
            assert "cache warm 99%" in status
        assert all(value in status for value in ("9%", "Carmack", "think:high"))
        if width == 120:
            assert "12.0 tok/s" in status and "cache warm 99%" in status
            assert all(value in meters for value in ("CPU", "GPU", "NET"))
            assert "▁" in meters and "█" in meters
        assert "CPU" in meters
        assert "DISK" not in meters  # Lowest priority goes first, not a clipped fragment.
        assert app.query_one(".ctx-label").region.y + 1 == app.query_one(".footer-meters").region.y
        row = app.query_one(".footer-second-row")
        toggle = app.query_one(".task-manager-toggle")
        meter_widget = app.query_one(".footer-meters")
        assert (row.query_one(".permission-label").region.width + toggle.region.width
                + meter_widget.region.width <= width)
        assert meter_widget.region.right <= width
        assert app.query_one(".task-manager-toggle").region.right <= app.query_one(".footer-meters").region.x
        assert (app.query_one(".footer-meters").region.width == app.footer_meters_text.cell_len)


@pytest.mark.asyncio
async def test_meters_are_off_by_default_and_run_no_sampler(monkeypatch):
    # Owner 2026-09-27: "turn this off in light UI though. Uh, make a setting to toggle that".
    from litetui.settings import Settings
    assert Settings().footer_task_manager is False
    monkeypatch.setattr(telemetry.Sampler, "sample", lambda self: telemetry.Reading(cpu=40, ram=50))
    app = make_app(meters_on=None)
    async with app.run_test(size=(160, 34)) as pilot:
        await pilot.pause(0.3)
        assert app.settings.footer_task_manager is False
        assert app._footer_sample_timer is None and app._footer_telemetry is None
        assert app.query_one(TaskManagerToggle).content == "meters:off"
        assert "CPU" not in app.footer_meters_text.plain
