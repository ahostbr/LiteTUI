"""Native launch holds actual source inputs until its explicit activation ACK."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from test_rust_background_input import app as _app
from test_rust_background_input import command, settle_inputs
from test_rust_background_input import switch_app as _switch_app
from test_rust_background_mail import mailbox
from test_rust_restored_input import seed

from litetui import background_input as work
from litetui import (
    background_mail,
    codex_steering,
    gui_rpc,
    paths,
    restored_input,
    rpc,
    scheduler,
)

app, switch_app = _app, _switch_app
pytestmark = pytest.mark.asyncio


def launch(app, monkeypatch, captured):
    app._launch_options.work_lifecycle_version = 1
    monkeypatch.setattr(app, "_rpc_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app, "_user_bubble", lambda *_args, **_kwargs: SimpleNamespace(border_title="You"))
    monkeypatch.setattr(app, "_chat_running", lambda: any(w.group == "chat" and not w.is_finished for w in app.workers))
    return work.capture(app, command(app)) if captured else None


def activate(app, descriptor):
    if descriptor is None:
        descriptor = work.capture(app, command(app))
    return work.activate(app, command(app, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))


@pytest.mark.parametrize("captured", [False, True], ids=["before-capture", "before-activation"])
async def test_due_job_is_not_stamped_or_dispatched_before_activation(app, monkeypatch, captured):
    job = scheduler.Job(prompt="Retained startup job", schedule="* * * * *", tool_profile="strict")
    app.jobs[:] = [job]
    scheduler.save(app.jobs, paths.data_root())
    descriptor = launch(app, monkeypatch, captured)
    before = scheduler.jobs_path(paths.data_root()).read_bytes(), app.convo_path.read_bytes()
    assert app._fire_job(job) is None
    with pytest.raises(ValueError, match="activation"):
        app._fire_job(job, manual=True)
    assert job.run_count == 0 and not app._rewrite_test_requests
    assert before == (scheduler.jobs_path(paths.data_root()).read_bytes(), app.convo_path.read_bytes())
    activate(app, descriptor)
    assert job.run_count == 0 and not app._rewrite_test_requests  # Handshake never implicitly fires jobs.
    receipt = await app._fire_job(job, manual=True).wait()
    assert receipt["delivered"] and receipt["run_count"] == 1
    await settle_inputs(app, 1)
    assert len(app._rewrite_test_requests) == 1
    await work.settle_before_shutdown(app)


@pytest.mark.parametrize("captured", [False, True], ids=["before-capture", "before-activation"])
async def test_mail_remains_unclaimed_until_activated_poll(app, tmp_path, monkeypatch, captured):
    path, done, message = mailbox(app, tmp_path, monkeypatch)
    descriptor = launch(app, monkeypatch, captured)
    before = path.read_bytes(), app.convo_path.read_bytes()
    assert await background_mail.poll(app) == []
    with pytest.raises(ValueError, match="activation"):
        app._deliver_inbox(message)
    assert before == (path.read_bytes(), app.convo_path.read_bytes()) and not list(done.iterdir())
    activate(app, descriptor)
    assert path.exists() and not app._rewrite_test_requests
    assert await background_mail.poll(app) == []
    await settle_inputs(app, 1)
    assert not path.exists() and len(list(done.iterdir())) == 1
    assert len(app._rewrite_test_requests) == 1
    await work.settle_before_shutdown(app)


@pytest.mark.parametrize("captured", [False, True], ids=["before-capture", "before-activation"])
async def test_recovered_input_and_raw_prompt_refuse_before_startup_effects(app, monkeypatch, captured):
    app._hold_restored_input = True
    native = seed(app)
    codex_steering.restore_queue(app)
    item = app._pending_input[0]
    descriptor = launch(app, monkeypatch, captured)
    before = app.convo_path.read_bytes(), app._active_tool_profile
    view = restored_input.inspect(app, app.convo_id)
    assert view["held"] == 1
    with pytest.raises(ValueError, match="activation"):
        await gui_rpc.async_dispatch(app, {"type": "gui.restored_input.continue", "id": "too-early",
            "session_id": app.convo_id, "snapshot": view["snapshot"], "entry_id": native["id"]})
    replies: list[dict[str, Any]] = []
    monkeypatch.setattr(rpc, "rpc_emit", replies.append)
    rpc._dispatch(app, {"type": "prompt", "id": "startup-prompt", "message": "Must remain unsubmitted", "tool_profile": "autonomous"})
    assert replies[-1]["ok"] is False and "activation" in replies[-1]["error"]
    assert app._deliver_queued_input() is False
    app._flush_pending_input()
    assert app._pending_input == [item] and native["state"] == "queued"
    assert before == (app.convo_path.read_bytes(), app._active_tool_profile)
    activate(app, descriptor)
    await asyncio.sleep(0)
    assert app._pending_input == [item] and app.convo_path.read_bytes() == before[0]
    assert not app._rewrite_test_requests and not work.bridge(app).inputs
    await work.settle_before_shutdown(app)
