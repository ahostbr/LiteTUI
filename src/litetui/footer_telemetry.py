"""Measured machine counters for the optional footer task manager.

All calls to sample() run in a worker thread. Missing sensors are absent, not zero.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class Reading:
    cpu: float | None = None
    ram: float | None = None
    gpu: float | None = None
    vram: float | None = None
    disk: float | None = None  # bytes/s
    network: float | None = None  # bytes/s


def _gpu() -> tuple[float | None, float | None]:
    from litetui.gpu_gate import nvidia_smi

    executable = nvidia_smi()
    if not executable:
        return None, None
    try:
        result = subprocess.run(
            [executable, "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"], capture_output=True, text=True,
            timeout=2, check=True,
        )
        first = result.stdout.splitlines()[0].split(",")
        gpu, used, total = (float(value.strip()) for value in first[:3])
        return gpu, 100 * used / total if total > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, ZeroDivisionError):
        return None, None


class Sampler:
    def __init__(self) -> None:
        self._previous: tuple[float, int | None, int | None] | None = None

    def sample(self) -> Reading:
        now = time.monotonic()
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_io_counters()
        net = psutil.net_io_counters()
        disk_total = disk.read_bytes + disk.write_bytes if disk is not None else None
        net_total = net.bytes_recv + net.bytes_sent if net is not None else None
        gpu, vram = _gpu()
        disk_rate = net_rate = None
        if self._previous is not None:
            before, old_disk, old_net = self._previous
            elapsed = now - before
            if elapsed > 0:
                if disk_total is not None and old_disk is not None:
                    disk_rate = max(0, disk_total - old_disk) / elapsed
                if net_total is not None and old_net is not None:
                    net_rate = max(0, net_total - old_net) / elapsed
        self._previous = now, disk_total, net_total
        return Reading(cpu, ram, gpu, vram, disk_rate, net_rate)


def sparkline(history: list[Reading], name: str, *, percent: bool) -> str:
    """Six fixed cells; percentages share a 0-100 scale, rates use their peak."""
    values = [getattr(sample, name) for sample in history[-6:]]
    values = [value for value in values if value is not None and value >= 0]
    if not values:
        return ""
    values = [values[0]] * (6 - len(values)) + values
    ceiling = 100 if percent else max(values) or 1
    levels = "▁▂▃▄▅▆▇█"
    return "".join(levels[min(7, int(value * 7 / ceiling))] for value in values)


def meter(reading: Reading, budget: int,
          history: list[Reading] | None = None) -> list[tuple[str, str]]:
    """Return complete Rich chunks in priority order; never split a meter."""
    values = (
        ("CPU", "cpu", reading.cpu, "#8bd878", True),
        ("GPU", "gpu", reading.gpu, "#78aaff", True),
        ("NET", "network", reading.network, "#78aaff", False),
        ("RAM", "ram", reading.ram, "#e3c66d", True),
        ("VRAM", "vram", reading.vram, "#78aaff", True),
        ("DISK", "disk", reading.disk, "#8bd878", False),
    )
    result: list[tuple[str, str]] = []
    for name, field, value, color, percent in values:
        if value is None:
            continue
        if percent:
            if not 0 <= value <= 100:
                continue  # Invalid sensor data is not a plausible percentage.
            slots = max(0, min(4, round(value / 25)))
            text = f"{name} {'▰' * slots}{'▱' * (4 - slots)} {value:.0f}%"
        else:
            rate = f"{value / 1048576:.1f}M/s" if value >= 1048576 else f"{value / 1024:.0f}K/s"
            text = f"{name} {rate}"
        if history:
            text += " " + sparkline(history, field, percent=percent)
        cost = len(text) + (3 if result else 0)
        if cost <= budget:
            result.append((text, color))
            budget -= cost
    return result
