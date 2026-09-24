"""Child edit event is serialized onto Textual and correlated without replay."""
from unittest.mock import Mock

from litetui.sidecar_dispatch import SettingsPatchDispatcher


def test_dispatch_uses_ui_thread_and_returns_correlated_result():
    app = Mock()
    app.call_from_thread.side_effect = lambda fn, *args: fn(*args)
    owner = Mock()
    handler = SettingsPatchDispatcher(app, owner, apply=lambda _app, payload: {"saved": True, "revisions": payload})
    handler({"id": 5, "command": "settings_patch", "payload": {"changes": []}})
    app.call_from_thread.assert_called_once()
    owner.send_event_reply.assert_called_once_with(5, {"saved": True, "revisions": {"changes": []}})
    handler({"id": 5, "command": "settings_patch", "payload": {"changes": []}})
    owner.send_event_reply.assert_called_once()


def test_dispatch_validation_error_cannot_claim_save():
    app = Mock()
    app.call_from_thread.side_effect = lambda fn, *args: fn(*args)
    owner = Mock()
    handler = SettingsPatchDispatcher(app, owner, apply=lambda _app, _p: (_ for _ in ()).throw(ValueError("invalid")))
    handler({"id": 8, "command": "settings_patch", "payload": {}})
    owner.send_event_reply.assert_called_once_with(8, {"saved": False, "error": "invalid"})
