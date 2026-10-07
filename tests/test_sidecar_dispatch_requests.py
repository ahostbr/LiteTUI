"""Real dispatcher routing: readonly requests cannot become settings writes."""
from copy import deepcopy
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from litetui.scheduler import Job
from litetui.settings import Settings
from litetui.settings_service import SettingsService
from litetui.sidecar_dispatch import SettingsPatchDispatcher
from litetui.sidecar_jobs import public_jobs
from litetui.sidecar_launch import SidecarWindow
from litetui.sidecar_patch import apply_patch
from test_sidecar_reader import PipeProcess


def host(tmp_path):
    directory = tmp_path / ".convos" / "dispatch-fixture"
    directory.mkdir(parents=True)
    app = SimpleNamespace(
        settings=Settings(),
        convo_dir=directory,
        _settings_service=SettingsService(tmp_path),
        jobs=[Job(prompt="fixture report", schedule="0 9 * * *", label="Report")],
    )
    # Execute the shipping callback without a Textual app/thread or event loop.
    app.call_from_thread = Mock(side_effect=lambda fn, *args: fn(*args))
    owner = SimpleNamespace(
        token="dispatch-fixture-generation",
        send_event_reply=Mock(),
        on_rejected_frame=Mock(),
        settings_write=False,
        jobs_write=False,
    )
    return app, owner


def valid_patch(app):
    return {
        "changes": [{"key": "sidecar_enabled", "scope": "device", "value": True}],
        "expected_revisions": app._settings_service.snapshot(app.convo_dir.name).revisions,
    }


def test_jobs_request_with_valid_patch_does_not_change_any_settings(tmp_path):
    app, owner = host(tmp_path)
    before_settings = deepcopy(vars(app.settings))
    before_saved = app._settings_service.snapshot(app.convo_dir.name)
    expected_jobs = public_jobs(app.jobs, app)
    handler = SettingsPatchDispatcher(app, owner)  # REAL apply_patch default

    handler({"id": 1, "command": "jobs_request", "payload": valid_patch(app)})

    assert vars(app.settings) == before_settings
    after_saved = app._settings_service.snapshot(app.convo_dir.name)
    assert after_saved.saved == before_saved.saved
    assert after_saved.revisions == before_saved.revisions
    owner.send_event_reply.assert_called_once_with(1, expected_jobs)
    app.call_from_thread.assert_called_once()
    assert not owner.settings_write and not owner.jobs_write


def test_jobs_request_reads_current_jobs_without_mutating_or_sharing_state(tmp_path):
    app, owner = host(tmp_path)
    handler = SettingsPatchDispatcher(app, owner)
    before_job = deepcopy(vars(app.jobs[0]))
    handler({"id": 2, "command": "jobs_request", "payload": {}})
    first = owner.send_event_reply.call_args.args[1]
    assert first == public_jobs(app.jobs, app)
    assert vars(app.jobs[0]) == before_job
    first["jobs"][0]["label"] = "edited reply only"
    assert app.jobs[0].label == "Report"
    app.jobs.append(Job(prompt="new fixture job", schedule="0 10 * * *", label="New"))

    handler({"id": 3, "command": "jobs_request", "payload": {}})

    second = owner.send_event_reply.call_args.args[1]
    assert second == public_jobs(app.jobs, app)
    assert [job["label"] for job in second["jobs"]] == ["Report", "New"]
    assert len(first["jobs"]) == 1
    assert app.call_from_thread.call_count == 2


def test_unknown_command_with_valid_patch_is_refused_without_applying(tmp_path):
    app, owner = host(tmp_path)
    before_settings = deepcopy(vars(app.settings))
    before_saved = app._settings_service.snapshot(app.convo_dir.name)
    handler = SettingsPatchDispatcher(app, owner)

    handler({"id": 4, "command": "unsupported_request", "payload": valid_patch(app)})

    assert vars(app.settings) == before_settings
    after_saved = app._settings_service.snapshot(app.convo_dir.name)
    assert after_saved.saved == before_saved.saved
    assert after_saved.revisions == before_saved.revisions
    app.call_from_thread.assert_not_called()
    owner.send_event_reply.assert_called_once_with(4, {
        "saved": False,
        "error": "Unsupported sidecar event command: unsupported_request",
    })


