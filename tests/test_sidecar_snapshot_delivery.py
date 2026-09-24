"""Authoritative read-only snapshot crosses the pipe only on Settings view."""
from pathlib import Path
from unittest.mock import Mock

from litetui.plugins import sidecar_plugin
from litetui.sidecar_launch import SidecarWindow


def test_send_settings_snapshot_after_open(tmp_path):
    owner = SidecarWindow(Path("preview.exe"))
    owner.open = Mock(return_value=True)
    owner._exchange = Mock(return_value={"settings_snapshot": True})
    assert owner.open_settings_snapshot({"revisions": {}, "fields": {}})
    owner.open.assert_called_once_with("settings")
    owner._exchange.assert_called_once_with("settings_snapshot", {"revisions": {}, "fields": {}})


def test_snapshot_error_falls_back_to_textual(tmp_path):
    owner = SidecarWindow(Path("preview.exe"))
    owner.open = Mock(return_value=True)
    owner._exchange = Mock(side_effect=TimeoutError("stalled"))
    owner.close = Mock()
    assert not owner.open_settings_snapshot({"revisions": {}, "fields": {}})
    owner.close.assert_called_once()


def test_non_settings_preview_does_not_read_settings():
    owner = Mock()
    owner.open.return_value = True
    app = Mock()
    app.call_from_thread.side_effect = lambda fn, text: fn(text)
    sidecar_plugin._open_background(app, owner, "timeline")
    owner.open.assert_called_once_with("timeline")
    owner.open_settings_snapshot.assert_not_called()
