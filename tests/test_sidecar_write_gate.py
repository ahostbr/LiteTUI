"""The native preview cannot mutate settings before editor parity is approved."""
import threading
from pathlib import Path
from unittest.mock import Mock

from test_sidecar_events import frame
from test_sidecar_reader import PipeProcess

from litetui.sidecar_launch import SidecarWindow


def test_settings_patch_refused_without_handshake_write_capability():
    process = PipeProcess()
    owner = SidecarWindow(Path("unused.exe"), timeout=1)
    owner.token = "a" * 48
    owner.process = process
    owner.on_event = Mock()
    rejected = []
    owner.on_rejected_frame = rejected.append
    owner._start_reader(process)
    process.respond(frame(owner, 7, "settings_patch", {"changes": [], "expected_revisions": {}}))
    for _ in range(100):
        if rejected:
            break
        threading.Event().wait(0.01)
    assert rejected == ["settings_write_disabled"]
    owner.on_event.assert_not_called()
    assert owner._reader_error is None
    owner.close()
