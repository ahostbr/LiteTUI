"""T622: exercise real scheduler persistence, never run scheduled work."""
from litetui import rpc, scheduler


def test_rpc_create_list_delete_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    wire = []
    monkeypatch.setattr(rpc, "rpc_emit", wire.append)
    rpc._handle_jobs(None, "jobs.create", {
        "type": "jobs.create", "id": "request", "prompt": "inert",
        "schedule": "0 9 * * *", "enabled": False,
    }, "request")
    assert wire[-1]["ok"] is True, wire[-1]
    job_id = wire[-1]["result"]["id"]
    assert [j.id for j in scheduler.load(tmp_path)] == [job_id]
    rpc._handle_jobs(None, "jobs.list", {}, "list")
    assert wire[-1]["result"][0]["id"] == job_id
    rpc._handle_jobs(None, "jobs.delete", {"job_id": job_id}, "delete")
    assert wire[-1]["ok"] is True, wire[-1]
    assert scheduler.load(tmp_path) == []
