"""Reviewed manual Run that hands one canonical scheduler stamp to a new conversation."""
from __future__ import annotations

from dataclasses import asdict

from litetui import background_input as work
from litetui import (
    conversation_transition,
    native_schedule,
    native_work,
    native_work_controls,
    native_work_execution,
    row_store,
    scheduler,
    seat_authority,
)
from litetui.shared_state import Lease

OPERATION = "gui.work.native_schedule_manual_review"


def review(app, cmd):
    native_schedule._command(app, cmd, "manual_review", ("instance", "inspection_instance", "snapshot",
                                                         "section", "target_id", "target_revision"))
    bridge = work.bridge(app)
    if not native_schedule.enabled(app) or bridge.instance != cmd["instance"]:
        raise ValueError("Manual New Run requires this activated scheduled handoff")
    native_work_execution._guard(app)
    if (native_work_execution.mutation_hold(app) or getattr(app, "_native_schedule_pending", None) is not None
            or getattr(app, "_gui_schedules_paused", False)):
        raise ValueError("Resolve the current Work action or paused scheduler before manual Run")
    capture = getattr(app, "_native_work_inspection", None)
    section = cmd["section"]
    if (not isinstance(capture, native_work.Capture) or capture.instance != cmd["inspection_instance"]
            or capture.snapshot["snapshot"] != cmd["snapshot"] or section not in ("jobs", "loops")
            or capture.owner != native_work._owner(app, native_work._identity(app))):
        raise ValueError("Refresh the current Work inspection before manual Run")
    selection = next((entry for entry in capture.rows[section] if entry["row"]["id"] == cmd["target_id"]
                      and entry["revision"] == cmd["target_revision"]), None)
    root = app._agent_session.store.data_root
    with Lease(root / ".scheduler.lease"):
        rows, revision = native_work_controls.read_jobs(scheduler.jobs_path(root))
        selected = next((row for row in rows if row["id"] == cmd["target_id"]), None)
        if (selected is None or selection is None or native_work._job(selected) != selection
                or capture.snapshot["sources"][section]["revision"] != revision):
            raise ValueError("The raw scheduler selection changed since inspection")
        job = scheduler.Job(**selection["row"])
        if not job.new_conversation or job.kind == "loop" and job.owner_convo_id != app.convo_id:
            raise ValueError("Manual New Run requires the selected new-conversation job and its loop owner")
        if why := seat_authority.withheld(app, scheduler.level_of(job.tool_profile)):
            raise ValueError(why)
        common = {"version": work.VERSION, **work.identity(app)}
        state = conversation_transition.snapshot(app, common)
        owner, transition = conversation_transition.build_review(app, {**common, "snapshot": state["snapshot"], "action": "new"})
        raw = work.encoded(selected)
        job_snapshot = work.encoded(asdict(job))
        work.payload_text(job.prompt)
        if any(len(body) > work.LIMITS["payload_bytes"] for body in (raw, job_snapshot)):
            raise ValueError("Manual New Run exceeds its bounded raw job review")
        proposal = native_schedule.job_proposal(job, job_snapshot, transition, trigger="manual", slot=None)
        if native_work_controls.read_jobs(scheduler.jobs_path(root))[1] != revision:
            raise ValueError("Scheduler changed during manual proposal review")
    pending = native_schedule.PendingScheduledTransition(app, bridge, owner, job, proposal, work.encoded(proposal))
    pending.manual_raw_before = raw
    result = {"kind": "work_schedule_manual_review", "version": work.VERSION, "schedule_version": native_schedule.VERSION,
              "identity": dict(bridge.identity), "instance": bridge.instance, "operation_id": cmd["id"],
              "inspection_instance": capture.instance, "snapshot": cmd["snapshot"], "section": section,
              "target_revision": cmd["target_revision"], "raw_before": raw.decode(), "raw_before_sha256": work.digest(raw),
              "proposal": proposal}
    if len(work.encoded(result)) > work.LIMITS["reply_bytes"]:
        raise ValueError("Manual New review exceeds the complete reply budget")
    app._native_schedule_manual_review = pending
    return result


def stamp(app, pending, job):
    """The caller holds the scheduler lease. Recheck raw PRE inside the jobs file lock."""
    root = app._agent_session.store.data_root
    path = scheduler.jobs_path(root)
    def change(rows):
        matches = [index for index, row in enumerate(rows) if row["id"] == job.id]
        if len(matches) != 1 or work.encoded(rows[matches[0]]) != pending.manual_raw_before:
            raise ValueError("Selected raw job changed inside the scheduler stamp")
        after = {**rows[matches[0]], **asdict(job)}
        rows[matches[0]] = after
        pending.manual_raw_after = work.encoded(after)
        pending.stamp_attempted = True
        return rows
    row_store.checked_update(path, lambda selected: native_work_controls.read_jobs(selected)[0], change, prefix=".jobs-")
    scheduler.refresh(app.jobs, root)
