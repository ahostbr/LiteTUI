"""Optional parent-owned preview never silently replaces Textual on failure."""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

from litetui import sidecar_launch


class FakeProcess:
    def __init__(self, *, response=None, responsive=True):
        self.stdin = Mock()
        self.stdout = Mock()
        self.stdout.readline.return_value = (json.dumps(response).encode() + b"\n") if response else b""
        self.returncode = None
        self.responsive = responsive
        self.terminated = False
        self.killed = False
        self.calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.calls += 1
        if not self.responsive and not self.killed:
            raise subprocess.TimeoutExpired("sidecar", timeout)
        self.returncode = 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_disabled_never_spawns():
    spawn = Mock()
    owner = sidecar_launch.SidecarWindow(Path("missing.exe"), spawn=spawn)
    assert not owner.open("settings", enabled=False)
    spawn.assert_not_called()


def test_missing_binary_falls_back_with_reason(tmp_path):
    warn = Mock()
    owner = sidecar_launch.SidecarWindow(tmp_path / "missing.exe", warn=warn)
    assert not owner.open("calendar")
    assert "not installed" in warn.call_args.args[0]


def test_handshake_and_live_view_switch(tmp_path):
    exe = tmp_path / "sidecar.exe"
    exe.write_bytes(b"sample")
    process = FakeProcess()
    spawn = Mock(return_value=process)
    owner = sidecar_launch.SidecarWindow(exe, spawn=spawn)
    # Frame token comes from launch argv, not hardcoded fixture.
    process.stdout.readline.side_effect = lambda _limit: (json.dumps({
        "version": 1, "id": 1, "token": owner.token,
        "command": "reply", "payload": {"ready": True, "version": 1},
    }).encode() + b"\n") if process.stdin.write.call_count == 1 else (json.dumps({
        "version": 1, "id": 2, "token": owner.token,
        "command": "reply", "payload": {"opened": "calendar"},
    }).encode() + b"\n")
    assert owner.open("job")
    assert owner.open("calendar")
    spawn.assert_called_once()
    args, kwargs = spawn.call_args
    assert args[0][:3] == [str(exe), "--view", "job"]
    assert args[0][3] == "--parent-pipe"
    assert kwargs["stdin"] == subprocess.PIPE and kwargs["stdout"] == subprocess.PIPE
    assert owner.close()
    assert process.terminated


def test_wrong_token_and_hung_child_fall_back_and_force_close(tmp_path):
    exe = tmp_path / "sidecar.exe"
    exe.write_bytes(b"sample")
    process = FakeProcess(response={"version": 1, "id": 1, "token": "forged", "command": "reply", "payload": {"ready": True}}, responsive=False)
    warn = Mock()
    owner = sidecar_launch.SidecarWindow(exe, spawn=Mock(return_value=process), warn=warn)
    assert not owner.open("settings")
    assert process.terminated and process.killed
    assert "handshake" in warn.call_args.args[0]


def test_launch_error_warns_and_falls_back(tmp_path):
    exe = tmp_path / "sidecar.exe"
    exe.write_bytes(b"sample")
    warn = Mock()
    owner = sidecar_launch.SidecarWindow(exe, warn=warn, spawn=Mock(side_effect=OSError("denied")))
    assert not owner.open("settings")
    assert "denied" in warn.call_args.args[0]


def test_oversized_unterminated_reply_falls_back_without_unbounded_read(tmp_path):
    exe = tmp_path / "sidecar.exe"
    exe.write_bytes(b"sample")
    process = FakeProcess()
    process.stdout.readline.return_value = b"x" * (sidecar_launch.sidecar_protocol.MAX_FRAME_BYTES + 2)
    warn = Mock()
    owner = sidecar_launch.SidecarWindow(exe, spawn=Mock(return_value=process), warn=warn)
    assert not owner.open("settings")
    process.stdout.readline.assert_called_once_with(sidecar_launch.sidecar_protocol.MAX_FRAME_BYTES + 2)
    assert process.terminated
    assert process.stdin.close.called and process.stdout.close.called
    assert "handshake" in warn.call_args.args[0]


def test_close_reaps_child_and_closes_both_pipes(tmp_path):
    process = FakeProcess()
    owner = sidecar_launch.SidecarWindow(tmp_path / "sidecar.exe")
    owner.process = process
    assert owner.close()
    assert process.terminated
    assert process.stdin.close.called and process.stdout.close.called
    assert owner.process is None
