"""R1/R2 Restore-defaults isolation regressions (WS1/WS5) — RigidStem.

Real-boundary tests: a service-bound settings dialog drives an actual
``SettingsService`` that writes real JSON files on disk. Every assertion reads
the persisted file or re-snapshots the service — none trusts an adapter return
value alone.

R1 — env-owned exclusion (guard at ``settings_screen.py`` ``_dialog_default_target``,
     the ``or settings_mod.source_of(name)`` clause): confirmed Restore must NOT
     reset a field the environment is currently overriding. Writing the factory
     value back would be an accidental write of a value the env wins right back —
     the repo's signature "a control that silently does nothing / writes what it
     shouldn't" defect. ``pin_default_model`` is the positive control proving
     Restore actually ran; ``test_restore_env_guard_is_load_bearing`` neutralises
     ``source_of`` to prove the R1 assertion discriminates on the guard.

R2 — "never reset other conversations" (sub-settings.md:34): Restore in
     conversation A must leave conversation B's own ``settings.json`` bytes
     untouched. The global defaults file is app-wide and legitimately changes;
     only B's per-conversation file is asserted invariant.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest
from textual.app import App

import litetui.settings as settings_mod
from litetui.settings import Settings
from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingChange, SettingsService
from litetui.settings_screen import (
    SettingsBody,
    SettingsExitConfirm,
    SettingsScreen,
)


# ── real-boundary helpers ────────────────────────────────────────────────────

def _service(tmp_path) -> SettingsService:
    return SettingsService(tmp_path / "root", tmp_path / "root" / "convos")


def _seed(service: SettingsService, convo_id: str, **values) -> None:
    """Persist explicit conversation values through the real save_patch path."""
    snap = service.snapshot(convo_id)
    changes = [SettingChange(k, v, SETTING_SPECS[k].scope.value) for k, v in values.items()]
    result = service.save_patch(convo_id, changes, snap.revisions)
    failed = [p.error for p in result.persistence if not p.saved]
    assert not failed, failed


def _convo_file(service: SettingsService, convo_id: str):
    return service.conversation_root / convo_id / "settings.json"


def _execution(service: SettingsService, convo_id: str) -> dict:
    return json.loads(_convo_file(service, convo_id).read_text("utf-8"))["execution"]


def _bindings(service: SettingsService, convo_id: str) -> dict:
    # Mirrors plugins/settings_ui.py. runtime_apply is intentionally omitted:
    # these tests assert at the PERSISTENCE boundary (disk bytes + re-snapshot),
    # which the app-side runtime never touches. App-side runtime is C1's domain.
    return dict(
        snapshot_provider=lambda: service.snapshot(convo_id),
        save_patch=lambda changes, revisions: service.save_patch(convo_id, changes, revisions),
        runtime_apply=None,
    )


class _Host(App):
    backend = NS(name="lmstudio")
    model_id = "local-model"

    def __init__(self, screen: SettingsScreen):
        super().__init__()
        self._start_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._start_screen)


async def _open_body(pilot_app: _Host, pilot):
    await pilot.pause()
    return pilot_app.screen.query_one(SettingsBody)


async def _confirm_restore(pilot) -> None:
    body = pilot.app.screen.query_one(SettingsBody)
    body._defaults()
    await pilot.pause()
    assert isinstance(pilot.app.screen, SettingsExitConfirm)
    assert pilot.app.screen.restore is True
    await pilot.click("#settings-restore")
    await pilot.pause()
    await pilot.pause()


# ── R1: Restore excludes an env-overridden field ─────────────────────────────

@pytest.mark.asyncio
async def test_restore_excludes_env_owned_field(tmp_path, monkeypatch):
    service = _service(tmp_path)
    # Seed with env CLEAR so 7 persists as a genuine preference, not a masked one.
    monkeypatch.delenv("LM_TOOL_ITERS", raising=False)
    _seed(service, "A", tool_iterations=7, pin_default_model=True)

    # The environment now owns tool_iterations; the dialog renders it read-only.
    monkeypatch.setenv("LM_TOOL_ITERS", "99")
    snap = service.snapshot("A")
    assert snap.effective.tool_iterations == 99, "precondition: env override is live"
    assert snap.saved.tool_iterations == 7, "precondition: disk preference is 7"

    screen = SettingsScreen(snap.effective, **_bindings(service, "A"))
    app = _Host(screen)
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open_body(app, pilot)
        await _confirm_restore(pilot)
        result = body._settings_adapter.last_result
        saved_fields = {
            f for p in result.persistence if p.saved for f in p.fields
        }

    execution = _execution(service, "A")
    # Positive control: a non-env field WAS reset, so Restore genuinely ran.
    assert execution["pin_default_model"] is False
    assert "pin_default_model" in saved_fields
    # The guard: the env-owned field was NOT rewritten to the factory value.
    assert execution["tool_iterations"] == 7
    assert "tool_iterations" not in saved_fields

    after = service.snapshot("A")
    assert after.effective.tool_iterations == 99  # env still wins at runtime
    assert after.saved.tool_iterations == 7        # persisted preference intact


@pytest.mark.asyncio
async def test_restore_env_guard_is_load_bearing(tmp_path, monkeypatch):
    """Sensitivity check: with the env-awareness removed, the SAME Restore now
    rewrites tool_iterations to the factory value — proving the R1 assertion
    above is discriminating on the ``source_of`` guard, not vacuously true."""
    service = _service(tmp_path)
    monkeypatch.delenv("LM_TOOL_ITERS", raising=False)
    _seed(service, "A", tool_iterations=7, pin_default_model=True)
    monkeypatch.setenv("LM_TOOL_ITERS", "99")

    # Neutralise the guard the dialog consults (settings_screen reads
    # settings_mod.source_of): pretend nothing is env-owned.
    monkeypatch.setattr(settings_mod, "source_of", lambda name: None)

    snap = service.snapshot("A")
    screen = SettingsScreen(snap.effective, **_bindings(service, "A"))
    app = _Host(screen)
    async with app.run_test(size=(120, 45)) as pilot:
        await _open_body(app, pilot)
        await _confirm_restore(pilot)

    # Guard removed => the env-owned field IS reset to factory (48).
    assert _execution(service, "A")["tool_iterations"] == Settings().tool_iterations == 48


# ── R2: Restore never touches another conversation's file ─────────────────────

@pytest.mark.asyncio
async def test_restore_leaves_other_conversation_bytes_untouched(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.delenv("LM_TOOL_ITERS", raising=False)
    _seed(service, "A", pin_default_model=True)
    _seed(service, "B", pin_default_model=True, tool_iterations=11)

    before = _convo_file(service, "B").read_bytes()

    snap = service.snapshot("A")
    screen = SettingsScreen(snap.effective, **_bindings(service, "A"))
    app = _Host(screen)
    async with app.run_test(size=(120, 45)) as pilot:
        await _open_body(app, pilot)
        await _confirm_restore(pilot)

    # Positive control: A's own file changed (Restore ran).
    assert _execution(service, "A")["pin_default_model"] is False
    # Acceptance: B's per-conversation file is byte-for-byte identical.
    assert _convo_file(service, "B").read_bytes() == before
