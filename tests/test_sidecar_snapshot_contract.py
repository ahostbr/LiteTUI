"""Contract: LiteTUI's real public_snapshot() is a payload the SHIPPED sidecar accepts.

Drives the binary LiteTUI would launch (the same lookup: LITETUI_SIDECAR_EXE, else
<data root>/bin) with `--check-settings-snapshot`, which applies the settings view's
own acceptance rule and exits 0 or 1 without opening a window. Skipped when no
sidecar is installed. It pins drift between the two repos; it did NOT reproduce
T1028's blank window (that was the page never asking for settings).
"""
import json
import subprocess

import pytest

from litetui.plugins import sidecar_plugin
from litetui.settings_service import SettingsService
from litetui.sidecar_settings import public_snapshot


def test_shipped_sidecar_accepts_the_real_settings_snapshot(tmp_path):
    exe = sidecar_plugin._new_window(None).executable
    if not exe.is_file():
        pytest.skip(f"no sidecar installed at {exe}")
    snapshot = public_snapshot(SettingsService(tmp_path).snapshot("c1"))
    result = subprocess.run([str(exe), "--check-settings-snapshot"], input=json.dumps(snapshot).encode(),
                            capture_output=True, timeout=20)
    assert result.returncode != 2, f"{exe} predates --check-settings-snapshot; rebuild and install the sidecar"
    assert result.returncode == 0, f"{exe} rejects LiteTUI's settings snapshot"
