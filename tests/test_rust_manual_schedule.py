"""Canonical manual New job acceptance, raw scheduler CAS and deferred wake ownership."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from test_rust_background_input import app as owned_app
from test_rust_background_input import command, settle_inputs
from test_rust_background_input import switch_app as owned_switch_app
from test_rust_scheduled_publication import ENTER, LEAVE, published_rpc

from litetui import background_input as work
from litetui import hook_host, native_work, paths, scheduler

app, switch_app = owned_app, owned_switch_app
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def armed(app, monkeypatch):
    monkeypatch.setattr(hook_host, "enter_conversation", ENTER)
    monkeypatch.setattr(hook_host, "leave_conversation", LEAVE)
    app._hook_conversation_id = app.convo_id
    app._rpc = app._gui_rpc_enabled = True
    app._launch_options.work_lifecycle_version = 1
    monkeypatch.setattr(app, "_user_bubble", lambda *_a, **_kw: SimpleNamespace(border_title="You"))
    app._manual_initial_activation = await activate(app, monkeypatch)
    return app


async def activate(app, monkeypatch):
    capture = work.capture(app, command(app, type="gui.work.native_capture", execution_version=1))
    activation = await published_rpc(app, monkeypatch, command(app, type="gui.work.native_activate", execution_version=1,
        scheduled_transition_version=1, instance=capture["instance"], activation_token=capture["activation_token"]))
    return {"capture": capture, "activation": activation}


async def reviewed(app, monkeypatch, kind="cron"):
    if kind == "loop":
        job = scheduler.Job.loop(prompt="Manual next", interval_minutes=17, owner_convo_id=app.convo_id, tool_profile="strict")
        job.new_conversation = True
    else:
        job = scheduler.Job(prompt="Manual next", schedule="0 0 1 1 *", new_conversation=True, tool_profile="strict")
    job.enabled = False
    job.last_fired_slot = "2026-11-04T12:04"
    app.jobs[:] = [job]
    root = paths.data_root()
    scheduler.save(app.jobs, root)
    path = scheduler.jobs_path(root)
    rows = work.strict_json(path.read_bytes())
    rows[0]["future_extension"] = {"preserve": [True, 0, "ðŸŒ±"]}
    path.write_bytes(work.encoded(rows))
    snap = native_work.snapshot(app, command(app, type="gui.work.native_snapshot"))
    section = "jobs" if kind == "cron" else "loops"
    page = native_work.page(app, command(app, type="gui.work.native_page", instance=snap["instance"],
        snapshot=snap["snapshot"], section=section, offset=0, limit=1))
    app._manual_inspection = {"snapshot": snap, "page": page}
    selected = page["rows"][0]
    request = command(app, id="manual-review", type="gui.work.native_schedule_manual_review", schedule_version=1,
        instance=work.bridge(app).instance, inspection_instance=snap["instance"], snapshot=snap["snapshot"],
        section=section, target_id=job.id, target_revision=selected["revision"])
    prior = path.read_bytes()
    result = await published_rpc(app, monkeypatch, request)
    proposal = result["proposal"]
    assert proposal["trigger"] == "manual" and proposal["due_slot"] is None
    assert work.digest(result["raw_before"].encode()) == result["raw_before_sha256"]
    assert path.read_bytes() == prior and not app._rewrite_test_requests and not work.prompt_refusal(app)
    accept = command(app, type="gui.work.native_schedule_accept", id="accept", schedule_version=1,
        instance=work.bridge(app).instance, proposal_id=proposal["proposal_id"], proposal_token=proposal["proposal_token"])
    return result, accept, path


@pytest.mark.parametrize("kind", ["cron", "loop"])
async def test_cross_minute_manual_new_stamps_once_and_delivers_to_exact_target(armed, monkeypatch, kind):
    app = armed
    from litetui import app as app_module
    class Clock(datetime):
        moment = datetime(2026, 11, 4, 12, 4, 17)  # noqa: DTZ001 - canonical scheduler local wall time
        @classmethod
        def now(cls, tz=None):
            return cls.moment if tz is None else cls.moment.astimezone(tz)
    monkeypatch.setattr(app_module, "datetime", Clock)
    review, accept, path = await reviewed(app, monkeypatch, kind)
    Clock.moment = datetime(2026, 11, 4, 12, 6, 17)  # noqa: DTZ001 - canonical scheduler local wall time
    result = await published_rpc(app, monkeypatch, accept)
    assert result["status"] == "staged", result["reason"]
    proposal = review["proposal"]
    assert app.convo_id == proposal["transition"]["target_session_id"] and not app._rewrite_test_requests
    actual = work.strict_json(path.read_bytes())[0]
    assert actual["run_count"] == 1 and actual["last_fired_slot"] == "2026-11-04T12:06" and not actual["enabled"]
    if kind == "loop":
        assert actual["next_run_at"] == "2026-11-04T12:23:17"
    assert actual["future_extension"] == work.strict_json(review["raw_before"])["future_extension"]
    assert result["stamp"]["raw_sha256"] == work.digest(work.encoded(actual))
    read_cmd = command(app, type="gui.work.native_schedule_readback", id="readback", schedule_version=1,
        old_instance=accept["instance"], proposal_id=proposal["proposal_id"], proposal_token=proposal["proposal_token"],
        accepted_operation_id=accept["id"], result_sha256=work.digest(work.encoded(result)))
    verified = await published_rpc(app, monkeypatch, read_cmd)
    assert verified["job_readback"]["raw_sha256"] == result["stamp"]["raw_sha256"]
    target = await activate(app, monkeypatch)
    await settle_inputs(app, 1)
    assert len(app._rewrite_test_requests) == 1 and work.strict_json(path.read_bytes())[0] == actual
    if export := os.environ.get("LITETUI_MANUAL_NEW_FIXTURES"):
        output = Path(export)
        cases = work.strict_json(output.read_bytes()) if output.exists() else {}
        cases[kind] = {"initial": app._manual_initial_activation, "inspection": app._manual_inspection,
                       "manual_review": review, "accept": accept,
                       "result": result, "readback_request": read_cmd, "readback": verified, "target": target,
                       "target_ledger": work.bridge(app).document()}
        output.write_bytes(work.encoded(cases))


async def test_manual_extension_only_change_refuses_before_any_effect(armed, monkeypatch):
    app = armed
    _, accept, path = await reviewed(app, monkeypatch)
    old_session = app.convo_id
    rows = work.strict_json(path.read_bytes())
    rows[0]["future_extension"]["preserve"].append(2)
    changed = work.encoded(rows)
    path.write_bytes(changed)
    result = await published_rpc(app, monkeypatch, accept)
    assert result["status"] == "refused" and path.read_bytes() == changed
    assert app.convo_id == old_session and not app._rewrite_test_requests


async def test_decline_never_starts_manual_job(armed, monkeypatch):
    app = armed
    _, accept, path = await reviewed(app, monkeypatch)
    before = path.read_bytes()
    result = await published_rpc(app, monkeypatch, {**accept, "type": "gui.work.native_schedule_decline",
        "id": "decline", "reason": "user cancelled"})
    assert result["status"] == "declined" and path.read_bytes() == before and not app._rewrite_test_requests


async def test_raw_change_inside_coordinated_stamp_refuses_without_clobber(armed, monkeypatch):
    from litetui import row_store
    app = armed
    _, accept, path = await reviewed(app, monkeypatch)
    checked = row_store.checked_update
    old_id = app.convo_id
    def competing_write(selected, reader, transform, **options):
        rows = work.strict_json(selected.read_bytes())
        rows[0]["late_extension"] = "racing writer"
        selected.write_bytes(work.encoded(rows))
        return checked(selected, reader, transform, **options)
    monkeypatch.setattr(row_store, "checked_update", competing_write)
    result = await published_rpc(app, monkeypatch, accept)
    current = work.strict_json(path.read_bytes())[0]
    assert result["status"] == "refused" and result["stamp"] is None
    assert current["late_extension"] == "racing writer" and current["run_count"] == 0
    assert app.convo_id == old_id and not app._rewrite_test_requests


async def test_quit_before_manual_accept_refuses_without_stamp(armed, monkeypatch):
    app = armed
    _, accept, path = await reviewed(app, monkeypatch)
    initial = path.read_bytes()
    app._gui_quitting = True
    result = await published_rpc(app, monkeypatch, accept)
    assert result["status"] == "refused" and path.read_bytes() == initial and not app._rewrite_test_requests


async def test_independent_readback_refuses_extension_drift_and_never_wakes(armed, monkeypatch):
    from litetui import native_schedule
    app = armed
    review, accept, path = await reviewed(app, monkeypatch)
    result = await published_rpc(app, monkeypatch, accept)
    assert result["status"] == "staged"
    rows = work.strict_json(path.read_bytes())
    rows[0]["future_extension"] = "changed after stamp"
    path.write_bytes(work.encoded(rows))
    p = review["proposal"]
    read = command(app, type="gui.work.native_schedule_readback", id="readback", schedule_version=1,
        old_instance=accept["instance"], proposal_id=p["proposal_id"], proposal_token=p["proposal_token"],
        accepted_operation_id=accept["id"], result_sha256=work.digest(work.encoded(result)))
    with pytest.raises(ValueError, match="raw POST"):
        native_schedule.readback(app, read)
    assert not app._rewrite_test_requests and app._native_work_deferred.handle is None


async def test_explicit_run_can_stamp_a_disabled_job_again_in_the_same_minute(armed, monkeypatch):
    from litetui import app as app_module
    class Clock(datetime):
        @classmethod
        def now(cls):
            return datetime(2026, 11, 4, 12, 4, 30)  # noqa: DTZ001 - canonical scheduler local wall time
    monkeypatch.setattr(app_module, "datetime", Clock)
    _, accept, path = await reviewed(armed, monkeypatch)
    assert work.strict_json(path.read_bytes())[0]["last_fired_slot"] == "2026-11-04T12:04"
    result = await published_rpc(armed, monkeypatch, accept)
    assert result["status"] == "staged" and result["stamp"]["run_count"] == 1
    assert result["stamp"]["slot"] == "2026-11-04T12:04" and not armed._rewrite_test_requests
