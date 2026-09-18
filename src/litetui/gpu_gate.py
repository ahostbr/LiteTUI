"""Is this box an RTX 5090? The one question every NInfer surface asks first (T893).

RYAN, 2026-09-18 13:2x: *"make sure the user never sees anything about ninfer in both
litesuite and litetui if there not running a rtx 5090 gpu ... we must use nvidia-smi,
detect if it exists on the system clean exit if not stating no way its a 5090 without
nvidia-smi on the machine ... then and only then IF gpu=5090 everything is shown about
installing and managing ninfer ... as well as the slash cmds"*.

nvidia-smi ships with every NVIDIA driver (System32), and NInfer needs Blackwell NVFP4,
so a machine without nvidia-smi has no NVIDIA driver and therefore no 5090: that is a
clean "no", not an error. The name decides, not compute capability — an RTX 5080 reports
the same `12.0` (LiteSuite's ninferEligibility.ts, ruling 3: "exactly the 5090").

Asked once per process (`is_rtx_5090` is cached): the card does not change while the app
runs. Tests clear the cache and monkeypatch `nvidia_smi` / `ttyguard.run`.
"""
from __future__ import annotations

import functools
import os
import shutil

from . import ttyguard

_SMI_FALLBACK = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")


def nvidia_smi() -> str | None:
    """Path of nvidia-smi, or None when this machine has no NVIDIA driver."""
    found = shutil.which("nvidia-smi")
    if found:
        return found
    return _SMI_FALLBACK if os.path.isfile(_SMI_FALLBACK) else None


def gpu_names() -> list[str]:
    """Every GPU name nvidia-smi reports; [] without nvidia-smi or on any failure."""
    smi = nvidia_smi()
    if smi is None:
        return []
    try:
        out = ttyguard.run([smi, "--query-gpu=name", "--format=csv,noheader"], timeout=10)
        text = str(getattr(out, "stdout", out) or "")
    except Exception:  # noqa: BLE001 - cannot ask -> not a 5090, never a crash
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


@functools.lru_cache(maxsize=1)
def is_rtx_5090() -> bool:
    return any("5090" in name for name in gpu_names())


def why_not() -> str:
    """The sentence a user reads when they ask for NInfer by name on the wrong box."""
    if nvidia_smi() is None:
        return ("NInfer needs an RTX 5090 — there is no nvidia-smi on this machine, "
                "so no NVIDIA driver, so no 5090.")
    names = ", ".join(gpu_names()) or "no GPU"
    return f"NInfer needs an RTX 5090; nvidia-smi reports {names}."
