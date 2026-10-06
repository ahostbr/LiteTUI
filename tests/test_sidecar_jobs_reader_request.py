"""The reader forwards read-only data requests without child/write effects."""
import json
import threading
from pathlib import Path

import pytest

from litetui.sidecar_launch import SidecarWindow
from test_sidecar_reader import PipeProcess


@pytest.mark.parametrize("command", ["settings_request", "jobs_request"])
def test_reader_forwards_readonly_requests_without_write_grants(command):
    process = PipeProcess()  # in-memory fake, never a subprocess
    owner = SidecarWindow(Path("unused.exe"), timeout=1)
    owner.token = "reader-only-test"
    owner.process = process
    received = threading.Event()
    events, rejected = [], []

    def on_event(frame):
        events.append(frame)
        received.set()

    owner.on_event = on_event
    owner.on_rejected_frame = rejected.append
    assert not owner.settings_write and not owner.jobs_write
    owner._start_reader(process)
    try:
        process.respond(json.dumps({"version": 1, "id": 2**32, "token": owner.token,
                                    "command": command, "payload": {}}).encode() + b"\n")
        assert received.wait(1)
        assert [frame["command"] for frame in events] == [command]
        assert rejected == []
        process.stdin.write.assert_not_called()
    finally:
        owner.close()  # closes only the fake pipe and reader thread
