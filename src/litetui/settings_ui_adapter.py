"""UI-side adapter for the scoped settings persistence contract.

The settings screen remains usable without a service binding.  When a host
supplies the optional callbacks below, this module translates a ``Settings``
draft into scoped changes, retains partial save outcomes, and refuses a retry
that would silently overwrite a newer destination revision.

The service owns the concrete snapshot/change classes.  The UI only relies on
their small structural contract, which keeps this module importable before a
host has wired the service in.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, fields, replace
from importlib import import_module
from typing import Any, Protocol, cast, runtime_checkable

from litetui.settings import Settings
from litetui.settings_apply import (
    RuntimeSettingStatus,
    SettingsSaveResult,
    SettingsScope,
)


@dataclass(frozen=True)
class SettingChange:
    """Structural equivalent of the service's scoped change value."""

    key: str
    value: object
    scope: SettingsScope


@runtime_checkable
class SettingsSnapshotLike(Protocol):
    saved: Settings
    effective: Settings
    revisions: Mapping[str, str]


SnapshotProvider = Callable[[], SettingsSnapshotLike]
SavePatch = Callable[[Sequence[SettingChange], Mapping[str, str]], SettingsSaveResult]
RuntimeApply = Callable[[Settings, SettingsSaveResult], SettingsSaveResult]


class SettingsConflictError(RuntimeError):
    """A retry would overwrite a destination changed since the last attempt."""

    def __init__(self, destinations: Sequence[str]):
        self.destinations = tuple(dict.fromkeys(destinations))
        joined = ", ".join(self.destinations)
        super().__init__(f"settings changed externally; reload before retry ({joined})")


def _scope_registry() -> Mapping[str, Any]:
    """Load the canonical scope registry only when a service binding is used."""

    try:
        module = import_module("litetui.settings_scope")
    except ModuleNotFoundError as exc:  # pragma: no cover - pre-service fallback
        raise RuntimeError(
            "scoped settings binding requires litetui.settings_scope"
        ) from exc
    return module.SETTING_SPECS


def changes_between(source: Settings, target: Settings) -> tuple[SettingChange, ...]:
    """Build changes using the service-owned scope registry, never UI metadata."""

    registry = _scope_registry()
    changes: list[SettingChange] = []
    for field in fields(Settings):
        before = getattr(source, field.name)
        after = getattr(target, field.name)
        if before == after:
            continue
        spec = registry.get(field.name)
        if spec is None:
            raise ValueError(f"missing persistence scope for {field.name}")
        scope = cast(SettingsScope, getattr(spec.scope, "value", spec.scope))
        changes.append(SettingChange(field.name, deepcopy(after), scope))
    return tuple(changes)


def _destinations_for(changes: Sequence[SettingChange]) -> set[str]:
    """Map a change list to service result destination names."""

    registry = _scope_registry()
    return {
        "conversation" if getattr(registry[c.key].scope, "value", registry[c.key].scope) == "conversation" else "global"
        for c in changes
    }


def _result_fields(result: SettingsSaveResult, *, saved: bool) -> set[str]:
    fields_out: set[str] = set()
    for item in result.persistence:
        if item.saved is saved:
            fields_out.update(item.fields)
    return fields_out


