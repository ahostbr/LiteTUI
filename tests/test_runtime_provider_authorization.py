"""Provider callback races against actual Work/runtime control RPC publication."""
import asyncio
from types import SimpleNamespace

import pytest
import test_rust_background_input as background_fixtures
import test_rust_runtime_controls as runtime_fixtures
from claude_agent_sdk import PermissionResultDeny
from test_rust_runtime_controls import cmd, snapshot_review
from test_rust_scheduled_publication import published_rpc

from litetui import background_input as work
from litetui import hook_host
from litetui import native_runtime_controls as controls
from litetui.claude_tools import ClaudeTools
from litetui.codex_native_policy import NativePolicy
from litetui.event_origin import EventOrigin

owned_app = background_fixtures.app
switch_app = background_fixtures.switch_app
armed = runtime_fixtures.armed
pytestmark = pytest.mark.asyncio


@pytest.fixture
def app(owned_app, request, monkeypatch):
    app = owned_app
    app.tools_enabled = True
    if request.param == "claude":
        from litetui.claude_backend import ClaudeBackend
        app.backend = ClaudeBackend(app.settings)
    else:
        monkeypatch.setattr(app.backend, "name", "codex")
        monkeypatch.setattr(app.backend, "app_server", object(), raising=False)
    app._agent_session.update_execution(backend=request.param, model=app.model_id, thinking_level="default")
    return app


def stream(app):
    origin = EventOrigin(session_id=app.convo_id, source="rpc", operation_id="ordinary").scheduled()
    work.bridge(app).executions[origin.execution_id] = (SimpleNamespace(is_finished=False), origin)
    return origin


async def apply(app, monkeypatch):
    _, review = await snapshot_review(app, monkeypatch, "tool_profile", "interactive")
    result = await published_rpc(app, monkeypatch, {"id": "apply", "type": controls.PREFIX + "apply", "review": review})
    assert result["status"] == "applied", result["reason"]
    assert controls.mutation_hold(app)
    return cmd(app, "readback", action_id=review["action_id"], review_token=review["review_token"],
               apply_operation_id=result["apply_operation_id"], readback_token=result["readback_token"],
               result_sha256=work.digest(work.encoded(result)))


def verdict(response):
    return response["hookSpecificOutput"]["permissionDecision"]


@pytest.mark.parametrize("app", ["codex"], indirect=True)
async def test_codex_new_call_waits_and_old_outcome_keeps_its_context(armed, monkeypatch):
    app, _ = armed
    owner = stream(app)
    policy = NativePolicy(app)
    policy.bind_turn("thread", "turn", owner)
    calls = []
    hooks = []

    async def authorize(_name, _args, _policy, *, profile, **_kwargs):
        ticket = controls.current_authorization(app)
        calls.append((ticket.origin, ticket.generation, ticket.profile))
        assert profile == ticket.profile

    async def dispatch(_app, event, data, *, captured, profile):
        ticket = controls.current_authorization(app)
        hooks.append((event, ticket.generation, ticket.origin, profile, captured))
        return SimpleNamespace(allowed=True)

    monkeypatch.setattr(app, "_authorize_action", authorize)
    monkeypatch.setattr(hook_host, "dispatch", dispatch)
    monkeypatch.setattr(app, "_hooks_suppressed", False)
    monkeypatch.setattr(hook_host, "context", lambda _app: {"captured": app._active_tool_profile})
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "thread",
             "turn_id": "turn", "tool_use_id": "old", "tool_input": {"command": "echo hello"}}
    assert await policy.handle(event) == {}
    assert calls == [(owner, 0, "strict")]
    readback = await apply(app, monkeypatch)
    fresh = asyncio.create_task(policy.handle(dict(event, tool_use_id="new")))
    await asyncio.sleep(0)
    assert controls._state(app).waiters == {fresh: owner}
    assert verdict(await policy.handle(dict(event, turn_id="older"))) == "deny"
    assert calls == [(owner, 0, "strict")]
    await asyncio.wait_for(policy.completed({"threadId": "thread", "turnId": "turn",
        "item": {"id": "old", "status": "completed", "exitCode": 0}}), 1)
    assert hooks[-1] == ("tool_after", 0, owner, "strict", {"captured": "strict"})
    assert not fresh.done()
    await published_rpc(app, monkeypatch, readback)
    assert await asyncio.wait_for(fresh, 1) == {}
    assert calls[-1] == (owner, 1, "interactive")
    await policy.finish()
    assert not controls._state(app).tickets


