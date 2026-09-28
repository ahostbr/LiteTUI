"""The three pinned rows of the compact seat, and its authoritative card lookup."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from textual.widgets import Static


class NowCard(Static):
    DEFAULT_CSS = """
    NowCard { display: none; height: 3; background: $surface-darken-2; }
    .compact-profile NowCard { display: block; }
    """

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self.card_label = "no card claimed"
        self.last = "—"
        self.wait: tuple[str, str, float] | None = None
        self.idle_since: float | None = time.time()
        self._read_at = 0.0

    def read_task(self, agent_id: str | None) -> None:
        if not agent_id or time.monotonic() - self._read_at < 30:
            return
        self._read_at = time.monotonic()
        previous = self.card_label
        path = Path.home() / ".litesuite" / "harness" / "tasks.db"
        if not path.exists():
            return
        try:
            # URI mode=ro keeps the UI from creating/locking the board. SQLite
            # timeout is zero: on a busy WAL, display the previous truth later.
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0) as db:
                row = db.execute("SELECT id, title FROM tasks WHERE assignee=? "
                                 "AND status IN ('thinking','building','fixing') "
                                 "ORDER BY claimed_at DESC LIMIT 1", (agent_id,)).fetchone()
            self.card_label = f"{row[0]} {row[1]}" if row else "no card claimed"
        except (sqlite3.Error, OSError):
            self.card_label = previous

    def repaint(self, *, agent_id=None, tool=None, thinking=None, elapsed=None) -> None:
        self.read_task(agent_id)
        if self.wait:
            owner, reason, since = self.wait
            delta = int(time.monotonic() - since)
            title = f"\u23f3 WAITING ON {owner} \u00b7 {reason} \u00b7 {delta // 60}:{delta % 60:02}"
        elif tool is not None or thinking is not None or elapsed is not None:
            self.idle_since = None
            title = self.card_label
        else:
            if self.idle_since is None:
                self.idle_since = time.time()
            title = f"idle since {time.strftime('%H:%M', time.localtime(self.idle_since))} \u00b7 last: done"
        if tool is not None:
            step = f"now  {tool.tool_name}  {int(time.monotonic() - tool._t0)}s"
        elif thinking is not None:
            step = f"now  thinking  {int(time.monotonic() - (thinking._t0 or time.monotonic()))}s"
        elif elapsed is not None:
            step = f"now  responding  {int(time.monotonic() - elapsed)}s"
        else:
            step = "now  idle"
        self.content = f"{title}\n{step}\nlast {self.last}"

    def begin_wait(self, owner: str, reason: str) -> None:
        self.wait = (owner, reason, time.monotonic())
        self.repaint()

    def end_wait(self) -> None:
        self.wait = None
        self.repaint()
