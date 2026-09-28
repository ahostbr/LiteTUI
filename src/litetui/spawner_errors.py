"""Best-effort, rate-limited error mail from a spawned seat to its spawner."""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock

WINDOW_SECONDS = 300
MAX_ERROR_CHARS = 1200
MAX_MAILS_PER_WINDOW = 5


class SpawnerErrorReporter:
    def __init__(self):
        self._sent: dict[tuple[str, str], float] = {}
        self._window_start: dict[str, float] = {}
        self._count: dict[str, int] = {}
        self._suppressed: dict[str, int] = {}
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
        key = (seat.agent_id, text)
        moment = time.monotonic() if now is None else now
        with self._lock:
            start = self._window_start.get(seat.agent_id, moment)
            if moment - start >= WINDOW_SECONDS:
                suppressed = self._suppressed.get(seat.agent_id, 0)
                if suppressed:
                    summary = (f"[LiteTUI error] {seat.name}: {suppressed} more errors suppressed "
                               f"in the last 5 min\nseat: {seat.agent_id}\nconvo: {convo_id or 'none'}")
                    if not seat.send(spawner, summary):
                        return False
                self._window_start[seat.agent_id] = moment
                self._count[seat.agent_id] = 0
                self._suppressed[seat.agent_id] = 0
                self._sent = {k: v for k, v in self._sent.items() if k[0] != seat.agent_id}
            else:
                self._window_start.setdefault(seat.agent_id, moment)
            if moment - self._sent.get(key, float("-inf")) < WINDOW_SECONDS:
                return False
            if self._count.get(seat.agent_id, 0) >= MAX_MAILS_PER_WINDOW:
                self._suppressed[seat.agent_id] = self._suppressed.get(seat.agent_id, 0) + 1
                return False
            title = text.splitlines()[0][:180]
            body = (f"[LiteTUI error] {seat.name}: {activity[:60]} failed: {title}\n"
                    f"seat: {seat.agent_id}\nconvo: {convo_id or 'none'}\nerror: {text}")
            if not seat.send(spawner, body):
                return False
            self._sent[key] = moment
            self._count[seat.agent_id] = self._count.get(seat.agent_id, 0) + 1
            return True
