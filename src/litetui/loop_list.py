"""The `/loop` list, as a panel. T074.

WHAT ALREADY EXISTED, AND IS NOT REBUILT HERE. `/loop` has listed, created,
paused, resumed and removed loops as TEXT since goal_loop.py was written. This
adds a surface over the same verbs; it adds no behaviour of its own.

🔴 THE PANEL CALLS `set_loop_enabled` AND `remove_loop`, NEVER ITS OWN WRITES.
Resuming a loop is not just `enabled = True` — it re-arms `next_run_at`, or a
loop resumed after a long pause fires IMMEDIATELY instead of at its cadence. A
panel that flipped the flag itself would look correct, save correctly, and
change the behaviour. Those two functions were extracted from `loop_command`
for exactly this reason, so the command and the panel cannot drift.

📌 LOOPS ARE ABSENT FROM THE CALENDAR BY DECISION, and this does not override
it. `calendar_view.py:125` — "fixed loops live in /loop, not on calendar
dates" — because a fixed-cadence loop has no date to sit on: it fires every N
minutes from whenever it was last armed. This panel is the surface that
decision implies, not a reversal of it.

`/loop list` still prints text. Only the BARE `/loop` opens this, so anything
reading the text form keeps working.
"""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Label, Static, Switch

from litetui.goal_loop import _loop_jobs, remove_loop, set_loop_enabled
from litetui.side_panel import SwapButton, close_dialog


def loop_countdown(job, now=None) -> str:
    from datetime import datetime
    import math
    if not job.enabled:
        return 'Paused'
    try:
        due = datetime.fromisoformat(job.next_run_at or '')
        now = now or datetime.now(due.tzinfo)
        seconds = max(0, math.ceil((due - now).total_seconds()))
    except (ValueError, TypeError, AttributeError):
        return 'Not scheduled'
    if seconds == 0:
        return 'Due'
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    clock = f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'
    return f'Next in {clock}'


def loop_rows(app) -> list[dict]:
    """One dict per loop, from the LIVE job list.

    A function over `app.jobs`, not a snapshot handed in at construction: a
    loop added or fired while the panel is open must not leave it describing a
    world that has moved.
    """
    rows = []
    for job in _loop_jobs(app):
        rows.append(
            {
                "id": job.id,
                "enabled": bool(job.enabled),
                "countdown": loop_countdown(job),
                "every": f"every {job.interval_minutes}m",
                "prompt": (job.prompt or "").strip(),
                "runs": int(getattr(job, "run_count", 0) or 0),
            }
        )
    return rows


class LoopListBody(Widget):
    """A dialog body that works in either host. No ModalScreen assumptions."""

    DEFAULT_CSS = """
    LoopListBody { height: auto; layout: vertical; }
    LoopListBody #ll-title { text-style: bold; padding: 0 0 1 0; }
    LoopListBody #ll-note { color: $text-muted; padding: 0 0 1 0; }
    LoopListBody #ll-scroll { height: 16; padding: 0 1; }
    LoopListBody .ll-head { height: auto; }
    LoopListBody .ll-when { width: 1fr; padding: 1 0 0 1; }
    LoopListBody .ll-prompt { color: $text-muted; padding: 0 0 1 3; }
    LoopListBody .ll-next { height: 1; padding: 0 0 0 3; color: $accent; }
    LoopListBody .ll-kill { width: 10; }
    LoopListBody #ll-buttons { height: auto; align: center middle; padding: 1 0 0 0; }
    LoopListBody #ll-buttons Button { margin: 0 1 0 0; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._scroll_y = 0

    def compose(self) -> ComposeResult:
        rows = loop_rows(self.app)
        yield Static(f"Loops ({len(rows)})", id="ll-title")
        if not rows:
            # The empty state says how to LEAVE it. A panel that only reports
            # nothing teaches you nothing.
            yield Static(
                "No loops running. `/loop 15m <prompt>` starts one — it then "
                "sends that prompt on its own every 15 minutes.",
                id="ll-note",
            )
        else:
            yield Static(
                "The switch pauses and resumes. Resuming re-arms the timer, so "
                "a resumed loop waits its full interval rather than firing at "
                "once. Changes apply immediately.",
                id="ll-note",
            )
        with VerticalScroll(id="ll-scroll"):
            for row in rows:
                with Horizontal(classes="ll-head"):
                    yield Switch(value=row["enabled"], id=f"ll-on-{row['id']}")
                    ran = f" · {row['runs']} run{'s' if row['runs'] != 1 else ''}"
                    yield Label(
                        f"{row['id']}  {row['every']}{ran if row['runs'] else ''}",
                        classes="ll-when",
                    )
                    yield Button("Remove", id=f"ll-rm-{row['id']}", classes="ll-kill")
                yield Static(row["countdown"], id=f"ll-next-{row['id']}", classes="ll-next")
                yield Static(row["prompt"] or "(no prompt)",
                             classes="ll-prompt", markup=False)
        with Horizontal(id="ll-buttons"):
            yield Button("Close", variant="primary", id="ll-close")
            yield SwapButton(classes="inline")

    # ── the swap contract ────────────────────────────────────────────────

    def get_state(self) -> dict:
        """Only the scroll position. Every switch and every removal is already
        written to disk, so a rebuilt body reads the same truth from the same
        place — there is no pending edit here to lose."""
        try:
            return {"scroll_y": self.query_one("#ll-scroll", VerticalScroll).scroll_offset.y}
        except Exception:
            return {}

    def set_state(self, state: dict) -> None:
        self._scroll_y = int(state.get("scroll_y") or 0)

    def on_mount(self) -> None:
        self.set_interval(1, self._refresh_countdowns)
        if self._scroll_y:
            try:
                self.query_one("#ll-scroll", VerticalScroll).scroll_to(
                    y=self._scroll_y, animate=False
                )
            except Exception:
                pass

    def _refresh_countdowns(self) -> None:
        for widget in self.query(".ll-next"):
            job = self._job(widget.id[len("ll-next-"):])
            widget.update(loop_countdown(job) if job is not None else "Removed")

    # ── the writes, both delegated ───────────────────────────────────────

    def _job(self, job_id: str):
        for job in _loop_jobs(self.app):
            if job.id == job_id:
                return job
        return None

    @on(Switch.Changed)
    def _toggle(self, event: Switch.Changed) -> None:
        sid = event.switch.id or ""
        if not sid.startswith("ll-on-"):
            return
        job = self._job(sid[len("ll-on-"):])
        if job is None or bool(job.enabled) == bool(event.value):
            return
        # THE SHARED VERB. Not `job.enabled = ...` — see the module docstring.
        set_loop_enabled(self.app, job, bool(event.value))
        self._refresh_countdowns()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if not bid.startswith("ll-rm-"):
            return
        job = self._job(bid[len("ll-rm-"):])
        if job is not None:
            remove_loop(self.app, job)
            # Rebuild from the live list rather than removing the row widgets:
            # the title count and the empty state both have to follow, and
            # pruning widgets by hand is how one of them gets forgotten.
            self.refresh(recompose=True)

    @on(Button.Pressed, "#ll-close")
    def _close(self) -> None:
        close_dialog(self, None)
