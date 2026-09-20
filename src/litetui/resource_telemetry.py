"""Read-only host capacity telemetry. No load/unload or guessed zero counters."""
import ctypes
import os
import time
from litetui.resource_admission import ResourceSnapshot


def parse_gpu_inventory(text):
    devices = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        uuid, free = [part.strip() for part in line.split(',')]
        if not uuid or uuid in devices:
            raise ValueError('GPU UUID absent or duplicated')
        amount = int(free)
        if amount < 0:
            raise ValueError('Negative GPU memory counter')
        devices[uuid] = amount * 1024**2
    if not devices:
        raise ValueError('GPU inventory empty')
    return devices


def ram_available():
    if os.name != 'nt':
        raise OSError('RAM telemetry currently supported on Windows only')
    from ctypes import wintypes as wt
    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', wt.DWORD), ('load', wt.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in ('total_phys', 'avail_phys', 'total_page',
             'avail_page', 'total_virtual', 'avail_virtual', 'avail_extended')]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    kernel.GlobalMemoryStatusEx.restype = wt.BOOL
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return status.avail_phys


def host_snapshot():
    from litetui.gpu_gate import nvidia_smi
    from litetui import ttyguard
    started = time.time()
    errors = []
    ram, devices = None, {}
    try:
        ram = ram_available()
    except Exception as exc:
        errors.append(f'RAM: {exc}')
    try:
        executable = nvidia_smi()
        if not executable:
            raise OSError('nvidia-smi unavailable; GPU inventory unknown')
        result = ttyguard.run([executable, '--query-gpu=uuid,memory.free', '--format=csv,noheader,nounits'], timeout=10)
        if getattr(result, 'returncode', 0) != 0:
            raise OSError('nvidia-smi failed')
        devices = parse_gpu_inventory(result.stdout)
    except Exception as exc:
        errors.append(f'VRAM: {exc}')
    return ResourceSnapshot(started, ram, devices, not errors, errors=tuple(errors))
