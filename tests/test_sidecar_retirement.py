"""Sidecar cleanup retains an exact child and joins its active reader."""
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_sidecar_reader import PipeProcess

from litetui.sidecar_launch import SidecarWindow


def owned(process, *, timeout=0.03):
    window = SidecarWindow(Path('unused.exe'), timeout=timeout)
    window.process = process
    window.token = 'a' * 48
    return window


def test_failed_child_reap_retains_handle_and_allows_exact_retry():
    process = PipeProcess()
    process.terminate = Mock(side_effect=OSError('blocked terminate'))
    window = owned(process)
    with pytest.raises(OSError, match='blocked terminate'):
        window.close()
    assert window.process is process
    process.stdin.close.assert_not_called()
    process.terminate = lambda: setattr(process, 'dead', True)
    assert window.close()
    assert window.process is None


def test_killed_child_must_be_observed_reaped_before_release():
    process = PipeProcess()
    process.terminate = Mock()
    process.kill = Mock()
    process.wait = Mock(side_effect=subprocess.TimeoutExpired('sidecar', 0.03))
    window = owned(process)
    with pytest.raises(subprocess.TimeoutExpired):
        window.close()
    assert window.process is process
    process.kill.assert_called_once()
    process.dead = True
    process.wait = Mock(return_value=0)
    assert window.close()


def test_reader_timeout_retains_owner_until_callback_joins():
    process = PipeProcess()
    window = owned(process)
    entered, release = threading.Event(), threading.Event()
    window.on_event = lambda _frame: (entered.set(), release.wait(3))
    window._start_reader(process)
    from litetui.sidecar_protocol import encode
    process.respond(encode(1, window.token, 'settings_request', {}) + b'\n')
    assert entered.wait(1)
    try:
        with pytest.raises(RuntimeError, match='reader'):
            window.close()
        assert window.process is process
        assert window._reader.is_alive()
        process.stdin.close.assert_not_called()
    finally:
        release.set()
        window._reader.join(2)
    assert window.close()
    assert window.process is None
    assert not window._reader.is_alive()


def test_reader_cannot_close_its_own_owner():
    process = PipeProcess()
    window = owned(process)
    finished = threading.Event()
    errors = []
    def close(_frame):
        try:
            window.close()
        except RuntimeError as exc:
            errors.append(str(exc))
        finally:
            finished.set()
    window.on_event = close
    window._start_reader(process)
    from litetui.sidecar_protocol import encode
    process.respond(encode(1, window.token, 'settings_request', {}) + b'\n')
    assert finished.wait(1)
    try:
        assert errors and 'reader' in errors[0]
        assert window.process is process
    finally:
        window.close()


def test_pending_cleanup_cannot_spawn_replacement(tmp_path):
    exe = tmp_path / 'sidecar.exe'
    exe.write_bytes(b'fixture never executed')
    process = PipeProcess()
    process.terminate = Mock(side_effect=OSError('blocked'))
    spawn = Mock()
    window = SidecarWindow(exe, spawn=spawn, timeout=0.03)
    window.process = process
    with pytest.raises(OSError):
        window.close()
    with pytest.raises(RuntimeError, match='cleanup'):
        window.open('settings')
    spawn.assert_not_called()
    assert window.process is process
    process.terminate = lambda: setattr(process, 'dead', True)
    assert window.close()


def test_failed_pipe_close_retains_owner_and_closes_other_pipe():
    process = PipeProcess()
    process.stdin.close.side_effect = OSError('pipe close blocked')
    closed = Mock(wraps=process.close)
    process.close = closed
    window = owned(process)
    with pytest.raises(OSError, match='pipe close blocked'):
        window.close()
    assert window.process is process
    closed.assert_called_once()
    process.stdin.close.side_effect = None
    assert window.close()


def test_real_private_child_pipes_and_reader_join_on_close():
    import sys
    child = '''import json,os,sys
for line in sys.stdin:
    request=json.loads(line)
    payload={'ready':True,'version':1} if request['command']=='hello' else {'opened':request['payload']['view']}
    print(json.dumps({**request,'command':'reply','payload':payload}),flush=True)
'''
    processes = []
    def spawn(_args, **kwargs):
        process = subprocess.Popen([sys.executable, '-u', '-c', child], **kwargs)
        processes.append(process)
        return process
    window = SidecarWindow(Path(sys.executable), spawn=spawn, timeout=2)
    try:
        assert window.open('settings')
        assert window.open('calendar')
        process = window.process
        reader = window._reader
        assert reader.is_alive() and process.poll() is None
        assert window.close()
        assert process.poll() is not None
        assert not reader.is_alive()
        assert process.stdin.closed and process.stdout.closed
        assert window.process is None
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
        if window.process is not None:
            window.close()


def test_reader_start_failure_reaps_child_closes_pipes_and_releases_owner(tmp_path, monkeypatch):
    executable = tmp_path / 'sidecar.exe'
    executable.write_bytes(b'fixture never executed')
    process = PipeProcess()
    spawn = Mock(return_value=process)
    window = SidecarWindow(executable, spawn=spawn, timeout=0.03)
    def fail_start(_thread):
        raise RuntimeError('reader start failed')
    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    assert not window.open('settings')
    spawn.assert_called_once()
    assert process.poll() == 0
    process.stdin.close.assert_called_once()
    assert window.process is None
    assert not window._closing
    assert window._reader.ident is None
    assert not window.close()