def test_settings_request_keeps_snapshot_callback_and_correlated_reply(tmp_path):
    app, owner = host(tmp_path)
    result = {"settings": "fixture snapshot"}
    snapshot = Mock(return_value=result)
    apply = Mock()
    handler = SettingsPatchDispatcher(app, owner, apply=apply, snapshot=snapshot)

    handler({"id": 5, "command": "settings_request", "payload": valid_patch(app)})

    app.call_from_thread.assert_called_once_with(snapshot, app)
    snapshot.assert_called_once_with(app)
    apply.assert_not_called()
    owner.send_event_reply.assert_called_once_with(5, result)


def test_job_create_keeps_callback_payload_and_correlated_reply(tmp_path):
    app, owner = host(tmp_path)
    payload = {"prompt": "fixture", "schedule": "0 9 * * *"}
    result = {"saved": True, "job": "fixture-created"}
    create_job = Mock(return_value=result)  # no scheduler or persistence starts
    apply = Mock()
    handler = SettingsPatchDispatcher(app, owner, apply=apply, create_job=create_job)

    handler({"id": 6, "command": "job_create", "payload": payload})

    app.call_from_thread.assert_called_once_with(create_job, app, payload)
    create_job.assert_called_once_with(app, payload)
    apply.assert_not_called()
    owner.send_event_reply.assert_called_once_with(6, result)


def test_explicit_settings_patch_keeps_real_save_and_runtime_apply_path(tmp_path):
    app, owner = host(tmp_path)
    handler = SettingsPatchDispatcher(app, owner)
    payload = valid_patch(app)
    assert app.settings.sidecar_enabled is False

    handler({"id": 7, "command": "settings_patch", "payload": payload})

    app.call_from_thread.assert_called_once_with(apply_patch, app, payload)
    owner.send_event_reply.assert_called_once()
    request_id, result = owner.send_event_reply.call_args.args
    assert request_id == 7
    assert result["saved"] is True
    assert app.settings.sidecar_enabled is True
    assert app._settings_service.snapshot(app.convo_dir.name).saved.sidecar_enabled is True
    assert result["runtime"][0]["status"] == "applied"


def test_real_reader_and_dispatcher_keep_jobs_request_readonly_without_grants(tmp_path):
    app, _unused_owner = host(tmp_path)
    process = PipeProcess()  # in-memory pipe; no subprocess exists
    spawn = Mock(side_effect=AssertionError("Subprocess spawn is forbidden"))
    owner = SidecarWindow(Path("unused.exe"), spawn=spawn, timeout=1)
    owner.process = process
    owner.on_event = SettingsPatchDispatcher(app, owner)
    rejected = []
    owner.on_rejected_frame = rejected.append
    completed = threading.Event()
    replies = []
    real_reply = owner.send_event_reply

    def observe_reply(request_id, result):
        real_reply(request_id, result)  # real serialization onto the fake pipe
        replies.append((request_id, result))
        completed.set()

    owner.send_event_reply = observe_reply
    before_settings = deepcopy(vars(app.settings))
    before_saved = app._settings_service.snapshot(app.convo_dir.name)
    expected_jobs = public_jobs(app.jobs, app)
    assert not owner.settings_write and not owner.jobs_write
    owner._start_reader(process)
    try:
        process.respond(json.dumps({
            "version": 1,
            "id": 2**32,
            "token": owner.token,
            "command": "jobs_request",
            "payload": valid_patch(app),
        }).encode() + b"\n")
        assert completed.wait(1)
        assert vars(app.settings) == before_settings
        after_saved = app._settings_service.snapshot(app.convo_dir.name)
        assert after_saved.saved == before_saved.saved
        assert after_saved.revisions == before_saved.revisions
        assert replies == [(2**32, expected_jobs)]
        assert rejected == []
        assert not owner.settings_write and not owner.jobs_write
        spawn.assert_not_called()
        process.stdin.write.assert_called_once()
        wire_reply = json.loads(process.stdin.write.call_args.args[0])
        assert wire_reply["command"] == "event_reply"
        assert wire_reply["id"] == 2**32
        assert wire_reply["payload"] == expected_jobs
    finally:
        owner.close()  # closes only the fake pipe and its reader thread
        owner._reader.join(timeout=1)
        assert not owner._reader.is_alive()
