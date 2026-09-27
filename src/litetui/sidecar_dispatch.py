"""Thread-safe bridge for allowlisted native events into the Textual owner."""
from __future__ import annotations

from collections.abc import Callable

from litetui.sidecar_patch import apply_patch


class SettingsPatchDispatcher:
    def __init__(self, app, owner, *, apply: Callable = apply_patch, snapshot: Callable | None = None,
                 create_job: Callable | None = None):
        self.app = app
        self.owner = owner
        self.apply = apply
        self.snapshot = snapshot
        self.create_job = create_job  # T1082 R4: job_create, run on the Textual thread
        # One owner outlives many windows, and every child numbers its events
        # from the same first id; the per-spawn token tells their events apart.
        self._seen: set[tuple[str, int]] = set()

    def __call__(self, frame: dict) -> None:
        request_id = frame["id"]
        key = (self.owner.token, request_id)
        if key in self._seen:
            self.owner.on_rejected_frame("duplicate_event_id")
            return
        self._seen.add(key)
        try:
            # Worker reader never writes app settings or calls service directly.
            if frame["command"] == "settings_request":
                if self.snapshot is None:
                    raise RuntimeError("Settings are not available to this window")
                result = self.app.call_from_thread(self.snapshot, self.app)
            elif frame["command"] == "job_create":
                if self.create_job is None:
                    raise RuntimeError("Job creation is not available to this window")
                result = self.app.call_from_thread(self.create_job, self.app, frame["payload"])
            else:
                result = self.app.call_from_thread(self.apply, self.app, frame["payload"])
        except (ValueError, RuntimeError, OSError) as exc:
            result = {"saved": False, "error": str(exc)}
        self.owner.send_event_reply(request_id, result)