class SettingsUiAdapter:
    """Stateful bridge between a settings draft and host-owned persistence."""

    def __init__(
        self,
        current: Settings,
        *,
        snapshot_provider: SnapshotProvider | None = None,
        save_patch: SavePatch | None = None,
        runtime_apply: RuntimeApply | None = None,
    ) -> None:
        if (snapshot_provider is None) != (save_patch is None):
            raise ValueError("snapshot_provider and save_patch must be supplied together")
        self.enabled = snapshot_provider is not None
        self._snapshot_provider = snapshot_provider
        self._save_patch = save_patch
        self._runtime_apply = runtime_apply
        self._baseline = snapshot_provider() if snapshot_provider else None
        self._effective = deepcopy(
            self._baseline.effective if self._baseline is not None else current
        )
        self._committed = deepcopy(
            self._baseline.saved if self._baseline is not None else current
        )
        self._expected_revisions = dict(self._baseline.revisions) if self._baseline else {}
        self._pending: dict[str, SettingChange] = {}
        self.last_result: SettingsSaveResult | None = None
        self.last_conflict: tuple[str, ...] = ()

    @property
    def pending_changes(self) -> tuple[SettingChange, ...]:
        return tuple(self._pending.values())

    @property
    def committed(self) -> Settings:
        return deepcopy(self._committed)

    def _set_pending(self, changes: Sequence[SettingChange]) -> None:
        self._pending = {change.key: change for change in changes}

    def _apply_persistence_result(
        self, result: SettingsSaveResult, target: Settings
    ) -> None:
        saved_fields = _result_fields(result, saved=True)
        failed_fields = _result_fields(result, saved=False)
        for change in tuple(self._pending.values()):
            if change.key in saved_fields:
                setattr(self._committed, change.key, deepcopy(change.value))
                setattr(self._effective, change.key, deepcopy(change.value))
                self._pending.pop(change.key, None)
        # A result can omit fields when a host reports a coarse destination;
        # retain those fields rather than pretending they were committed.
        self.last_conflict = ()
        for item in result.persistence:
            if item.saved and item.revision is not None:
                self._expected_revisions[item.destination] = item.revision
        if failed_fields:
            self.last_conflict = ()

    def save(
        self, target: Settings, *, force_fields: Sequence[str] = ()
    ) -> SettingsSaveResult | None:
        """Persist the target, or return ``None`` for the legacy unbound path."""

        if not self.enabled:
            return None
        assert self._save_patch is not None
        # Normal edits compare against the effective snapshot so environment
        # overrides do not become accidental writes. Restore is different: it
        # explicitly forces dialog fields back to factory values, even when an
        # environment override makes the effective value look unchanged.
        initial_changes = changes_between(self._effective, target)
        forced_names = set(force_fields)
        forced = {
            change.key: change
            for change in changes_between(self._committed, target)
            if change.key in forced_names
        }
        merged = {change.key: change for change in initial_changes}
        merged.update(forced)
        changes = tuple(merged.values())
        self._set_pending(changes)
        if not changes:
            self.last_result = SettingsSaveResult()
            return self.last_result
        result = self._save_patch(tuple(changes), dict(self._expected_revisions))
        result = self._apply_runtime(target, result)
        self.last_result = result
        self._apply_persistence_result(result, target)
        return result

    def retry(self) -> SettingsSaveResult:
        """Retry only pending fields after checking destination revisions."""

        if not self.enabled or self._save_patch is None or self._snapshot_provider is None:
            raise RuntimeError("settings retry requires a bound persistence service")
        if not self._pending:
            self.last_result = SettingsSaveResult()
            return self.last_result
        fresh = self._snapshot_provider()
        destinations = _destinations_for(tuple(self._pending.values()))
        conflicts = tuple(
            destination
            for destination in sorted(destinations)
            if fresh.revisions.get(destination) != self._expected_revisions.get(destination)
        )
        if conflicts:
            self.last_conflict = conflicts
            raise SettingsConflictError(conflicts)
        self._expected_revisions = dict(fresh.revisions)
        target = self._target_from_pending()
        result = self._save_patch(tuple(self._pending.values()), dict(self._expected_revisions))
        result = self._apply_runtime(target, result)
        self.last_result = result
        self._apply_persistence_result(result, target)
        return result

    def _apply_runtime(
        self, target: Settings, result: SettingsSaveResult
    ) -> SettingsSaveResult:
        if self._runtime_apply is None:
            return result
        try:
            return self._runtime_apply(target, result)
        except Exception as exc:  # noqa: BLE001 - keep persistence evidence
            statuses = tuple(
                RuntimeSettingStatus(
                    field=change.key,
                    scope=cast(SettingsScope, change.scope),
                    status="failed",
                    requested=deepcopy(change.value),
                    reason=f"runtime apply failed: {type(exc).__name__}: {exc}",
                )
                for change in self._pending.values()
            )
            return replace(result, runtime=result.runtime + statuses)

    def _target_from_pending(self) -> Settings:
        target = self.committed
        for change in self._pending.values():
            setattr(target, change.key, deepcopy(change.value))
        return target


def persistence_error(result: SettingsSaveResult) -> str | None:
    """Small UI-safe summary for a partial or failed persistence result."""

    errors = [
        f"{item.destination}: {item.error or 'save failed'}"
        for item in result.persistence
        if not item.saved
    ]
    return "; ".join(errors) if errors else None
