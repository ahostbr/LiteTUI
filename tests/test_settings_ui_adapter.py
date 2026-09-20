"""Contract tests for the optional settings-screen persistence binding."""
from __future__ import annotations

from dataclasses import fields, replace
from types import ModuleType, SimpleNamespace

import pytest

from litetui.settings import Settings
from litetui.settings_apply import (
    PersistenceDestinationResult,
    RuntimeSettingStatus,
    SettingsSaveResult,
)
from litetui.settings_ui_adapter import (
    SettingsConflictError,
    SettingsUiAdapter,
    changes_between,
)


def _install_scope_registry(monkeypatch):
    import litetui

    module = ModuleType("litetui.settings_scope")
    module.SETTING_SPECS = {
        field.name: SimpleNamespace(
            scope=SimpleNamespace(
                value="conversation" if field.name == "default_model" else "device"
            )
        )
        for field in fields(Settings)
    }
    monkeypatch.setitem(__import__("sys").modules, "litetui.settings_scope", module)
    monkeypatch.setattr(litetui, "settings_scope", module, raising=False)


def _success(destination, fields, revision):
    return PersistenceDestinationResult(
        destination=destination,
        scope="conversation" if destination == "conversation" else "device",
        saved=True,
        revision=revision,
        fields=tuple(fields),
    )


def _failure(destination, fields, error="disk unavailable"):
    return PersistenceDestinationResult(
        destination=destination,
        scope="conversation" if destination == "conversation" else "device",
        saved=False,
        error=error,
        fields=tuple(fields),
    )


def test_changes_use_canonical_scope_registry(monkeypatch):
    _install_scope_registry(monkeypatch)
    before = Settings()
    after = replace(before, default_model="model-b", footer_order="seat,think")

    changes = {change.key: change for change in changes_between(before, after)}

    assert changes["default_model"].scope == "conversation"
    assert changes["footer_order"].scope == "device"


def test_partial_result_retains_failed_fields_and_retry_uses_new_success_revision(monkeypatch):
    _install_scope_registry(monkeypatch)
    baseline = Settings(default_model="model-a", footer_order="seat")
    snapshots = [
        SimpleNamespace(saved=baseline, effective=baseline, revisions={"global": "g2", "conversation": "c1"}),
    ]
    calls = []

    def snapshot():
        return snapshots[-1]

    def save(changes, revisions):
        calls.append((tuple(changes), dict(revisions)))
        if len(calls) == 1:
            return SettingsSaveResult(
                persistence=(
                    _success("global", ("default_model",), "g2"),
                    _failure("conversation", ("footer_order",)),
                )
            )
        return SettingsSaveResult(
            persistence=(_success("conversation", ("footer_order",), "c2"),)
        )

    adapter = SettingsUiAdapter(
        baseline,
        snapshot_provider=snapshot,
        save_patch=save,
    )
    target = replace(baseline, default_model="model-b", footer_order="think,seat")

    result = adapter.save(target)

    assert result is not None
    assert [change.key for change in adapter.pending_changes] == ["footer_order"]
    assert adapter.committed.default_model == "model-b"
    assert calls[0][1] == {"global": "g2", "conversation": "c1"}

    result = adapter.retry()

    assert result.fully_saved
    assert adapter.pending_changes == ()
    assert calls[1][1] == {"global": "g2", "conversation": "c1"}


def test_retry_refuses_external_destination_change(monkeypatch):
    _install_scope_registry(monkeypatch)
    baseline = Settings(default_model="model-a")
    snapshots = [
            SimpleNamespace(saved=baseline, effective=baseline, revisions={"global": "g1", "conversation": "c1"}),
            SimpleNamespace(saved=baseline, effective=baseline, revisions={"global": "g1", "conversation": "c2"}),
    ]
    calls = []

    def snapshot():
        return snapshots.pop(0)

    def save(changes, revisions):
        calls.append((changes, revisions))
        return SettingsSaveResult(
                persistence=(_failure("conversation", ("default_model",)),)
        )

    adapter = SettingsUiAdapter(
        baseline,
        snapshot_provider=snapshot,
        save_patch=save,
    )
    adapter.save(replace(baseline, default_model="model-b"))

    with pytest.raises(SettingsConflictError, match="conversation"):
        adapter.retry()

    assert len(calls) == 1


