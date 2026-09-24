"""Read-only view of parent-owned scheduled jobs, never a second scheduler."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict

from litetui.scheduler import Job


def public_jobs(jobs: Iterable[Job]) -> dict:
    """Detach the in-memory jobs; no scheduling or persistence side effects."""
    return {"jobs": [asdict(job) for job in jobs]}
