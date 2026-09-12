"""T622: exercise real scheduler persistence, never run scheduled work."""
from types import SimpleNamespace

from litetui import rpc, scheduler


def test_rpc_create_list_delete_roundtrip(tmp_path, monkeypatch):
    """⚠️ THE APP IS NO LONGER `None`, AND THAT IS THE POINT (T689).

    This handler used to read its own copy of the list off disk, so it worked
    with no app at all — and that was the defect: it was a SECOND holder inside
    one process, so an rpc create never reached the list the calendar UI
    renders and saves. It now edits `app.jobs`, the shared list every other
    writer already goes through, and the dispatcher (rpc.py:314) has always
    passed a real app. The file assertions below are unchanged, so this still
    proves persistence and now also proves the two agree.
    """
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    wire = []
    monkeypatch.setattr(rpc, "rpc_emit", wire.append)
    app = SimpleNamespace(jobs=[])
    rpc._handle_jobs(app, "jobs.create", {
        "type": "jobs.create", "id": "request", "prompt": "inert",
        "schedule": "0 9 * * *", "enabled": False,
    }, "request")
    assert wire[-1]["ok"] is True, wire[-1]
    job_id = wire[-1]["result"]["id"]
    assert [j.id for j in scheduler.load(tmp_path)] == [job_id]
    assert [j.id for j in app.jobs] == [job_id], "the list the UI holds never saw it"
    rpc._handle_jobs(app, "jobs.list", {}, "list")
    assert wire[-1]["result"][0]["id"] == job_id
    rpc._handle_jobs(app, "jobs.delete", {"job_id": job_id}, "delete")
    assert wire[-1]["ok"] is True, wire[-1]
    assert scheduler.load(tmp_path) == []
    assert app.jobs == []
