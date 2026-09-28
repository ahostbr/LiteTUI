"""Best-effort, rate-limited error mail from a spawned seat to its spawner."""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock

WINDOW_SECONDS = 300
MAX_ERROR_CHARS = 1200


class SpawnerErrorReporter:
    def __init__(self):
        self._sent: dict[tuple[str, str], float] = {}
        self._lock = Lock()

    def send(self, seat, convo_id: str | None, error: str, activity: str,
             *, root: Path | None = None, now: float | None = None) -> bool:
        """Presence is the authority; no spawned_by on disk means no mail."""
        try:
            return self._send(seat, convo_id, error, activity, root=root, now=now)
        except Exception:
            # Error reporting cannot generate another UI error (or a report loop).
            return False

    def _send(self, seat, convo_id, error, activity, *, root, now):
        if not getattr(seat, "agent_id", None):
            return False
        path = (root or Path.home() / ".liteharness") / "agents" / f"{seat.agent_id}.json"
        try:
            presence = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(presence, dict) or presence.get("agent_id") != seat.agent_id:
            return False
        spawner = presence.get("spawned_by")
        if not isinstance(spawner, str) or not spawner.strip() or spawner == seat.agent_id:
            return False
        text = str(error).strip()[:MAX_ERROR_CHARS]
        key = (spawner, text)
        moment = time.monotonic() if now is None else now
        with self._lock:
            if moment - self._sent.get(key, float("-inf")) < WINDOW_SECONDS:
                return False
            self._sent[key] = moment
        body = (f"[LiteTUI error] seat: {seat.name} ({seat.agent_id})\n"
                f"doing: {activity[:120]}\nconvo: {convo_id or 'none'}\nerror: {text}")
        if seat.send(spawner, body):
            return True
        with self._lock:
            self._sent.pop(key, None)
        return False
