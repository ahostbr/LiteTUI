"""Parent-owned scheduled jobs for the sidecar, never a second scheduler.

The child shows the jobs and may ASK the parent to create a cron job (T1082 R4,
job_create under the jobs_write grant). The parent validates and writes through its
own CronService.create, the same path as /cron add.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict

from litetui import seat_authority, tool_policy
from litetui.scheduler import Job, level_of

#: What a page's job_create may carry. Cron only: a loop is never scheduled directly.
JOB_CREATE_FIELDS = frozenset({"prompt", "schedule", "label", "tool_profile", "kind"})


def public_jobs(jobs: Iterable[Job], app=None) -> dict:
    """Detach the in-memory jobs; no scheduling or persistence side effects. Each
    job carries `level`, the level it RUNS at (scheduler.level_of: the removed
    "scheduled" shows as interactive). With `app`, also the levels a new job may
    record here and this seat's level as the default (T1082)."""
    out = {"jobs": [{**asdict(job), "level": level_of(job.tool_profile)} for job in jobs]}
    if app is not None:
        out["levels"] = list(tool_policy.PROFILE_NAMES)
        out["default_level"] = seat_authority.seat_profile(app)
    return out


def create_job(app, payload) -> dict:
    """A page's job_create, on the Textual thread. ValueError says why not."""
    if not isinstance(payload, dict) or set(payload) - JOB_CREATE_FIELDS:
        raise ValueError("Invalid job fields")
    if payload.get("kind", "cron") != "cron":
        raise ValueError(seat_authority.LOOP_REFUSAL)
    job = app.cron.create(str(payload.get("prompt", "")), str(payload.get("schedule", "")),
                          label=str(payload.get("label", "")), level=payload.get("tool_profile"))
    return {"saved": True, "job": asdict(job), "jobs": public_jobs(app.jobs, app)}
