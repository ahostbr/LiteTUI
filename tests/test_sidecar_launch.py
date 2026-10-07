"""Optional parent-owned preview never silently replaces Textual on failure."""
import json
import subprocess
import queue
import threading
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
    def reply(_limit):
        import time
        while process.stdin.write.call_count <= process.stdout.readline.call_count - 1:
            time.sleep(0.001)
        request_id = process.stdin.write.call_count
        return (json.dumps({"version": 1, "id": request_id, "token": owner.token,
                            "command": "reply", "payload": {"ready": True, "version": 1}
                            if request_id == 1 else {"opened": "calendar"}}).encode() + b"\n")
    process.stdout.readline.side_effect = reply
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


def test_parallel_exchange_refuses_second_reader(tmp_path):
    process = FakeProcess()
    owner = sidecar_launch.SidecarWindow(tmp_path / "sidecar.exe")
    owner.process = process
    assert owner._exchange_lock.acquire(blocking=False)
    try:
        import pytest
        with pytest.raises(RuntimeError, match="busy"):
            owner._exchange("open", {"view": "calendar"})
        process.stdin.write.assert_not_called()
        process.stdout.readline.assert_not_called()
    finally:
        owner._exchange_lock.release()


class ReplyingProcess:
    """In-memory child boundary: real protocol/reader, no OS process or pipe."""

    def __init__(self, token, *, pause_command=None, entered=None, release=None,
                 ready=True):
        self.token = token
        self.pause_command = pause_command
        self.entered = entered
        self.release = release
        self.ready = ready
        self.stdin = self
        self.stdout = self
        self.replies = queue.Queue()
        self.commands = []
        self.dead = False

    def poll(self):
        return 0 if self.dead else None

    def write(self, raw):
        frame = sidecar_launch.sidecar_protocol.decode(raw.rstrip(b"\n"), self.token)
        command = frame["command"]
        self.commands.append(command)
        if command == self.pause_command:
            self.entered.set()
            assert self.release.wait(3), "test failed to release paused child"
        if command == "hello":
            payload = {"ready": self.ready, "version": 1}
        elif command == "open":
            payload = {"opened": frame["payload"]["view"]}
        else:
            payload = {command: True}
        self.replies.put(sidecar_launch.sidecar_protocol.encode(
            frame["id"], self.token, "reply", payload) + b"\n")

    def flush(self):
        pass

    def readline(self, limit):
        return self.replies.get(timeout=4)

    def terminate(self):
        self.dead = True
        self.replies.put(b"")

    def wait(self, timeout=None):
        return 0

    def close(self):
        self.terminate()


def overlapping_calls(first, second, entered, release):
    """Keep the first operation paused while the second caller enters."""
    results = [None, None]
    errors = []
    second_started = threading.Event()
    second_done = threading.Event()

    def call(index, operation):
        if index == 1:
            second_started.set()
        try:
            results[index] = operation()
        except Exception as exc:
            errors.append(exc)
        finally:
            if index == 1:
                second_done.set()

    threads = [threading.Thread(target=call, args=(0, first), daemon=True),
               threading.Thread(target=call, args=(1, second), daemon=True)]
    try:
        threads[0].start()
        assert entered.wait(2), "first call did not reach the paused boundary"
        threads[1].start()
        assert second_started.wait(2)
        # Give the overlapping caller a bounded opportunity to expose the race;
        # fixed code instead waits for the first owner operation to complete.
        second_done.wait(0.1)
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=4)
    assert not any(thread.is_alive() for thread in threads), "owner operation hung"
    assert not errors, errors
    return results


def test_overlapping_startup_spawns_one_responsive_child(tmp_path):
    exe = tmp_path / "sidecar.exe"
    exe.write_bytes(b"fake executable; never run")
    entered, release = threading.Event(), threading.Event()
    processes = []

    def spawn(_args, **kwargs):
        process = ReplyingProcess(kwargs["env"]["LITETUI_SIDECAR_TOKEN"])
        processes.append(process)
        if len(processes) == 1:
            entered.set()
            assert release.wait(3)
        return process

    owner = sidecar_launch.SidecarWindow(exe, spawn=spawn, timeout=1)
    try:
        results = overlapping_calls(lambda: owner.open("settings"),
                                    lambda: owner.open("timeline"), entered, release)
        assert results == [True, True]
        assert len(processes) == 1
        assert owner.process is processes[0] and not processes[0].dead
        assert owner.open("calendar"), "surviving child must still answer"
    finally:
        owner.close()
        for process in processes:
            process.close()


def test_overlapping_handshake_keeps_child_alive(tmp_path):
    exe = tmp_path / "sidecar.exe"
    exe.write_bytes(b"fake executable; never run")
    entered, release = threading.Event(), threading.Event()
    processes = []

    def spawn(_args, **kwargs):
        process = ReplyingProcess(kwargs["env"]["LITETUI_SIDECAR_TOKEN"],
                                  pause_command="hello", entered=entered, release=release)
        processes.append(process)
        return process

    owner = sidecar_launch.SidecarWindow(exe, spawn=spawn, timeout=1)
    try:
        assert overlapping_calls(lambda: owner.open("settings"),
                                 lambda: owner.open("timeline"), entered, release) == [True, True]
        assert len(processes) == 1 and not processes[0].dead
        assert owner.open("calendar")
    finally:
        owner.close()
        for process in processes:
            process.close()


def test_overlapping_settings_snapshots_both_deliver_without_closing_child(tmp_path):
    exe = tmp_path / "sidecar.exe"
    exe.write_bytes(b"fake executable; never run")
    entered, release = threading.Event(), threading.Event()
    processes = []

    def spawn(_args, **kwargs):
        process = ReplyingProcess(kwargs["env"]["LITETUI_SIDECAR_TOKEN"],
                                  pause_command="settings_snapshot", entered=entered, release=release)
        processes.append(process)
        return process

    owner = sidecar_launch.SidecarWindow(exe, spawn=spawn, timeout=1)
    try:
        assert overlapping_calls(lambda: owner.open_settings_snapshot({"fields": {}}),
                                 lambda: owner.open_settings_snapshot({"fields": {}}),
                                 entered, release) == [True, True]
        assert len(processes) == 1 and not processes[0].dead
        assert processes[0].commands.count("settings_snapshot") == 2
        assert owner.open("timeline")
    finally:
        owner.close()
        for process in processes:
            process.close()
