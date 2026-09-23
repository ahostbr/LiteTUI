"""Optional preview launch must never silently replace the Textual UI."""
from pathlib import Path
from unittest.mock import Mock

from litetui import sidecar_launch


def test_disabled_never_spawns():
    spawn = Mock()
    assert not sidecar_launch.try_open("settings", enabled=False, executable=Path("missing.exe"), spawn=spawn)
    spawn.assert_not_called()


def test_missing_binary_falls_back_with_reason(tmp_path):
    warn = Mock()
    assert not sidecar_launch.try_open("calendar", enabled=True, executable=tmp_path / "missing.exe", warn=warn)
    assert "not installed" in warn.call_args.args[0]


def test_existing_binary_opens_once_and_unwired_view_switch_falls_back(tmp_path):
    exe = tmp_path / "sidecar.exe"
    exe.write_bytes(b"sample")
    process = Mock()
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    warn = Mock()
    owner = sidecar_launch.SidecarWindow(executable=exe, spawn=spawn, warn=warn)
    assert owner.open("job")
    assert not owner.open("calendar")
    assert "not connected" in warn.call_args.args[0]
    spawn.assert_called_once_with([str(exe), "--view", "job"], close_fds=True)
    assert owner.close()
    process.terminate.assert_called_once()


def test_launch_error_warns_and_falls_back(tmp_path):
    exe = tmp_path / "sidecar.exe"
    exe.write_bytes(b"sample")
    warn = Mock()
    spawn = Mock(side_effect=OSError("denied"))
    assert not sidecar_launch.try_open("settings", enabled=True, executable=exe, warn=warn, spawn=spawn)
    assert "denied" in warn.call_args.args[0]
