"""Native calendar preview reads authoritative in-memory jobs without editing."""
from datetime import UTC, datetime
from unittest.mock import Mock

from litetui.scheduler import Job
from litetui.sidecar_jobs import public_jobs
from litetui.sidecar_launch import SidecarWindow


def test_jobs_projection_does_not_mutate_runtime_state():
    cron = Job(prompt="write report", schedule="0 9 * * *", label="Report", run_count=4)
    loop = Job.loop(prompt="repeat", interval_minutes=15, owner_convo_id="abc", now=datetime(2026, 9, 23, 10, tzinfo=UTC))
    jobs = [cron, loop]
    before = [(job.run_count, job.last_fired_slot, job.next_run_at) for job in jobs]
    result = public_jobs(jobs)
    assert result["jobs"][0]["label"] == "Report"
    assert result["jobs"][0]["schedule"] == "0 9 * * *"
    assert result["jobs"][1]["kind"] == "loop"
    assert result["jobs"][1]["next_run_at"] == "2026-09-23T10:15:00+00:00"
    assert before == [(job.run_count, job.last_fired_slot, job.next_run_at) for job in jobs]


def test_job_snapshot_delivery_rejects_failure(tmp_path):
    owner = SidecarWindow(tmp_path / "preview.exe")
    owner.open = Mock(return_value=True)
    owner._exchange = Mock(return_value={"jobs_snapshot": True})
    assert owner.open_jobs_snapshot("calendar", {"jobs": []})
    owner._exchange.assert_called_once_with("jobs_snapshot", {"jobs": []})
    owner._exchange.return_value = {"error": "unavailable"}
    owner.close = Mock()
    assert not owner.open_jobs_snapshot("job", {"jobs": []})
    owner.close.assert_called_once()
