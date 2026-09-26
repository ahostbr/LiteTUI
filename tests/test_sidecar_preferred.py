"""T1028: the "Prefer optional native sidecar" toggle routes /settings and /calendar,
a window that cannot launch hands over to Textual and says why, and a window
switched to its Settings tab can ask the parent for the settings it was never sent."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from test_sidecar_reader import PipeProcess

from litetui.plugins import scheduler_plugin, settings_ui, sidecar_plugin
from litetui.sidecar_dispatch import SettingsPatchDispatcher
from litetui.sidecar_launch import SidecarWindow


class Window:
    """Stands in for SidecarWindow: opens, or fails the way it does (warn, then False)."""
    def __init__(self, failure=None, raises=None):
        self.failure, self.raises, self.opened = failure, raises, []
        self.warn = lambda _message: None

    def _open(self, view):
        if self.raises:
            raise self.raises
        if self.failure:
            self.warn(self.failure)
            return False
        self.opened.append(view)
        return True

    def open_settings_snapshot(self, snapshot):
        return self._open("settings")

    def open_jobs_snapshot(self, view, snapshot):
        return self._open(view)


def app(monkeypatch, window, enabled=True):
    messages = []
    a = SimpleNamespace(settings=SimpleNamespace(sidecar_enabled=enabled), jobs=[],
                        convo_dir=Path("c1"), system_message=messages.append,
                        call_from_thread=lambda fn, *args: fn(*args), messages=messages)
    monkeypatch.setattr(sidecar_plugin, "_new_window", lambda _app: window)
    monkeypatch.setattr(sidecar_plugin, "settings_snapshot", lambda _app: {"fields": {}, "revisions": {}})

    class InlineThread:  # the launch thread, run to completion where the test can see it
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            self.target()

    monkeypatch.setattr(sidecar_plugin.threading, "Thread", InlineThread)
    return a


def test_toggle_off_opens_textual_and_never_spawns(monkeypatch):
    window = Window()
    a = app(monkeypatch, window, enabled=False)
    textual = Mock()
    sidecar_plugin.open_preferred(a, "settings", textual)
    textual.assert_called_once_with()
    assert window.opened == [] and not hasattr(a, "_sidecar_preview")


def test_toggle_on_opens_native_instead_of_textual(monkeypatch):
    window = Window()
    a = app(monkeypatch, window)
    textual = Mock()
    sidecar_plugin.open_preferred(a, "settings", textual)
    sidecar_plugin.open_preferred(a, "calendar", textual)
    assert window.opened == ["settings", "calendar"]
    textual.assert_not_called()


def test_launch_failure_falls_back_to_textual_and_shows_why(monkeypatch):
    window = Window(failure="Sidecar is not installed: C:/x/litetui-sidecar.exe; use Textual instead.")
    a = app(monkeypatch, window)
    textual = Mock()
    sidecar_plugin.open_preferred(a, "settings", textual)
    textual.assert_called_once_with()
    assert a.messages == ["[sidecar] Sidecar is not installed: C:/x/litetui-sidecar.exe; use Textual instead."]


def test_launch_exception_falls_back_to_textual(monkeypatch):
    a = app(monkeypatch, Window(raises=OSError("pipe")))
    textual = Mock()
    sidecar_plugin.open_preferred(a, "calendar", textual)
    textual.assert_called_once_with()
    assert "OSError" in a.messages[-1]


def test_explicit_sidecar_command_reports_the_reason_too(monkeypatch):
    a = app(monkeypatch, Window(failure="Sidecar handshake/launch failed: boom; use Textual instead."))
    sidecar_plugin._handle(a, "/sidecar", "timeline")
    assert a.messages == ["[sidecar] Sidecar handshake/launch failed: boom; use Textual instead.",
                          "[sidecar] Preview unavailable; use Textual /settings, /calendar or /job."]


def test_settings_and_calendar_commands_route_through_the_toggle(monkeypatch):
    routed = []
    monkeypatch.setattr(sidecar_plugin, "open_preferred", lambda a, view, textual: routed.append((view, textual)))
    textual_settings = Mock()
    monkeypatch.setattr(settings_ui, "_open_textual_settings", textual_settings)
    present = Mock()
    monkeypatch.setattr(scheduler_plugin, "present_dialog", present)
    a = SimpleNamespace(jobs=[])
    settings_ui._cmd_settings(a, "/settings", "")
    scheduler_plugin._cmd_calendar(a, "/calendar", "")
    assert [view for view, _ in routed] == ["settings", "calendar"]
    for _, textual in routed:  # each fallback is that command's own Textual screen
        textual()
    textual_settings.assert_called_once_with(a)
    present.assert_called_once()


# ── The window's Settings tab asks the parent (the /sidecar -> Settings-tab path) ──

def _frame(owner, request_id, command, payload=None):
    return (json.dumps({"version": 1, "id": request_id, "token": owner.token,
                        "command": command, "payload": payload or {}}).encode() + b"\n")


def test_reader_routes_settings_request_without_the_write_grant():
    process = PipeProcess()
    owner = SidecarWindow(Path("unused.exe"), timeout=1)
    owner.token = "a" * 48
    owner.process = process
    events, rejected = [], []
    owner.on_event = events.append
    owner.on_rejected_frame = rejected.append
    owner._start_reader(process)
    process.respond(_frame(owner, 2**32, "settings_request"))
    for _ in range(100):
        if events:
            break
        threading.Event().wait(0.01)
    assert [e["command"] for e in events] == ["settings_request"] and rejected == []
    owner.close()


def test_dispatcher_answers_a_request_with_the_current_snapshot_not_a_save():
    a = Mock()
    a.call_from_thread.side_effect = lambda fn, *args: fn(*args)
    owner, apply = Mock(), Mock()
    handler = SettingsPatchDispatcher(a, owner, apply=apply,
                                      snapshot=lambda _app: {"snapshot": {"fields": {}, "revisions": {"g": "1"}}})
    handler({"id": 9, "command": "settings_request", "payload": {}})
    owner.send_event_reply.assert_called_once_with(9, {"snapshot": {"fields": {}, "revisions": {"g": "1"}}})
    apply.assert_not_called()


def test_request_reads_settings_when_asked(monkeypatch):
    calls = []
    monkeypatch.setattr(sidecar_plugin, "settings_snapshot", lambda a: calls.append(a) or {"fields": {}})
    a = SimpleNamespace(convo_dir=Path("c1"))
    assert sidecar_plugin._requested_snapshot(a) == {"snapshot": {"fields": {}}}
    assert sidecar_plugin._requested_snapshot(a) == {"snapshot": {"fields": {}}}
    assert len(calls) == 2  # fresh on every switch, never cached from launch
    assert "error" in sidecar_plugin._requested_snapshot(SimpleNamespace(convo_dir=None))
