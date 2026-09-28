"""T1082 R4 — the sidecar creates a CRON job THROUGH THE PARENT.

Ryan (liteask a-a203e2c0): "we need new settings to set this at the time u create the
schedule. in litetui and the sidecar." Sentinel e9576f7f (R4): creation goes through
the parent, job_create under a jobs_write grant from hello (the settings_patch
pattern), cron only. The parent writes through CronService.create, the same path as
/cron add; the levels offered are capped by the parent, not by the grant (Dijkstra
f0ae21c1).
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import Mock

from test_sidecar_events import frame
from test_sidecar_preferred import FIRST_EVENT_ID, _child
from test_sidecar_reader import PipeProcess

from litetui import app as m
from litetui import paths, scheduler, seat_authority, sidecar_protocol
from litetui.plugins import sidecar_plugin
from litetui.sidecar_dispatch import SettingsPatchDispatcher
from litetui.sidecar_jobs import create_job, public_jobs
from litetui.sidecar_launch import SidecarWindow
from litetui.tool_policy import AUTONOMOUS, INTERACTIVE, STRICT

JOB = {"prompt": "summarise yesterday", "schedule": "0 9 * * 1-5", "label": "brief"}


def _app(profile, *, own=False):
    """conftest clears the owner mark: locked unless `own`."""
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a.settings.tool_policy_profile = profile
    a._active_tool_profile = profile
    a.call_from_thread = lambda fn, *args: fn(*args)
    a.system_message = lambda *_a, **_k: None
    if own:
        a._spawned_seat, a._owner_seat, a._pty_term = False, True, None
    return a


def _replies(owner):
    return [c.args[1] for c in owner.send_event_reply.call_args_list]


def test_SC1_the_snapshot_offers_only_the_levels_this_seat_may_record():
    jobs = [scheduler.Job(prompt="p", schedule="@daily", tool_profile="scheduled")]
    spawned = public_jobs(jobs, _app(AUTONOMOUS))
    assert (spawned["levels"], spawned["default_level"]) == ([STRICT, INTERACTIVE, AUTONOMOUS], AUTONOMOUS)
    own = public_jobs(jobs, _app(INTERACTIVE, own=True))
    assert (own["levels"], own["default_level"]) == ([STRICT, INTERACTIVE, AUTONOMOUS], INTERACTIVE)
    # Each job shows the level it RUNS at; the stored value is untouched.
    assert (own["jobs"][0]["level"], own["jobs"][0]["tool_profile"]) == (INTERACTIVE, "scheduled")
    assert "levels" not in public_jobs(jobs), "without the app, the old shape"


def test_SC2_the_reader_passes_job_create_only_under_the_jobs_write_grant():
    for granted in (False, True):
        process = PipeProcess()
        owner = SidecarWindow(Path("unused.exe"), timeout=1)
        owner.token = "a" * 48
        owner.process = process
        owner.settings_write = True          # the settings grant must not open jobs
        owner.jobs_write = granted
        events, rejected = [], []
        owner.on_event = events.append
        owner.on_rejected_frame = rejected.append
        owner._start_reader(process)
        process.respond(frame(owner, 7, "job_create", JOB))
        for _ in range(100):
            if events or rejected:
                break
            threading.Event().wait(0.01)
        if granted:
            assert [e["command"] for e in events] == ["job_create"] and rejected == []
        else:
            assert events == [] and rejected == ["jobs_write_disabled"]
        owner.close()


def test_SC3_job_create_goes_through_the_one_cron_creation_path():
    a = _app(INTERACTIVE, own=True)
    owner = Mock()
    handler = SettingsPatchDispatcher(a, owner, create_job=create_job)
    handler({"id": 1, "command": "job_create", "payload": {**JOB, "tool_profile": STRICT}})
    [reply] = _replies(owner)
    assert reply["saved"] is True, reply
    assert (reply["job"]["tool_profile"], reply["job"]["label"]) == (STRICT, "brief")
    assert [j["level"] for j in reply["jobs"]["jobs"]] == [STRICT]
    for n, (payload, why) in enumerate([
        ({**JOB, "kind": "loop"}, "never scheduled directly"),
        ({**JOB, "shell": "x"}, "Invalid job fields"),
        ({**JOB, "schedule": "nonsense"}, "5 fields"),
        ({**JOB, "prompt": "  "}, "cannot be empty"),
        ({**JOB, "tool_profile": "scheduled"}, "no level"),
    ], start=2):
        handler({"id": n, "command": "job_create", "payload": payload})
        reply = _replies(owner)[-1]
        assert reply["saved"] is False and why in reply["error"], (payload, reply)
    assert [j.tool_profile for j in scheduler.load(paths.data_root())] == [STRICT]


def test_SC3_spawned_seat_can_create_an_autonomous_job_from_the_sidecar():
    a = _app(AUTONOMOUS)
    owner = Mock()
    handler = SettingsPatchDispatcher(a, owner, create_job=create_job)
    handler({"id": 1, "command": "job_create", "payload": {**JOB, "tool_profile": AUTONOMOUS}})
    [reply] = _replies(owner)
    assert reply["saved"] is True and reply["job"]["tool_profile"] == AUTONOMOUS
    assert [j.tool_profile for j in scheduler.load(paths.data_root())] == [AUTONOMOUS]


def test_SC3_a_window_without_the_create_hook_refuses():
    app = Mock()
    app.call_from_thread.side_effect = lambda fn, *args: fn(*args)
    owner = Mock()
    SettingsPatchDispatcher(app, owner)({"id": 3, "command": "job_create", "payload": JOB})
    owner.send_event_reply.assert_called_once_with(
        3, {"saved": False, "error": "Job creation is not available to this window"})


def test_SC4_the_plugin_grants_jobs_write_in_hello_and_answers_a_childs_job_create(monkeypatch, tmp_path):
    exe = tmp_path / "litetui-sidecar.exe"
    exe.write_bytes(b"")
    written = []
    monkeypatch.setattr(sidecar_plugin, "_new_window",
                        lambda _app: SidecarWindow(exe, spawn=lambda *a, **k: _child(written), timeout=1))
    a = _app(INTERACTIVE, own=True)
    owner = sidecar_plugin._owner(a)
    assert owner.open("calendar")
    [hello] = [f for f in written if f["command"] == "hello"]
    assert hello["payload"] == {"settings_write": True, "jobs_write": True}
    owner.process.respond(sidecar_protocol.encode(
        FIRST_EVENT_ID, owner.token, "job_create", {**JOB, "tool_profile": STRICT}) + b"\n")
    for _ in range(200):
        if any(f["command"] == "event_reply" for f in written):
            break
        threading.Event().wait(0.01)
    owner.close()
    [reply] = [f for f in written if f["command"] == "event_reply"]
    assert reply["id"] == FIRST_EVENT_ID
    assert reply["payload"]["saved"] is True and reply["payload"]["job"]["tool_profile"] == STRICT
    assert [j.tool_profile for j in a.jobs] == [STRICT]
