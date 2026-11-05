"""Real RPC output failures must retain every owned publication barrier."""

from __future__ import annotations

import pytest
from test_native_dictation import command as dictation_command
from test_native_playback import follow, review_command
from test_rust_claude_queue import invoke
from test_rust_claude_queue import running as claude_running
from test_rust_context_reader import armed as model_armed
from test_rust_model_lifecycle import call as model_call
from test_rust_model_lifecycle import command as model_command
from test_rust_model_lifecycle import prepare as prepare_model
from test_rust_model_lifecycle import review as review_model
from test_rust_runtime_controls import app, snapshot_review, switch_app
from test_rust_runtime_controls import armed as runtime_armed
from test_rust_runtime_controls import cmd as control_command
from test_rust_scheduled_publication import activation

from litetui import background_input as work
from litetui import (
    gui_rpc,
    native_claude_queue,
    native_dictation,
    native_midturn,
    native_playback,
    rpc,
)
from litetui import native_runtime_controls as controls

__all__ = ["app", "claude_running", "model_armed", "runtime_armed", "switch_app"]
pytestmark = pytest.mark.asyncio
ACTUAL_EMIT = rpc.rpc_emit


class Output:
    def __init__(self, failure):
        self.failure = failure
        self.rows = []
        self.flushed = False

    def write(self, text):
        if self.failure == "write":
            raise BrokenPipeError("Controlled response write failure")
        self.rows.append(work.strict_json(text))
        return len(text)

    def flush(self):
        if self.failure == "flush":
            raise OSError("Controlled response flush failure")
        self.flushed = True


def publish(app, command, result, monkeypatch, failure):
    output = Output(failure)
    with monkeypatch.context() as patch:
        patch.setattr(rpc, "rpc_emit", ACTUAL_EMIT)
        patch.setattr(rpc, "_real_stdout", output)
        if failure:
            with pytest.raises(OSError, match="Controlled response"):
                rpc.publish_management_result(app, command, result)
        else:
            rpc.publish_management_result(app, command, result)
            assert output.flushed
            assert output.rows[-1]["id"] == command["id"]
            assert output.rows[-1]["ok"] is True
        # A completed or failed publication must not leak its strict output
        # scope into ordinary legacy emissions on this same task.
        output.failure = "write"
        rpc.rpc_emit({"type": "output", "text": "Ordinary event after publication"})
        with pytest.raises(OSError, match="Controlled response"):
            rpc.rpc_emit(
                {"type": "response", "result": {"runtime_controls_version": 1}}
            )


@pytest.mark.parametrize("failure", [None, "write", "flush"])
@pytest.mark.parametrize("action", ["apply", "cancel"])
async def test_dictation_release_uses_real_output_boundary(
    runtime_armed, monkeypatch, failure, action
):
    owner, _ = runtime_armed
    review = await native_dictation.dispatch(owner, dictation_command(owner, "review"))
    if action == "apply":
        # Exercise the publication of a completed transcript without opening a device.
        capture = owner._native_dictation
        capture.status, capture.transcript = "ready", "Exact retained transcript"
        review = await native_dictation.dispatch(
            owner, dictation_command(owner, "status", review)
        )
    command = dictation_command(owner, action, review)
    result = await native_dictation.dispatch(owner, command)
    assert native_dictation.blocking(owner)
    publish(owner, command, result, monkeypatch, failure)
    assert native_dictation.blocking(owner) == bool(failure)


@pytest.mark.parametrize("failure", [None, "write", "flush"])
async def test_playback_close_uses_real_output_boundary(
    runtime_armed, monkeypatch, failure
):
    owner, _ = runtime_armed
    owner.settings.tts_enabled = True
    command = review_command(owner)
    review = await native_playback.dispatch(owner, command)
    command = follow(command, review, "close")
    result = await native_playback.dispatch(owner, command)
    assert native_playback.blocking(owner)
    publish(owner, command, result, monkeypatch, failure)
    assert native_playback.blocking(owner) == bool(failure)


@pytest.mark.parametrize("failure", [None, "write", "flush"])
@pytest.mark.parametrize("phase", ["apply", "readback"])
async def test_model_lifecycle_uses_real_output_boundary(
    model_armed, monkeypatch, failure, phase
):
    owner = model_armed
    prepare_model(owner)
    review = await review_model(owner)
    if phase == "apply":
        command = model_command(owner, "apply", token=review["token"])
    else:
        applied = await model_call(owner, "apply", token=review["token"])
        command = model_command(
            owner, "readback", token=review["token"], apply_id=applied["apply_id"]
        )
    result = await gui_rpc.async_dispatch(owner, command)
    retained = owner._native_model_lifecycle_review
    publish(owner, command, result, monkeypatch, failure)
    if phase == "apply":
        assert retained.apply_published == (failure is None)
        assert owner._native_model_lifecycle_hold
    else:
        assert owner._native_model_lifecycle_hold == bool(failure)
        assert (owner._native_model_lifecycle_review is retained) == bool(failure)


@pytest.mark.parametrize("failure", [None, "write", "flush"])
async def test_claude_queue_uses_real_output_boundary(
    claude_running, monkeypatch, failure
):
    owner, command, queries = claude_running
    review = await invoke(owner, command(native_midturn.SEND_REVIEW))
    apply = command(
        native_midturn.SEND_APPLY, review=review["review"], native=review["native"]
    )

    async def deliver():
        result = await gui_rpc.async_dispatch(owner, apply)
        publish(owner, apply, result, monkeypatch, failure)

    await owner.run_worker(
        deliver(), group="gui-management", exit_on_error=False
    ).wait()
    assert native_claude_queue.blocking(owner) == bool(failure)
    assert queries == ["first controlled input"]
    entry = next(iter(work.bridge(owner).inputs.values()))
    assert (
        entry.state == "queued" and entry.item["_claude_entry"]["state"] == "prepared"
    )


@pytest.mark.parametrize("failure", [None, "write", "flush"])
@pytest.mark.parametrize("phase", ["apply", "readback"])
async def test_runtime_control_uses_real_output_boundary(
    runtime_armed, monkeypatch, failure, phase
):
    owner, _ = runtime_armed
    _, review = await snapshot_review(owner, monkeypatch, "plan_mode", True)
    command = {"id": "apply", "type": controls.PREFIX + "apply", "review": review}
    applied = await controls.apply(owner, command)
    if phase == "readback":
        rpc.publish_management_result(owner, command, applied)
        command = control_command(
            owner,
            "readback",
            action_id=review["action_id"],
            review_token=review["review_token"],
            apply_operation_id=applied["apply_operation_id"],
            readback_token=applied["readback_token"],
            result_sha256=work.digest(work.encoded(applied)),
        )
        result = controls.readback(owner, command)
    else:
        result = applied
    attempt = controls._state(owner).attempt
    publish(owner, command, result, monkeypatch, failure)
    if phase == "apply":
        assert attempt.result_published == (failure is None)
        assert controls.mutation_hold(owner)
    else:
        assert attempt.readback_published == (failure is None)
        assert bool(controls.mutation_hold(owner)) == bool(failure)


@pytest.mark.parametrize("failure", [None, "write", "flush"])
async def test_scheduled_activation_uses_real_output_boundary(
    app, monkeypatch, failure
):
    command = activation(app)
    result = work.activate(app, command)
    assert not work.enabled(app)
    publish(app, command, result, monkeypatch, failure)
    assert work.enabled(app) == (failure is None)
