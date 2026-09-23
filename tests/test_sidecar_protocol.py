"""Wire compatibility and rejection contract for the optional Rust sidecar."""
import json

import pytest

from litetui import sidecar_protocol


def test_parent_frame_round_trips_and_is_versioned():
    raw = sidecar_protocol.encode(7, "capability", "open", {"view": "settings"})
    assert sidecar_protocol.decode(raw, "capability") == {
        "version": 1,
        "id": 7,
        "token": "capability",
        "command": "open",
        "payload": {"view": "settings"},
    }


@pytest.mark.parametrize("frame", [
    {"version": 2, "id": 7, "token": "capability", "command": "open"},
    {"version": 1, "id": 7, "token": "wrong", "command": "open"},
    {"version": 1, "id": 7, "token": "capability", "command": "open", "extra": True},
    {"version": 1, "id": -1, "token": "capability", "command": "open"},
])
def test_invalid_frames_rejected(frame):
    with pytest.raises(ValueError):
        sidecar_protocol.decode(json.dumps(frame).encode(), "capability")


def test_oversize_and_empty_capability_rejected():
    with pytest.raises(ValueError):
        sidecar_protocol.decode(b"x" * (sidecar_protocol.MAX_FRAME_BYTES + 1), "capability")
    with pytest.raises(ValueError):
        sidecar_protocol.encode(1, "", "open", {})
