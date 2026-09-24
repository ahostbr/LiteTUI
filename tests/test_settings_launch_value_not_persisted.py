"""A TUI /settings save must not persist a value the user never changed.

Write contract (Docs/Plans/sidecar-settings-parity.md, cross-cutting): controls
"must expose effective versus saved values and not persist an unchanged
invocation override".

An instance started with `--backend codex` holds backend='codex' in app.settings
(the launch value) while its conversation's saved backend is something else. The
/settings form opens on app.settings; its adapter used to diff that form against
SettingsService.snapshot().effective, which knows environment overrides but not
launch ones. So saving ANY unrelated field also wrote backend='codex'.

These tests drive the real /settings wiring (plugins/settings_ui._cmd_settings),
capturing the adapter bindings it hands to the dialog.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from litetui import settings_runtime
from litetui.plugins import settings_ui
from litetui.settings import Settings
from litetui.settings_service import SettingsService
from litetui.settings_ui_adapter import SettingsUiAdapter


@pytest.fixture
def launched(tmp_path, monkeypatch):
    """`litetui --backend codex` on a conversation whose saved engine differs."""
    monkeypatch.delenv("LITETUI_BACKEND", raising=False)  # conftest pins it
    directory = tmp_path / ".convos" / "abc"
    directory.mkdir(parents=True)
    app = SimpleNamespace(settings=Settings(), convo_dir=directory, _settings_service=SettingsService(tmp_path),
                          available_models=[])
    app._invocation_saved_values = {"backend": app.settings.backend}
    app._cli_initial_backend = "codex"
    app.settings.backend = "codex"
    return app


def settings_dialog_save(app, monkeypatch, **edits):
    """Open /settings the way the command does, edit the form, press Save."""
    seen = {}
    monkeypatch.setattr(settings_ui, "present_dialog", lambda _app, body, _screen, _done: seen.update(body=body))
    monkeypatch.setattr(settings_ui, "mcp_server_names", lambda _app: [])
    monkeypatch.setattr(settings_ui.model_residency, "resident_models", lambda _app: (set(), True))
    settings_ui._cmd_settings(app, "/settings", "")
    bindings = seen["body"].keywords
    adapter = SettingsUiAdapter(seen["body"].args[0], **bindings)
    form = deepcopy(app.settings)  # the form opens on app.settings
    for key, value in edits.items():
        setattr(form, key, value)
    return adapter.save(form)


def test_an_unrelated_save_does_not_persist_the_launch_engine(launched, monkeypatch):
    saved_before = launched._settings_service.snapshot("abc").saved.backend
    settings_dialog_save(launched, monkeypatch, show_stop_time=True)
    snapshot = launched._settings_service.snapshot("abc")
    assert snapshot.saved.show_stop_time is True  # the edit the user made
    assert snapshot.saved.backend == saved_before  # was 'codex' before the fix
    assert launched._invocation_saved_values == {"backend": saved_before}  # the launch pin survives
    assert launched.settings.backend == "codex"


def test_a_deliberate_engine_change_still_saves_and_survives_reconnect(launched, monkeypatch):
    """46a18b3 made this work; the fix must not break it."""
    settings_dialog_save(launched, monkeypatch, backend="llamacpp")
    assert launched._settings_service.snapshot("abc").saved.backend == "llamacpp"
    assert launched._invocation_saved_values == {}
    settings_runtime.prepare_reconnect(launched)
    assert launched.settings.backend == "llamacpp"
