"""Sidecar admission shares native holds without blocking retained settlement."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_native_sidecar import (
    app,
    armed,
    call,
    command,
    enqueue,
    next_proposal,
    opened,
    switch_app,
)

from litetui import gui_rpc, native_runtime_controls, rpc
from litetui import native_sidecar as sidecar

__all__ = ["app", "armed", "switch_app"]
pytestmark = pytest.mark.asyncio

HOLDS = (
    ("_native_voice_action", SimpleNamespace()),
    ("_native_voice_catalog", SimpleNamespace()),
    ("_native_playback", SimpleNamespace()),
    ("_native_dictation", SimpleNamespace(status="capturing")),
    ("_native_model_preferences_hold", True),
    ("_native_runtime_controls", native_runtime_controls.State(None, None, 0, {}, (),
        attempt=SimpleNamespace(verified=False))),
)


@pytest.mark.parametrize(("attribute", "value"), HOLDS)
@pytest.mark.parametrize("operation", ("open", "refresh", "configure"))
async def test_external_hold_rejects_new_sidecar_admission(armed, tmp_path, monkeypatch, attribute, value, operation):
    manager = await opened(armed, tmp_path)
    before = (list(manager.window.sent), len(manager.proposals))
    with monkeypatch.context() as held:
        held.setattr(armed, attribute, value, raising=False)
        assert native_runtime_controls.mutation_hold(armed)
        with pytest.raises(ValueError, match="owns|own admission"):
            if operation == "configure":
                await call(armed, "configure", value=False)
            else:
                await call(armed, "review", action=operation, view="settings")
    assert (manager.window.sent, len(manager.proposals)) == before
    assert not sidecar.hold(armed)


@pytest.mark.parametrize(("attribute", "value"), HOLDS)
@pytest.mark.parametrize("operation", ("open", "opt_in", "desktop"))
async def test_external_hold_after_review_refuses_apply_without_consuming(armed, tmp_path, monkeypatch, attribute, value, operation):
    manager = await opened(armed, tmp_path)
    if operation == "open":
        review = await call(armed, "review", action="open", view="settings")
        op, fields = "apply", {"token": review["token"]}
    elif operation == "opt_in":
        armed.settings.sidecar_enabled = False
        review = await call(armed, "configure", value=True)
        op, fields = "step_apply", {"proposal": review["proposal"], "index": review["index"]}
    else:
        enqueue(manager, ("show_stop_time", not armed.settings.show_stop_time))
        token = await next_proposal(armed)
        review = await call(armed, "step_review", proposal=token)
        op, fields = "step_apply", {"proposal": token, "index": review["index"]}
    before = (armed.settings.sidecar_enabled, armed.settings.show_stop_time, list(manager.window.sent))
    with monkeypatch.context() as held:
        held.setattr(armed, attribute, value, raising=False)
        with pytest.raises(ValueError, match="owns|own admission"):
            await call(armed, op, **fields)
    assert (armed.settings.sidecar_enabled, armed.settings.show_stop_time, manager.window.sent) == before
    assert not sidecar.hold(armed)
    if operation == "open":
        assert manager.operation.apply_id is None
    else:
        assert manager.proposals[-1].current.phase == "review"
        assert manager.proposals[-1].status == "reviewing"
    applied = await call(armed, op, **fields)
    if operation == "open":
        await call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])
    else:
        await call(armed, "step_readback", proposal=fields["proposal"], index=fields["index"],
                   apply_id=applied["command"]["id"])
    if manager.reply_worker:
        await manager.reply_worker


@pytest.mark.parametrize(("attribute", "value"), HOLDS)
async def test_retained_lifecycle_readback_and_cleanup_survive_external_hold(armed, tmp_path, monkeypatch, attribute, value):
    manager = await opened(armed, tmp_path)
    review = await call(armed, "review", action="refresh", view="settings")
    applied = await call(armed, "apply", token=review["token"])
    with monkeypatch.context() as held:
        held.setattr(armed, attribute, value, raising=False)
        result = await call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])
        assert result["verified"] is True
        close = await call(armed, "review", action="close", view=None)
        applied = await call(armed, "apply", token=close["token"])
        result = await call(armed, "readback", token=close["token"], apply_id=applied["apply_id"])
        assert result["verified"] is True
        assert native_runtime_controls.mutation_hold(armed)
    assert manager.window.process is None and not sidecar.hold(armed)


async def test_exact_delegated_model_readback_retains_both_holds_until_publication(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    enqueue(manager, ("temperature", 0.37))
    proposal = await next_proposal(armed)
    await call(armed, "step_review", proposal=proposal)
    applied = await call(armed, "step_apply", proposal=proposal, index=0)
    assert sidecar.hold(armed) and armed._native_model_preferences_hold
    cmd = command(armed, "step_readback", proposal=proposal, index=0, apply_id=applied["command"]["id"])
    result = await gui_rpc.async_dispatch(armed, cmd)
    assert sidecar.hold(armed) and armed._native_model_preferences_hold
    rpc.publish_management_result(armed, cmd, result)
    await manager.reply_worker
    assert not sidecar.hold(armed) and not armed._native_model_preferences_hold
    assert armed._effective_request_overrides()["temperature"] == 0.37


@pytest.mark.parametrize(("attribute", "value"), HOLDS)
@pytest.mark.parametrize("operation", ("review", "apply"))
async def test_delegated_opt_in_itself_honors_external_hold(armed, tmp_path, monkeypatch, attribute, value, operation):
    manager = await opened(armed, tmp_path)
    armed.settings.sidecar_enabled = False
    review = await call(armed, "configure", value=True)
    step = manager.proposals[-1].current
    if operation == "review":
        cmd = review["command"]
    else:
        cmd = {**sidecar.routes.apply_command(armed, step.group, step.review, step.draft), "id": "direct-apply"}
    with monkeypatch.context() as held:
        held.setattr(armed, attribute, value, raising=False)
        with pytest.raises(ValueError, match="owns|own admission"):
            await sidecar._delegate(armed, manager, cmd)
    assert armed.settings.sidecar_enabled is False
    assert armed._native_sidecar_preference["apply_id"] is None
    assert not sidecar.hold(armed)
