"""Single reader correlates replies and fails pending work on child disconnect."""
import json
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from litetui.sidecar_launch import SidecarWindow


class PipeProcess:
    def __init__(self):
        self.stdin = Mock()
        self.lines = []
        self.cv = threading.Condition()
        self.stdout = self
        self.dead = False

    def poll(self):
        return None if not self.dead else 0

    def readline(self, limit):
        with self.cv:
            self.cv.wait_for(lambda: bool(self.lines) or self.dead, timeout=2)
            return self.lines.pop(0) if self.lines else b""

    def respond(self, raw):
        with self.cv:
            self.lines.append(raw)
            self.cv.notify_all()

    def terminate(self):
        self.dead = True
        with self.cv:
            self.cv.notify_all()

    def wait(self, timeout=None):
        pass

    def close(self):
        self.terminate()


def test_single_reader_correlates_reply_and_eof_fails_next_request():
    process = PipeProcess()
    owner = SidecarWindow(Path("unused.exe"), timeout=1)
    owner.process = process
    owner.token = "a" * 48
    owner._start_reader(process)

    def reply():
        process.respond(json.dumps({"version": 1, "id": 1, "token": owner.token,
                                    "command": "reply", "payload": {"ok": True}}).encode() + b"\n")

    threading.Timer(0.02, reply).start()
    assert owner._exchange("hello", {}) == {"ok": True}
    process.terminate()
    with pytest.raises(RuntimeError, match="disconnected"):
        owner._exchange("open", {"view": "job"})
    owner.close()


def test_eof_fails_in_flight_request_without_waiting_timeout():
    process = PipeProcess()
    owner = SidecarWindow(Path("unused.exe"), timeout=2)
    owner.process = process
    owner.token = "a" * 48
    owner._start_reader(process)
    threading.Timer(0.02, process.terminate).start()
    with pytest.raises(RuntimeError, match="disconnected"):
        owner._exchange("hello", {})
    owner.close()