def test_runtime_failure_keeps_result_but_does_not_retry_saved_disk_fields(monkeypatch):
    _install_scope_registry(monkeypatch)
    baseline = Settings(footer_order="seat")
    calls = []

    def save(changes, revisions):
        calls.append(tuple(changes))
        return SettingsSaveResult(
            persistence=(_success("global", ("footer_order",), "g2"),)
        )

    def runtime(_target, _result):
        raise OSError("reconnect unavailable")

    adapter = SettingsUiAdapter(
        baseline,
        snapshot_provider=lambda: SimpleNamespace(
            saved=baseline,
            effective=baseline,
            revisions={"global": "g1", "conversation": "c1"},
        ),
        save_patch=save,
        runtime_apply=runtime,
    )

    result = adapter.save(replace(baseline, footer_order="think"))

    assert result is not None
    assert result.has_runtime_failures
    assert adapter.pending_changes == ()
    assert len(calls) == 1


def test_restore_forces_saved_value_when_effective_value_already_matches_factory(monkeypatch):
    _install_scope_registry(monkeypatch)
    factory = Settings()
    saved = replace(factory, footer_order="saved-order")
    effective = replace(saved, footer_order=factory.footer_order)
    calls = []

    def save(changes, revisions):
        calls.append(tuple(changes))
        return SettingsSaveResult(
            persistence=(_success("global", ("footer_order",), "g2"),)
        )

    adapter = SettingsUiAdapter(
        effective,
        snapshot_provider=lambda: SimpleNamespace(
            saved=saved,
            effective=effective,
            revisions={"global": "g1", "conversation": "c1"},
        ),
        save_patch=save,
    )

    adapter.save(factory, force_fields=("footer_order",))

    assert [change.key for change in calls[0]] == ["footer_order"]
    assert calls[0][0].value == factory.footer_order


def test_path_destination_updates_logical_revision_and_pending_runtime_effective(monkeypatch):
    _install_scope_registry(monkeypatch)
    baseline = Settings(footer_order="seat")
    calls = []

    def save(changes, revisions):
        calls.append((tuple(changes), dict(revisions)))
        if len(calls) == 1:
            return SettingsSaveResult(
                persistence=(
                    PersistenceDestinationResult(
                        destination="C:/settings/settings.json",
                        scope="device",
                        saved=True,
                        revision="g2",
                        fields=("footer_order",),
                    ),
                ),
                runtime=(
                    RuntimeSettingStatus(
                        field="footer_order",
                        scope="device",
                        status="pending",
                        action="restart",
                        requested="think",
                        effective="seat",
                    ),
                ),
            )
        return SettingsSaveResult(
            persistence=(
                PersistenceDestinationResult(
                    destination="C:/settings/settings.json",
                    scope="device",
                    saved=True,
                    revision="g3",
                    fields=("theme_name",),
                ),
            )
        )

    adapter = SettingsUiAdapter(
        baseline,
        snapshot_provider=lambda: SimpleNamespace(
            saved=baseline,
            effective=baseline,
            revisions={"global": "g1", "conversation": "c1"},
        ),
        save_patch=save,
    )
    first_target = replace(baseline, footer_order="think")
    adapter.save(first_target)

    assert adapter.committed.footer_order == "think"
    assert adapter.effective.footer_order == "seat"

    repeat = adapter.save(first_target)
    assert repeat.has_pending_runtime
    assert repeat.runtime[0].field == "footer_order"

    adapter.save(replace(first_target, theme_name="monokai"))

    assert [change.key for change in calls[1][0]] == ["theme_name"]
    assert calls[1][1] == {"global": "g2", "conversation": "c1"}
