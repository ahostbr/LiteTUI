"""Owned Sidecar proposals never bypass native review or publication."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from test_rust_context_reader import app, armed, switch_app

from litetui import background_input as work
from litetui import gui_rpc, native_runtime_controls, rpc
from litetui import native_sidecar as sidecar

__all__ = ["app", "armed", "switch_app"]
pytestmark = pytest.mark.asyncio


def command(owner, operation, **fields):
    return {"type": sidecar.PREFIX + operation, "id": "sidecar-" + operation, "version": 1,
            **work.identity(owner), "work_instance": work.bridge(owner).instance, **fields}


async def call(owner, operation, *, publish=True, **fields):
    cmd = command(owner, operation, **fields)
    value = await gui_rpc.async_dispatch(owner, cmd)
    if publish:
        rpc.publish_management_result(owner, cmd, value)
    return value


class Child:
    pid = 1234
    def __init__(self):
        self.returncode = None
    def poll(self):
        return self.returncode


class Window:
    def __init__(self, path):
        self.executable = path
        self.process = None
        self.token = ""
        self._reader = SimpleNamespace(is_alive=lambda: self.process is not None and self.process.poll() is None)
        self._closing = False
        self.sent = []
        self.replies = []
        self.fail_close = False
    def open(self, view):
        if self.process is None:
            self.process = Child()
            self.token = "a" * 64
        self.sent.append(("open", view))
        return True
    def _exchange(self, name, value):
        self.sent.append((name, copy.deepcopy(value)))
        return {name: True}
    def close(self):
        self._closing = True
        if self.fail_close:
            raise RuntimeError("controlled close failure")
        self.process.returncode = 0
        self.process = None
        self.token = ""
        self._closing = False
        return True
    def send_event_reply(self, event_id, value, *, expected_process, expected_token):
        assert expected_process is self.process and expected_token == self.token
        self.replies.append((event_id, copy.deepcopy(value)))


async def opened(owner, tmp_path):
    owner.settings.sidecar_enabled = True
    await call(owner, "snapshot")
    manager = sidecar.manager(owner)
    exe = tmp_path / "controlled-sidecar.exe"
    exe.write_bytes(b"fixture only")
    manager.window = Window(exe)
    review = await call(owner, "review", action="open", view="settings")
    assert manager.window.process is None
    applied = await call(owner, "apply", token=review["token"])
    assert sidecar.hold(owner)
    await call(owner, "readback", token=review["token"], apply_id=applied["apply_id"])
    assert not sidecar.hold(owner)
    return manager


def enqueue(manager, *changes, event_id=7, command_name="settings_patch", payload=None):
    if payload is None:
        public = manager.delivered["settings"]
        payload = {"conversation_id": manager.binding["session_id"], "expected_revisions": public["revisions"],
                   "changes": [{"key": key, "value": value, "scope": public["fields"][key]["scope"]} for key, value in changes]}
    manager.enqueue({"id": event_id, "token": manager.window.token, "command": command_name, "payload": payload})


async def next_proposal(owner):
    snap = await call(owner, "snapshot")
    return snap["proposals"][-1]["proposal"]


async def step(owner, proposal):
    review = await call(owner, "step_review", proposal=proposal)
    applied = await call(owner, "step_apply", proposal=proposal, index=review["index"])
    assert sidecar.hold(owner)
    result = await call(owner, "step_readback", proposal=proposal, index=review["index"],
                        apply_id=applied["command"]["id"])
    manager = sidecar.manager(owner)
    if manager.reply_worker:
        await manager.reply_worker
    return review, result


async def test_lifecycle_requires_exact_readback_and_joins_close(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    review = await call(armed, "review", action="close", view=None)
    applied = await call(armed, "apply", token=review["token"])
    assert manager.window.process is None and sidecar.hold(armed)
    await call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])
    assert not sidecar.hold(armed)


async def test_desktop_proposal_waits_for_native_review_and_final_publication(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    initial = armed.settings.show_stop_time
    enqueue(manager, ("show_stop_time", not initial))
    proposal = await next_proposal(armed)
    assert armed.settings.show_stop_time == initial and not manager.window.replies
    review = await call(armed, "step_review", proposal=proposal)
    assert review["destinations"]["show_stop_time"].endswith("settings.json")
    applied = await call(armed, "step_apply", proposal=proposal, index=0)
    assert armed.settings.show_stop_time == (not initial)
    assert not manager.window.replies and native_runtime_controls.mutation_hold(armed)
    cmd = command(armed, "step_readback", proposal=proposal, index=0, apply_id=applied["command"]["id"])
    readback = await gui_rpc.async_dispatch(armed, cmd)
    assert not manager.window.replies and native_runtime_controls.mutation_hold(armed)
    rpc.publish_management_result(armed, cmd, readback)
    await manager.reply_worker
    assert manager.window.replies[-1][1]["saved"] is True
    assert manager.proposals[-1].status == "complete"
    assert not sidecar.hold(armed)


async def test_multi_destination_groups_preserve_prior_verified_outcome_on_cancel(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    enqueue(manager, ("show_stop_time", not armed.settings.show_stop_time), ("tool_iterations", 29))
    proposal = await next_proposal(armed)
    review, result = await step(armed, proposal)
    assert review["group"]["scope"] == "device"
    assert result["verification"]["fields"]["show_stop_time"]["saved"] == armed.settings.show_stop_time
    assert not manager.window.replies
    await call(armed, "cancel", proposal=proposal)
    reply = manager.window.replies[-1][1]
    assert reply["saved"] is False and len(reply["verified"]) == 1
    assert "remain saved" in reply["error"]
    assert armed.settings.tool_iterations != 29


async def test_model_default_uses_exact_inner_publication_and_next_request(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    enqueue(manager, ("temperature", 0.37))
    proposal = await next_proposal(armed)
    _, result = await step(armed, proposal)
    assert result["verification"]["fields"]["temperature"]["saved"] == 0.37
    assert armed._effective_request_overrides()["temperature"] == 0.37
    assert not armed._native_model_preferences_hold and not sidecar.hold(armed)
    armed._validate_owned_identity()


async def test_stale_revisions_and_child_generation_do_not_apply(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    enqueue(manager, ("tool_iterations", 27))
    proposal = await next_proposal(armed)
    manager.window.token = "b" * 64
    with pytest.raises(ValueError, match="exact child"):
        await call(armed, "step_review", proposal=proposal)
    assert armed.settings.tool_iterations != 27


async def test_lost_apply_publication_holds_admission_and_refuses_replay(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    enqueue(manager, ("show_stop_time", not armed.settings.show_stop_time))
    proposal = await next_proposal(armed)
    await call(armed, "step_review", proposal=proposal)
    applied = await call(armed, "step_apply", proposal=proposal, index=0, publish=False)
    with pytest.raises(ValueError, match="published"):
        await call(armed, "step_readback", proposal=proposal, index=0, apply_id=applied["command"]["id"])
    with pytest.raises(ValueError):
        await call(armed, "step_apply", proposal=proposal, index=0)
    assert sidecar.hold(armed) and not manager.window.replies
    await sidecar.settle_before_shutdown(armed)
    assert manager.window.process is None


@pytest.mark.parametrize("key,value", [
    ("show_stop_line", False), ("tts_timeout", 31), ("tool_iterations", 31),
    ("lm_host", "http://127.0.0.1:4242"), ("compact_keep_recent", 7),
    ("temperature", 0.23), ("stop", ["comma,kept", "line\\nnext", ""]),
    ("stop", None), ("tool_deny", ["write_file:workspace_write"]),
])
async def test_existing_native_owners_are_reused(armed, tmp_path, key, value):
    if key == "stop" and value is None:
        from test_native_model_settings import seed
        seed(armed, stop=["prior"])
    manager = await opened(armed, tmp_path)
    enqueue(manager, (key, value))
    proposal = await next_proposal(armed)
    _, result = await step(armed, proposal)
    facts = result["verification"]["fields"][key]
    assert facts["saved"] == ([] if key == "stop" and value is None else value)
    if key == "lm_host":
        assert facts["pending"] and facts["effective"] != value
    assert manager.window.replies[-1][1]["saved"]


async def test_job_proposal_uses_canonical_work_owner_and_immutable_seat_level(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    enqueue(manager, command_name="job_create", payload={
        "prompt": "controlled fixture only", "schedule": "0 0 * * *",
        "label": "sidecar fixture", "tool_profile": "strict",
    })
    proposal = await next_proposal(armed)
    review, result = await step(armed, proposal)
    assert review["review"]["action"] == "job_create"
    assert result["verification"]["job"]["verified"]
    assert manager.window.replies[-1][1]["job"]["prompt"] == "controlled fixture only"


async def test_close_failure_retains_owner_and_explicit_close_retry(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    original = manager.window.process
    manager.window.fail_close = True
    review = await call(armed, "review", action="close", view=None)
    with pytest.raises(RuntimeError, match="controlled"):
        await call(armed, "apply", token=review["token"])
    assert manager.window.process is original
    with pytest.raises(ValueError):
        await call(armed, "review", action="open", view="calendar")
    manager.window.fail_close = False
    review = await call(armed, "review", action="close", view=None)
    applied = await call(armed, "apply", token=review["token"])
    await call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])
    assert manager.window.process is None and not sidecar.hold(armed)


async def test_secret_and_unsupported_values_never_reach_terminal_proposals(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    enqueue(manager, ("groq_api_key", "private-credential-marker"))
    snapshot = await call(armed, "snapshot")
    assert snapshot["proposals"] == []
    assert "private-credential-marker" not in str(snapshot)
    assert manager.window.replies[-1][1]["saved"] is False


async def test_duplicate_event_queues_once_and_gets_only_one_final_reply(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    enqueue(manager, ("tool_iterations", 29))
    enqueue(manager, ("tool_iterations", 29))
    snapshot = await call(armed, "snapshot")
    assert len(snapshot["proposals"]) == 1
    await step(armed, snapshot["proposals"][0]["proposal"])
    assert len(manager.window.replies) == 1


async def test_tampered_wrapped_result_cannot_publish_or_release_admission(armed, tmp_path, monkeypatch):
    manager = await opened(armed, tmp_path)
    enqueue(manager, ("temperature", 0.31))
    proposal = await next_proposal(armed)
    await call(armed, "step_review", proposal=proposal)
    cmd = command(armed, "step_apply", proposal=proposal, index=0)
    result = await gui_rpc.async_dispatch(armed, cmd)
    result["result"]["apply_id"] = "other-operation"
    replies = []
    monkeypatch.setattr(rpc, "_respond", lambda _id, **value: replies.append(value))
    rpc.publish_management_result(armed, cmd, result)
    assert len(replies) == 1 and replies[0]["ok"] is False
    assert sidecar.hold(armed) and armed._native_model_preferences_hold
    await sidecar.settle_before_shutdown(armed)


async def test_enable_opt_in_is_reviewed_saved_and_does_not_launch(armed):
    assert not armed.settings.sidecar_enabled
    review = await call(armed, "configure", value=True)
    assert not armed.settings.sidecar_enabled
    assert review["group"]["field"] == "sidecar_enabled"
    applied = await call(armed, "step_apply", proposal=review["proposal"], index=0)
    await call(armed, "step_readback", proposal=review["proposal"], index=0,
               apply_id=applied["command"]["id"])
    assert armed.settings.sidecar_enabled
    assert sidecar.manager(armed).window.process is None
    assert not sidecar.hold(armed)

@pytest.mark.parametrize("changes", [None, 7, {}, [], "bad"])
async def test_malformed_desktop_changes_get_one_refusal(armed, tmp_path, changes):
    manager = await opened(armed, tmp_path)
    enqueue(manager, payload={"changes": changes})
    snapshot = await call(armed, "snapshot")
    assert not snapshot["proposals"]
    assert len(manager.window.replies) == 1
    assert manager.window.replies[0][1]["saved"] is False
    await sidecar.settle_before_shutdown(armed)


async def test_shutdown_retry_preserves_failed_child_until_joined(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    manager.window.fail_close = True
    with pytest.raises(RuntimeError, match="controlled close"):
        await sidecar.settle_before_shutdown(armed)
    assert manager.window.process is not None and manager.retirement_worker is None
    manager.window.fail_close = False
    await sidecar.settle_before_shutdown(armed)
    assert manager.retired and manager.window.process is None
    await call(armed, "snapshot")
    assert sidecar.manager(armed) is not manager


async def test_close_after_owner_model_changes_keeps_child_identity(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    old = copy.deepcopy(manager.binding)
    proof = await work.before_identity_transition(armed)
    armed.model_id = "changed-model"
    armed._agent_session.update_execution(backend=armed.backend.name, model=armed.model_id,
                                         thinking_level=armed._agent_session.authority.thinking_level)
    from litetui import effort_projection
    effort_projection.install(armed)
    work.after_identity_transition(armed, proof)
    capture = work.capture(armed, {**work.identity(armed), "version": 1, "id": "new-capture", "type": "gui.work.native_capture"})
    work.activate(armed, {**work.identity(armed), "version": 1, "id": "new-activate", "type": "gui.work.native_activate",
                          "instance": capture["instance"], "activation_token": capture["activation_token"]})
    snapshot = await call(armed, "snapshot")
    assert snapshot["model"] == "changed-model" and snapshot["child_binding"] == old
    assert snapshot["stale"]
    review = await call(armed, "review", action="close", view=None)
    assert review["model"] == "changed-model"
    applied = await call(armed, "apply", token=review["token"])
    await call(armed, "readback", token=review["token"], apply_id=applied["apply_id"])
    assert manager.window.process is None
    await call(armed, "snapshot")
    assert sidecar.manager(armed) is not manager


async def test_unapplied_review_can_be_reopened_but_never_replays_attempt(armed, tmp_path):
    manager = await opened(armed, tmp_path)
    enqueue(manager, ("show_stop_time", not armed.settings.show_stop_time))
    proposal = await next_proposal(armed)
    first = await call(armed, "step_review", proposal=proposal)
    second = await call(armed, "step_review", proposal=proposal)
    assert first["expected"] == second["expected"]
    await call(armed, "step_apply", proposal=proposal, index=0, publish=False)
    with pytest.raises(ValueError, match="publication"):
        await call(armed, "step_review", proposal=proposal)
    await sidecar.settle_before_shutdown(armed)


async def test_internal_optin_cannot_be_called_without_retained_delegate(armed):
    cmd = command(armed, "snapshot")
    cmd["type"] = "gui.sidecar_preferences.native_preferences"
    with pytest.raises(ValueError, match="retained terminal proposal"):
        await gui_rpc.async_dispatch(armed, cmd)

async def test_owned_legacy_patch_requires_all_three_canonical_revisions(armed):
    from litetui import settings_runtime, sidecar_patch
    service = settings_runtime.service_for(armed)
    before = service.snapshot(armed.convo_id, read_only=True)
    assert set(before.revisions) == {"global", "agent", "conversation"}
    value = not armed.settings.show_stop_time
    request = {"changes": [{"key": "show_stop_time", "value": value, "scope": "device"}],
               "expected_revisions": before.revisions, "conversation_id": armed.convo_id}
    missing = copy.deepcopy(request)
    missing["expected_revisions"].pop("agent")
    with pytest.raises(ValueError, match="every current owned destination"):
        sidecar_patch.apply_patch(armed, missing)
    assert service.snapshot(armed.convo_id, read_only=True).revisions == before.revisions
    result = sidecar_patch.apply_patch(armed, request)
    assert result["saved"]
    assert service.snapshot(armed.convo_id, read_only=True).saved.show_stop_time == value

async def test_repeated_local_optin_reviews_do_not_exhaust_completed_history(armed):
    for _ in range(sidecar.MAX_PROPOSALS + 1):
        review = await call(armed, "configure", value=not armed.settings.sidecar_enabled)
        applied = await call(armed, "step_apply", proposal=review["proposal"], index=0)
        await call(armed, "step_readback", proposal=review["proposal"], index=0, apply_id=applied["command"]["id"])
    manager = sidecar.manager(armed)
    assert len(manager.proposals) == sidecar.MAX_PROPOSALS
    assert all(p.status == "complete" and p.replied for p in manager.proposals)
    assert manager.window.process is None and not sidecar.hold(armed)
