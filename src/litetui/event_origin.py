"""Immutable producer attribution, separate from admission and delivery success.

Only explicitly participating producers supply this metadata. Unknown/malformed
IDs remain null rather than borrowing ambient state or rejecting legacy commands.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class EventOrigin:
    session_id: str | None = None
    operation_id: str | int | None = None
    hook_id: str | None = None
    source: str | None = None
    execution_id: str | None = None

    def __post_init__(self):
        # Frozen dataclasses alone do not freeze caller-owned lists/dicts. Keep
        # only immutable protocol scalars; never stringify an unknown identity.
        if type(self.operation_id) not in (str, int):
            object.__setattr__(self, "operation_id", None)
        for name in ("session_id", "hook_id", "source", "execution_id"):
            if type(getattr(self, name)) is not str:
                object.__setattr__(self, name, None)

    def scheduled(self) -> EventOrigin:
        """One fresh invocation, even when a caller supplies an older token."""
        return replace(self, execution_id=str(uuid.uuid4()))

    def envelope_fields(self) -> dict:
        return {
            "session_id": self.session_id,
            "operation_id": self.operation_id,
            "hook_id": self.hook_id,
            "source": self.source,
            "execution_id": self.execution_id,
        }
