"""Thread-safe bridge for allowlisted native events into the Textual owner."""
from __future__ import annotations

from collections.abc import Callable

from litetui.sidecar_patch import apply_patch


class SettingsPatchDispatcher:
    def __init__(self, app, owner, *, apply: Callable = apply_patch):
        self.app = app
        self.owner = owner
        self.apply = apply
        self._seen: set[int] = set()

    def __call__(self, frame: dict) -> None:
        request_id = frame["id"]
        if request_id in self._seen:
            self.owner.on_rejected_frame("duplicate_event_id")
            return
        self._seen.add(request_id)
        try:
            # Worker reader never writes app settings or calls service directly.
            result = self.app.call_from_thread(self.apply, self.app, frame["payload"])
        except (ValueError, RuntimeError, OSError) as exc:
            result = {"saved": False, "error": str(exc)}
        self.owner.send_event_reply(request_id, result)
