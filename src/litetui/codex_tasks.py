"""Live provider-owned operation rows, never host-process ownership records."""

import hashlib
import json
import time
from dataclasses import dataclass


@dataclass
class NativeActivity:
    id: str
    thread_id: str
    turn_id: str
    item_id: str
    convo_id: str | None
    label: str
    started: float
    clock: float
    tool: str = "Codex activity"
    state: str = "running"
    tokens: int | None = None

    @property
    def seconds(self):
        return max(0, time.monotonic() - self.clock)


def update(app, event):
    if getattr(app, "_native_activity_disabled", False):
        return
    kind = event.get("type")
    if kind not in ("tool_call", "tool_result"):
        return
    identity = tuple(event.get(key) for key in ("threadId", "turnId", "id"))
    if not all(isinstance(value, str) and value for value in identity):
        return
    registry = getattr(app, "codex_native_activity", None)
    if registry is None:
        registry = app.codex_native_activity = {}
    if kind == "tool_result":
        registry.pop(identity, None)
    elif identity not in registry:
        digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]
        label = str(event.get("name") or "operation")
        if event.get("args") is not None:
            label += "\n" + json.dumps(event["args"], ensure_ascii=False)[:2000]
        registry[identity] = NativeActivity(
            "codex-" + digest, *identity, getattr(app, "convo_id", None),
            label, time.time(), time.monotonic())
    refresh = getattr(app, "_refresh_ctx_label", None)
    if refresh:
        refresh()
