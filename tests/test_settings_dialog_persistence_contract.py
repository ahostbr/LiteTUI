"""Settings-dialog persistence-contract regressions (WS1/WS5) — OpenBolt.

Complements ``test_settings_restore_isolation.py`` (R1/R2, the persistence
boundary: real JSON on disk, the mounted ``SettingsScreen``). Those tests end
with a note that "App-side runtime is C1's domain"; this file closes the two
remaining items of the WS1/WS5 gap inventory (section 7) that live at the
dialog's own persistence contract:

C1 — conversation targeting. The app-side guard in
     ``plugins/settings_ui.py:_cmd_settings`` captures
     ``conversation_id = app.convo_dir.name`` AT DIALOG-OPEN and closes over it
     in the ``save_patch`` / ``runtime_apply`` bindings it hands to the dialog.
     ``still_current()`` is ``getattr(app, "convo_dir", None) == <open-time
     dir>``; if the conversation moves mid-dialog (a gui_rpc/background switch
     while the modal is up) BOTH closures REFUSE: ``save_patch`` returns a
     failed ``PersistenceDestinationResult`` ("Conversation changed; close and
     reopen /settings.") and never calls the service; ``runtime_apply`` returns
     failed statuses and never applies. Writing the WRONG conversation is the
     multi-instance defect these pin.

R3 — unbound host. With no service bound the adapter is disabled, and a
     confirmed Restore yields a factory ``Settings()`` through ``close_dialog``
     (``settings_screen.py:_on_restore_answer``) — the legacy unbound-host
     contract — rather than attempting any persistence or apply.

Both drive the real closure / real mounted screen; only the model-residency
read and the settings service are faked, and the fakes RECORD the conversation
a write routes to so the assertions read routing, not a return value.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from textual.app import App

from litetui.settings import Settings
from litetui.settings_apply import (
    PersistenceDestinationResult,
    SettingsSaveResult,
)
from litetui.settings_service import SettingChange
from litetui.settings_screen import SettingsBody, SettingsScreen


# ── C1 helpers: drive the real _cmd_settings, capture the bindings closure ───

def _open_bindings(monkeypatch, tmp_path):
    """Build ``_cmd_settings``' bindings with a recording fake service and a
    mocked ``present_dialog``. Returns
    ``(app, service, bindings, apply_calls, convo_a, convo_b)``.

    ``service.written`` records the conversation id each ``save_patch`` routes
    to; ``apply_calls`` records any ``settings_runtime.apply_saved_result``
    call. ``present_dialog`` is mocked so no screen is built — the point is the
    closures it receives, not the dialog itself.
    """
    import litetui.model_residency as mr
    import litetui.plugins.settings_ui as sui
    import litetui.settings_runtime as sr

    root = tmp_path / "root"
    convo_a = root / "convos" / "A"
    convo_b = root / "convos" / "B"
    convo_a.mkdir(parents=True)
    convo_b.mkdir(parents=True)

    class _RecordingService:
        def __init__(self):
            self.written = []

        def snapshot(self, convo_id):
            # Never invoked: present_dialog is mocked, so the adapter is never
            # built and snapshot_provider never fires. Stub for completeness.
            return NS(effective=Settings(), saved=Settings(), revisions={})

        def save_patch(self, convo_id, changes, revisions):
            self.written.append(convo_id)
            return SettingsSaveResult((PersistenceDestinationResult(
                str(convo_id), "conversation", True,
                fields=tuple(c.key for c in changes)),))

    service = _RecordingService()
    apply_calls = []
    app = NS(
        available_models=["m1"],
        # Non-empty servers dict so mcp_server_names reads the keys and never
        # touches mcp.json / .mcp.json on disk.
        mcp=NS(servers={"probe": {}}),
        settings=Settings(),
        convo_dir=convo_a,
    )

    captured = {}

    def fake_present_dialog(app, body_factory, screen_factory, callback):
        captured["bindings"] = body_factory.keywords

    monkeypatch.setattr(sui, "present_dialog", fake_present_dialog)
    monkeypatch.setattr(mr, "resident_models", lambda app: ([], []))
    monkeypatch.setattr(sr, "service_for", lambda app: service)
    monkeypatch.setattr(sr, "apply_saved_result",
                        lambda app, requested, result: apply_calls.append(result))

    sui._cmd_settings(app, "/settings", "")
    return app, service, captured["bindings"], apply_calls, convo_a, convo_b


def _change() -> list[SettingChange]:
    return [SettingChange("tool_iterations", 5, "conversation")]


# ── C1: conversation targeting (app-side guard) ──────────────────────────────

def test_settings_dialog_routes_save_to_open_time_conversation(tmp_path, monkeypatch):
    """C1(a): while the conversation is unchanged, save routes to the
    OPEN-TIME conversation (the captured id), not whatever is current."""
    app, service, bindings, _apply, _a, _b = _open_bindings(monkeypatch, tmp_path)

    result = bindings["save_patch"](_change(), {})

    assert service.written == ["A"], f"save routed to {service.written}, expected open-time A"
    assert result.fully_saved


def test_settings_dialog_refuses_save_after_convo_switch(tmp_path, monkeypatch):
    """C1(b) — the discriminator. A mid-dialog conversation switch makes save
    REFUSE. The closure writes to the captured OPEN-TIME id, so if the
    ``still_current`` guard were absent this second save would silently write
    to the (now-stale) open-time conversation AGAIN and ``service.written``
    would be ``['A', 'A']`` — a write landing in a conversation the user has
    moved off. This assertion is load-bearing on the guard, not vacuously true."""
    app, service, bindings, _apply, _a, convo_b = _open_bindings(monkeypatch, tmp_path)
    bindings["save_patch"](_change(), {})          # positive control: writes open-time A

    app.convo_dir = convo_b                        # mid-dialog gui_rpc/background switch
    refused = bindings["save_patch"](_change(), {})

    assert service.written == ["A"], f"switched write must be refused, got {service.written}"
    assert refused.persistence and refused.persistence[0].saved is False
    assert "Conversation changed" in refused.persistence[0].error
    assert not refused.fully_saved
    assert refused.persistence[0].fields == ("tool_iterations",)


def test_settings_dialog_refuses_runtime_apply_after_convo_switch(tmp_path, monkeypatch):
    """C1(b) runtime half: after the switch, ``runtime_apply`` returns failed
    statuses and never applies (no ``settings_runtime.apply_saved_result``
    call); the passed persistence is carried through unchanged."""
    app, _service, bindings, apply_calls, _a, convo_b = _open_bindings(monkeypatch, tmp_path)

    saved = SettingsSaveResult((PersistenceDestinationResult(
        "A", "conversation", True, fields=("tool_iterations",)),))

    app.convo_dir = convo_b
    out = bindings["runtime_apply"](Settings(), saved)

    assert apply_calls == [], "a switched conversation must not apply runtime settings"
    assert out.has_runtime_failures
    status = {s.field: s for s in out.runtime}["tool_iterations"]
    assert status.status == "failed"
    assert "Conversation changed" in status.reason
    assert out.persistence == saved.persistence


# ── R3: unbound host yields a factory Settings on Restore ─────────────────────

class _Host(App):
    def __init__(self, screen: SettingsScreen):
        super().__init__()
        self._start_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._start_screen)


@pytest.mark.asyncio
async def test_restore_unbound_host_returns_factory_settings(tmp_path, monkeypatch):
    """R3: with no service bound (adapter disabled), a confirmed Restore yields
    a factory ``Settings()`` through ``close_dialog`` — the legacy unbound-host
    contract — without attempting persistence or apply."""
    import litetui.settings_screen as ss

    screen = SettingsScreen(Settings())            # no bindings -> adapter disabled
    captured = {}
    monkeypatch.setattr(
        ss, "close_dialog",
        lambda widget, value=None: captured.setdefault("value", value),
    )

    app = _Host(screen)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        body = pilot.app.screen.query_one(SettingsBody)
        assert not body._settings_adapter.enabled, "precondition: host is unbound"
        body._on_restore_answer("restore")

    assert "value" in captured, "unbound Restore must close the dialog"
    assert isinstance(captured["value"], Settings)
    assert captured["value"] == Settings(), "unbound Restore returns the factory value"
