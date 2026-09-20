"""Reviewable settings-save result contract.

No storage or runtime application happens here. Astra owns the eventual
ConvoSettings/global adapter; this module only gives the UI a precise result
shape that cannot claim one global bool covers multiple destinations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

RuntimeState = Literal["applied", "pending", "failed"]
SaveAction = Literal["none", "retry", "reconnect", "reload", "restart"]
SettingsScope = Literal["conversation", "defaults", "device", "app", "mixed"]


@dataclass(frozen=True)
class PersistenceDestinationResult:
    destination: str
    scope: SettingsScope
    saved: bool
    error: str | None = None
    revision: str | None = None
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeSettingStatus:
    field: str
    scope: SettingsScope
    status: RuntimeState
    action: SaveAction = "none"
    requested: Any = None
    effective: Any = None
    reason: str | None = None


@dataclass(frozen=True)
class SettingsSaveResult:
    persistence: tuple[PersistenceDestinationResult, ...] = ()
    runtime: tuple[RuntimeSettingStatus, ...] = ()

    @property
    def fully_saved(self) -> bool:
        """True only when every reported destination saved successfully."""
        return bool(self.persistence) and all(item.saved for item in self.persistence)

    @property
    def has_pending_runtime(self) -> bool:
        return any(item.status == "pending" for item in self.runtime)

    @property
    def has_runtime_failures(self) -> bool:
        return any(item.status == "failed" for item in self.runtime)