@pytest.mark.parametrize("app", ["claude"], indirect=True)
async def test_claude_question_consumes_exact_old_call_new_host_and_native_wait(armed, monkeypatch):
    app, _ = armed
    owner = stream(app)
    app.backend.session = object()
    app.backend.segment_id = "segment"
    bridge = ClaudeTools(app, app.backend, "segment")
    turn = bridge.bind_turn(owner)
    calls = []

    async def authorize(name, *_args, profile, **_kwargs):
        ticket = controls.current_authorization(app)
        assert ticket.profile == profile
        calls.append((name, profile, ticket.origin))

    monkeypatch.setattr(app, "_authorize_action", authorize)
    monkeypatch.setattr(app, "_all_tools", lambda: [{"type": "function", "function": {"name": "chrome"}}])

    async def execute(name, args):
        ticket = controls.current_authorization(app)
        calls.append((name, ticket.profile, ticket.origin))
        return "controlled", True

    monkeypatch.setattr(app, "_execute_tool", execute)
    async def answer(name, args):
        ticket = controls.current_authorization(app)
        return name, ticket.profile, ticket.origin

    monkeypatch.setattr(bridge, "_answer_question", answer)
    args = {"questions": [{"question": "Which?", "options": [{"label": "A"}]}]}
    event = {"tool_name": "AskUserQuestion", "tool_input": args}
    bridge.observed(turn, "question", "AskUserQuestion", args)
    assert verdict(await bridge.pre_tool(event, "question", None)) == "ask"
    readback = await apply(app, monkeypatch)
    bridge.observed(turn, "read", "Read", {})
    native = asyncio.create_task(bridge.pre_tool({"tool_name": "Read", "tool_input": {}}, "read", None))
    host = asyncio.create_task(bridge.host_call("chrome", {}, {}))
    await asyncio.sleep(0)
    assert controls._state(app).waiters == {native: owner, host: None}
    assert calls == [("AskUserQuestion", "strict", owner)]
    request = SimpleNamespace(tool_use_id="question")
    assert isinstance(await bridge.can_use_tool("AskUserQuestion", {}, request), PermissionResultDeny)
    assert await bridge.can_use_tool("AskUserQuestion", args, request) == ("AskUserQuestion", "strict", owner)
    assert (await bridge.can_use_tool("AskUserQuestion", args, request)).behavior == "deny"
    bridge.completed(turn, "question")
    assert not controls._state(app).tickets
    await published_rpc(app, monkeypatch, readback)
    assert verdict(await asyncio.wait_for(native, 1)) == "allow"
    assert not (await asyncio.wait_for(host, 1))["isError"]
    assert ("Read", "interactive", owner) in calls and ("chrome", "interactive", None) in calls
    bridge.finish_turn(turn)
    assert not controls._state(app).tickets


@pytest.mark.parametrize("app", ["codex"], indirect=True)
async def test_child_cancel_before_first_poll_retires_auth_without_running(armed):
    app, _ = armed
    async def never():
        pytest.fail("Cancelled provider callback executed")

    async with controls.authorization(app) as ticket:
        task = controls.child_call(app, never())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert ticket.done.is_set()
    assert not controls._state(app).tickets


@pytest.mark.parametrize("app", ["codex"], indirect=True)
async def test_codex_server_approval_only_reuses_the_matching_item_then_shutdown_joins(armed, monkeypatch):
    from litetui.codex_app_server import AppServerTransport

    app, _ = armed
    owner = stream(app)
    policy = NativePolicy(app)
    policy.bind_turn("thread", "turn", owner)
    sent = []
    decisions = []

    async def send(result):
        sent.append(result)

    async def authorize(name, *args, **kwargs):
        ticket = controls.current_authorization(app)
        decisions.append((name, ticket.profile, ticket.origin))

    monkeypatch.setattr(app, "_authorize_action", authorize)
    monkeypatch.setattr(app, "_hooks_suppressed", True)
    server = SimpleNamespace(native_bridge=SimpleNamespace(policy=policy), send=send)
    transport = AppServerTransport(server, app)
    transport.thread_id, transport.turn_id = "thread", "turn"
    assert await policy.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash",
        "session_id": "thread", "turn_id": "turn", "tool_use_id": "actual"}) == {}
    await apply(app, monkeypatch)
    message = {"id": 10, "method": "item/commandExecution/requestApproval",
        "params": {"threadId": "thread", "turnId": "turn", "itemId": "actual"}}
    await asyncio.wait_for(transport._server_request(message), 1)
    assert decisions == [("bash", "strict", owner), ("codex_approval", "strict", owner)]
    assert sent == [{"id": 10, "result": {"decision": "accept"}}]
    waiting = asyncio.create_task(transport._server_request({**message, "id": 11,
        "params": {**message["params"], "itemId": "different"}}, interrupt_on_stop=False))
    await asyncio.sleep(0)
    assert controls._state(app).waiters == {waiting: owner}
    shutdown = asyncio.create_task(controls.shutdown(app))
    await asyncio.sleep(0)
    assert not shutdown.done()
    await asyncio.wait_for(waiting, 1)
    assert sent[-1] == {"id": 11, "result": {"decision": "cancel"}}
    await policy.finish()
    proof = (await asyncio.wait_for(shutdown, 1))["runtime_controls_shutdown"]
    assert proof["authorization_owners_joined"] and proof["waiter_owners_joined"]
    assert proof["remaining_waiters"] == proof["remaining_authorizations"] == 0
