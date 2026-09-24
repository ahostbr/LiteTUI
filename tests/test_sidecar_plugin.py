"""Preview command cannot displace existing Textual editors or block the UI."""
from types import SimpleNamespace
from unittest.mock import Mock

from litetui.plugins import sidecar_plugin


def app(enabled=True):
    messages = []
    return SimpleNamespace(
        settings=SimpleNamespace(sidecar_enabled=enabled),
        system_message=messages.append,
        call_from_thread=lambda fn, message: fn(message),
        messages=messages,
    )


def test_disabled_or_bad_view_never_spawns(monkeypatch):
    owner = Mock()
    monkeypatch.setattr(sidecar_plugin, "_new_window", lambda _app: owner)
    a = app(False)
    sidecar_plugin._handle(a, "/sidecar", "timeline")
    assert "disabled" in a.messages[-1]
    assert not hasattr(a, "_sidecar_preview")
    a.settings.sidecar_enabled = True
    sidecar_plugin._handle(a, "/sidecar", "invalid")
    assert "Usage" in a.messages[-1]
    owner.open.assert_not_called()


def test_preview_async_and_existing_editors_not_replaced(monkeypatch):
    a = app()
    owner = Mock()
    owner.open.return_value = True
    monkeypatch.setattr(sidecar_plugin, "_new_window", lambda _app: owner)
    threads = []

    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            threads.append(target)
        def start(self):
            pass

    monkeypatch.setattr(sidecar_plugin.threading, "Thread", DeferredThread)
    sidecar_plugin._handle(a, "/sidecar", "timeline")
    owner.open.assert_not_called()  # no pipe I/O on Textual's event loop
    assert len(threads) == 1
    threads.pop()()
    owner.open.assert_called_once_with("timeline")
    assert "preview" in a.messages[-1].lower()


def test_missing_binary_reports_once_without_spawn(monkeypatch):
    a = app()
    owner = Mock()
    owner.open.return_value = False
    monkeypatch.setattr(sidecar_plugin, "_new_window", lambda _app: owner)
    sidecar_plugin._handle(a, "/sidecar", "settings")
    import time
    for _ in range(100):
        if a.messages:
            break
        time.sleep(0.01)
    assert len(a.messages) == 1
    assert "Textual" in a.messages[0]
