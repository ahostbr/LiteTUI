"""Immutable-boundary draft handling for the settings UI.

The draft is intentionally independent of Textual and persistence. It deep
copies nested lists/maps so closing a dialog can never mutate live settings.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from typing import Any

from litetui.settings import Settings


class SettingsDraft:
    """A source snapshot plus an isolated working copy."""

    def __init__(self, source: Settings):
        self.source = deepcopy(source)
        self.working = deepcopy(source)

    def dirty_fields(self) -> tuple[str, ...]:
        return tuple(
            field.name
            for field in fields(Settings)
            if getattr(self.source, field.name) != getattr(self.working, field.name)
        )

    @property
    def dirty(self) -> bool:
        return bool(self.dirty_fields())

    def update(self, field_name: str, value: Any) -> None:
        if not hasattr(self.working, field_name):
            raise AttributeError(field_name)
        setattr(self.working, field_name, deepcopy(value))

    def replace_working(self, value: Settings) -> None:
        self.working = deepcopy(value)

    def snapshot(self) -> Settings:
        return deepcopy(self.working)
