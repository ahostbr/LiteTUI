"""Cron jobs for LiteTUI: a prompt that arrives on its own.

Input -> do work -> output. A scheduled job is just an INPUT that nobody typed.
Everything downstream of it is the ordinary turn machinery, unchanged.

This module is deliberately pure -- no Textual, no app, no I/O beyond the job
file. That is what lets the whole of it be tested without a running terminal,
which is where every interesting bug in scheduling lives (boundaries, catch-up,
double-fire) and where a UI test would tell you nothing.

WHY A JOB IS ISOMORPHIC TO INBOX MAIL
-------------------------------------
Both are text that arrives without the human pressing Enter, and both must
become a real user turn WITHOUT cancelling work already in flight. app.py
already solved that for mail (`_deliver_inbox` holds mid-turn and flushes
after), so a job delivers down the same path instead of growing a second one.
The failure that discipline prevents is not theoretical: `_stream` and
`_compact` share `@work(exclusive=True, group="chat")`, so anything that starts
a turn from inside a turn CANCELS THE CALLER.

WHAT THIS DOES NOT DO
---------------------
Jobs fire only while LiteTUI is running. There is no OS-level registration, so
a job due at 03:00 with the app closed does not wake the machine. The store is
plain JSON with an explicit schema so a headless runner could consume it later
without a migration, but that runner does not exist yet -- see `due()` on how
missed occurrences are handled meanwhile.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from tool_policy import SCHEDULED

#: How often the app polls. Cron resolves to the minute, so anything under 60s
#: is enough; 20s keeps a job's fire within a third of a minute of its slot
#: without spinning.
TICK_SECONDS = 20

#: A job that has not fired for this long past its slot is STALE and skipped
#: rather than fired late. Sixty seconds = the width of one cron minute: fire
#: in the right minute or not at all.
LATE_TOLERANCE = timedelta(seconds=90)

JOBS_FILENAME = "jobs.json"

_FIELD_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
_FIELD_NAMES = ["minute", "hour", "day-of-month", "month", "day-of-week"]

_ALIASES = {
    "@hourly":  "0 * * * *",
    "@daily":   "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly":  "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly":  "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}


class CronError(ValueError):
    """A schedule that cannot be parsed.

    Raised with the offending FIELD named. "invalid cron expression" tells the
    person nothing about which of five fields to fix.
    """


def _parse_field(spec: str, index: int) -> set[int]:
    lo, hi = _FIELD_BOUNDS[index]
    name = _FIELD_NAMES[index]
    out: set[int] = set()

    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{name}: empty value in {spec!r}")

        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"{name}: step must be a positive integer, got {step_s!r}")
            step = int(step_s)

        if part == "*":
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"{name}: range must be numbers, got {part!r}")
            start, end = int(a), int(b)
            if start > end:
                raise CronError(f"{name}: range {part!r} runs backwards")
        elif part.isdigit():
            start = end = int(part)
        else:
            raise CronError(f"{name}: cannot parse {part!r}")

        # Sunday is 0, and 7 is also Sunday by convention. Normalise before the
        # bounds check so `* * * * 7` is accepted rather than rejected as 7>6.
        if index == 4:
            start = 0 if start == 7 else start
            end = 0 if end == 7 else end
            if start > end:
                start, end = end, start

        if start < lo or end > hi:
            raise CronError(f"{name}: {part!r} outside {lo}-{hi}")

        out.update(range(start, end + 1, step))

    if not out:
        raise CronError(f"{name}: {spec!r} matches nothing")
    return out


@dataclass(frozen=True)
class Cron:
    """A parsed 5-field cron expression: minute hour day-of-month month day-of-week."""

    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    source: str

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        raw = expr.strip()
        expanded = _ALIASES.get(raw.lower(), raw)
        parts = expanded.split()
        if len(parts) != 5:
            raise CronError(
                f"expected 5 fields (minute hour day-of-month month day-of-week), "
                f"got {len(parts)} in {expr!r}"
            )
        sets = [_parse_field(p, i) for i, p in enumerate(parts)]
        return cls(
            minute=frozenset(sets[0]), hour=frozenset(sets[1]),
            dom=frozenset(sets[2]), month=frozenset(sets[3]),
            dow=frozenset(sets[4]), source=raw,
        )

    def matches(self, when: datetime) -> bool:
        """Standard cron day semantics.

        When BOTH day-of-month and day-of-week are restricted, cron ORs them --
        `0 0 13 * 5` is the 13th *or* any Friday, not Friday the 13th. This
        surprises everyone once; it is the documented behaviour and matching it
        matters more than matching intuition.
        """
        if when.minute not in self.minute or when.hour not in self.hour:
            return False
        return self.matches_date(when)

    def matches_date(self, when) -> bool:
        """True when the DAY qualifies, ignoring hour and minute.

        Split out so the calendar can ask "does this job run on the 14th?"
        without inventing a second copy of the day rules -- and the day rules
        are the surprising part, so a second copy would drift and only the
        calendar would be wrong.
        """
        if when.month not in self.month:
            return False

        dom_restricted = len(self.dom) < 31
        dow_restricted = len(self.dow) < 7
        dom_hit = when.day in self.dom
        dow_hit = (when.weekday() + 1) % 7 in self.dow  # Mon=0 -> cron Sun=0

        if dom_restricted and dow_restricted:
            return dom_hit or dow_hit
        return dom_hit and dow_hit

    def times_on(self, when, cap: int = 6) -> list[str]:
        """The HH:MM slots this job fires on that date, at most `cap` of them.

        `*/5 * * * *` is 288 fires a day; a calendar cell has room for two.
        The caller gets the first `cap` and the true total, so it can say
        "+282 more" rather than silently showing a fraction as the whole.
        """
        if not self.matches_date(when):
            return []
        return sorted(f"{h:02d}:{m:02d}" for h in self.hour for m in self.minute)[:cap]

    def count_on(self, when) -> int:
        """How many times this job fires on that date. 0 when it does not."""
        if not self.matches_date(when):
            return 0
        return len(self.hour) * len(self.minute)

    def next_after(self, when: datetime, limit_days: int = 400) -> datetime | None:
        """First matching minute strictly after `when`, or None within the limit.

        None is a real answer, not a failure: `0 0 30 2 *` (Feb 30th) never
        matches. Returning None lets the caller say so instead of looping.
        """
        probe = (when + timedelta(minutes=1)).replace(second=0, microsecond=0)
        horizon = when + timedelta(days=limit_days)
        while probe <= horizon:
            if self.matches(probe):
                return probe
            probe += timedelta(minutes=1)
        return None


@dataclass
class Job:
    """One scheduled prompt.

    `last_fired_slot` is the ISO minute a job last ran, and it is what makes
    the poll loop idempotent: ticking every 20s means a job's minute is visited
    about three times, and without a per-slot record it would fire on each.
    Storing the SLOT rather than a timestamp is the whole trick -- a timestamp
    would need a tolerance window to compare against, and that window is
    exactly the bug.
    """

    prompt: str
    schedule: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    enabled: bool = True
    new_conversation: bool = False
    last_fired_slot: str | None = None
    run_count: int = 0
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    label: str = ""
    #: Unattended work defaults to the narrow host profile. This is stored per
    #: job so an explicit human override is durable rather than ambient app
    #: state that silently changes every automation at once.
    tool_profile: str = SCHEDULED

    def cron(self) -> Cron:
        return Cron.parse(self.schedule)

    def describe(self) -> str:
        state = "on " if self.enabled else "off"
        name = self.label or self.prompt
        if len(name) > 46:
            name = name[:45] + "…"
        return f"{self.id}  {state}  {self.schedule:<16}  x{self.run_count:<4} {name}"


def slot_of(when: datetime) -> str:
    """The minute a moment belongs to, as a stable key."""
    return when.strftime("%Y-%m-%dT%H:%M")


def due(jobs: list[Job], now: datetime) -> list[Job]:
    """Jobs that should fire for the minute containing `now`.

    Only the CURRENT minute is considered. A job whose slot passed while the
    app was closed is NOT fired on startup: waking to a burst of overdue
    prompts is worse than missing them, because the burst arrives with no
    indication that it is late and the model answers all of it as if it were
    now. `LATE_TOLERANCE` keeps a tick that drifts slightly still counting as
    on-time for its slot.
    """
    ready = []
    for job in jobs:
        if not job.enabled:
            continue
        try:
            cron = job.cron()
        except CronError:
            continue  # a malformed schedule never fires; /cron list shows it
        slot = now.replace(second=0, microsecond=0)
        if not cron.matches(slot):
            continue
        if job.last_fired_slot == slot_of(slot):
            continue  # already fired this minute
        if now - slot > LATE_TOLERANCE:
            continue
        ready.append(job)
    return ready


def jobs_path(root: Path) -> Path:
    return Path(root) / JOBS_FILENAME


def load(root: Path) -> list[Job]:
    """Read the job file. A missing or unreadable file is an empty list.

    A corrupt job file must not stop the app from starting -- the scheduler is
    a convenience and the chat is the product.
    """
    path = jobs_path(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict) or "prompt" not in item or "schedule" not in item:
            continue
        known = {k: v for k, v in item.items() if k in Job.__dataclass_fields__}
        try:
            out.append(Job(**known))
        except TypeError:
            continue
    return out


def save(jobs: list[Job], root: Path) -> None:
    """Write the job file atomically.

    Temp file in the SAME directory, then os.replace. Never open the real path
    for writing: that truncates at open, so an encoding error lands between the
    destructive step and the constructive one and the traceback is evidence the
    damage is already maximal.
    """
    path = jobs_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([asdict(j) for j in jobs], indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".jobs-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
