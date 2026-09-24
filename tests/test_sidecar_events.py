"""Bad child frames are logged/dropped; valid allowlisted events survive."""
import json
import threading
from pathlib import Path

from test_sidecar_reader import PipeProcess

from litetui import sidecar_protocol
from litetui.sidecar_launch import SidecarWindow


def frame(owner, request_id, command, payload=None, token=None):
    return (json.dumps({"version": 1, "id": request_id, "token": token or owner.token,
                        "command": command, "payload": payload or {}}).encode() + b"\n")


def test_malformed_unmatched_unknown_bad_token_drop_without_disconnect():
    process = PipeProcess()
    owner = SidecarWindow(Path("unused.exe"), timeout=1)
    owner.token = "a" * 48
    owner.process = process
    rejected = []
    events = []
    owner.on_event = lambda message: events.append(message)
    owner.on_rejected_frame = rejected.append
    owner._start_reader(process)
    process.respond(b"not-json\n")
    process.respond(frame(owner, 999, "reply", {"ok": True}))
    process.respond(frame(owner, 5, "unlisted"))
    process.respond(frame(owner, 6, "settings_patch", token="bad"))
    process.respond(frame(owner, 7, "settings_patch", {"changes": [], "expected_revisions": {}}))
    ready = threading.Event()
    threading.Timer(0.05, ready.set).start()
    assert ready.wait(1)
    assert len(rejected) == 4
    assert [event["id"] for event in events] == [7]
    assert owner._reader_error is None
    owner.close()


def test_oversized_frame_ends_reader_and_fails_pending():
    process = PipeProcess()
    owner = SidecarWindow(Path("unused.exe"), timeout=1)
    owner.token = "a" * 48
    owner.process = process
    owner._start_reader(process)
    process.respond(b"x" * (sidecar_protocol.MAX_FRAME_BYTES + 2))
    for _ in range(100):
        if owner._reader_error is not None:
            break
        threading.Event().wait(0.01)
    assert isinstance(owner._reader_error, ValueError)
    assert "length" in str(owner._reader_error)
    owner.close()
