"""The three pinned rows of the compact seat, and its authoritative card lookup."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from rich.cells import cell_len, set_cell_size
from textual.widgets import Static

from litetui.fmt import fmt_dur


class NowCard(Static):
    DEFAULT_CSS = """
    NowCard { display: none; height: 3; background: $surface-darken-2; text-wrap: nowrap; }
    """

    def __init__(self, **kwargs):
        # Task titles and command summaries are literal data, never Textual markup.
        super().__init__("", markup=False, **kwargs)
        self.card_label = "no card claimed"
        self.last = "—"
        self._waits: dict[object, tuple[str, str, float]] = {}
        self.wait: tuple[str, str, float] | None = None
        self.idle_since: float | None = time.time()
        self._read_at = 0.0
        self._agent_id: str | None = None

    def read_task(self, agent_id: str | None) -> None:
        if not agent_id:
            self._agent_id = None
            self.card_label = "no card claimed"
            return
        if agent_id == self._agent_id and time.monotonic() - self._read_at < 30:
            return
        previous = self.card_label if agent_id == self._agent_id else "no card claimed"
        self._agent_id = agent_id
        self._read_at = time.monotonic()
        path = Path.home() / ".litesuite" / "harness" / "tasks.db"
        if not path.exists():
            self.card_label = "no card claimed"
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

    def repaint(self, *, agent_id=None, tool=None, thinking=None, elapsed=None, eta=None) -> None:
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
            title = f"{self.card_label} \u00b7 idle since {time.strftime('%H:%M', time.localtime(self.idle_since))}"
        if tool is not None:
            step = f"now  {tool.tool_name}  {int(time.monotonic() - tool._t0)}s"
        elif thinking is not None:
            step = f"now  thinking  {int(time.monotonic() - (thinking._t0 or time.monotonic()))}s"
        elif elapsed is not None:
            step = f"now  responding  {int(time.monotonic() - elapsed)}s"
            if eta is not None and eta > 0:
                step += f"  · est ~{fmt_dur(eta)}"
        else:
            step = "now  idle"
        width = max(1, self.content_region.width or self.size.width or 46)

        def fit(line: str) -> str:
            line = " ".join(str(line).split())
            return line if cell_len(line) <= width else set_cell_size(line, width - 1).rstrip() + "…"

        if self.wait:
            owner, reason, since = self.wait
            delta = int(time.monotonic() - since)
            clock = f"{delta // 60}:{delta % 60:02}"
            # Keep the time at the right edge even if a long owner drops reason.
            prefix = f"⏳ WAITING ON {owner} · {reason}"
            if cell_len(prefix) + cell_len(clock) + 3 > width:
                prefix = f"⏳ WAITING ON {owner}"
            budget = max(0, width - cell_len(clock) - 3)
            if cell_len(prefix) > budget:
                prefix = set_cell_size(prefix, max(0, budget - 1)).rstrip() + "…"
            title = f"{prefix} · {clock}"
        self.content = "\n".join((fit(title), fit(step), fit(f"last {self.last}")))

    def begin_wait(self, owner: str, reason: str) -> object:
        token = object()
        self._waits[token] = (owner, reason, time.monotonic())
        self.wait = self._waits[token]
        self.repaint(agent_id=self._agent_id)
        return token

    def end_wait(self, token: object | None) -> None:
        if token is None or token not in self._waits:
            return
        del self._waits[token]
        self.wait = next(reversed(self._waits.values())) if self._waits else None
        self.repaint(agent_id=self._agent_id)
