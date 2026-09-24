"""Behaviour of the Claude authority boundary.

These are contract tests, not smoke tests: each one names a way the bridge
could let something through, and fails if it does. The app is a fake because
the point is what `claude_tools` DECIDES — the real dispatcher is exercised by
the host suites — but every attribute the fake exposes is one the real app
actually has, and the authorize/execute doors record what they were handed so
a test can assert the arguments rather than the outcome alone.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from litetui import ask_user_question, paths, question_result, tool_policy
from litetui.claude_tools import (
    HOST_TOOLS,
    NATIVE_TOOLS,
    ClaudeTools,
    _deadlines,
    native_policy,
)

INVENTORY = [
    {"type": "function", "function": {
        "name": "chrome", "description": "browser",
        "parameters": {"type": "object", "properties": {"action": {"type": "string"}},
                       "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "harness", "description": "fleet",
        "parameters": {"type": "object", "properties": {"action": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "read_file", "description": "host read",
                                      "parameters": {"type": "object"}}},
]


class FakeSettings:
    def __init__(self, disabled=()):
        self.tools_disabled = list(disabled)
        self.tool_always_allow = []
        self.tool_deny = []


class FakeBackend:
    def __init__(self, segment_id="seg-1"):
        self.segment_id = segment_id


class FakeApp:
    def __init__(self, *, tools_enabled=True, disabled=(), authorize=None,
                 execute=None, inventory=None):
        self.convo_id = "convo-1"
        self.backend = FakeBackend()
        self.tools_enabled = tools_enabled
        self.settings = FakeSettings(disabled)
        self._stop_requested = False
        self._active_tool_profile = "autonomous"
        self._pending_tool_images = []
        self._approval_waiters = {}
        self._rpc = True  # keeps question_slot off the Textual presentation lock
        self.authorized = []
        self.executed = []
        self._authorize = authorize
        self._execute = execute
        self._inventory = INVENTORY if inventory is None else inventory

    def _all_tools(self):
        return self._inventory

    async def _authorize_action(self, name, args, policy, *, profile=None, workspace=None,
                                allow_prompt=True, stop_on_denial=True):
        self.authorized.append({"name": name, "args": args, "policy": policy,
                                "profile": profile, "workspace": workspace,
                                "stop_on_denial": stop_on_denial})
        if self._authorize is None:
            return None
        return await self._authorize(name, args, policy)

    async def _execute_tool(self, name, args, *, hooks_enabled=True):
        self.executed.append((name, dict(args)))
        if self._execute is None:
            return "done", True
        return await self._execute(name, args)


def make(app=None, **kwargs):
    app = app if app is not None else FakeApp()
    return ClaudeTools(app, app.backend, app.backend.segment_id, **kwargs), app


async def pre(bridge, name, args=None):
    return await bridge.pre_tool(
        {"tool_name": name, "tool_input": {} if args is None else args}, "toolu_1", None
    )


def verdict(out):
    return out["hookSpecificOutput"]["permissionDecision"]


def because(out):
    return out["hookSpecificOutput"].get("permissionDecisionReason", "")


def text_of(result):
    return "".join(part["text"] for part in result["content"] if part["type"] == "text")


# -- what must never get through ---------------------------------------


@pytest.mark.asyncio
async def test_an_unmapped_native_tool_is_denied():
    bridge, _ = make()
    out = await pre(bridge, "Task", {"prompt": "spawn a sub-agent"})
    assert verdict(out) == "deny"
    assert "Unmapped native tool Task" in because(out)


@pytest.mark.asyncio
async def test_native_background_work_is_denied():
    bridge, _ = make()
    out = await pre(bridge, "Bash", {"command": "ls", "run_in_background": True})
    assert verdict(out) == "deny"
    assert "background" in because(out)


@pytest.mark.asyncio
async def test_a_host_tool_outside_the_bridge_inventory_is_denied():
    bridge, app = make()
    out = await pre(bridge, "mcp__litetui__read_file", {})
    assert verdict(out) == "deny"
    assert "approved bridge inventory" in because(out)
    assert app.authorized == []


@pytest.mark.asyncio
async def test_a_tool_from_another_mcp_server_falls_through_to_the_native_refusal():
    bridge, _ = make()
    out = await pre(bridge, "mcp__somethingelse__write_everywhere", {})
    assert verdict(out) == "deny"


@pytest.mark.asyncio
async def test_calls_are_denied_once_tools_are_switched_off():
    bridge, _ = make(FakeApp(tools_enabled=False))
    assert verdict(await pre(bridge, "Read", {"path": "a.txt"})) == "deny"


@pytest.mark.asyncio
async def test_a_stale_segment_cannot_authorize_anything():
    bridge, app = make()
    app.backend = FakeBackend("seg-2")
    out = await pre(bridge, "Read", {"path": "a.txt"})
    assert verdict(out) == "deny"
    assert app.authorized == []


# -- the user's switched-off list reaches native tools too --------------


@pytest.mark.asyncio
@pytest.mark.parametrize("called", ["Bash", "mcp__litetui__chrome"])
async def test_a_switched_off_tool_is_denied_at_the_gate(called):
    # Withholding the name from the offer is not enough on its own: a model
    # imitating its own transcript still calls a tool it used earlier in the
    # same conversation. app._execute_tool enforces the same list, but only a
    # host call ever reaches it.
    bridge, app = make(FakeApp(disabled=["Bash", "chrome"]))
    out = await pre(bridge, called, {"command": "ls", "action": "status"})
    assert verdict(out) == "deny"
    assert "switched off" in because(out)
    assert app.authorized == []


def test_a_switched_off_native_tool_is_not_offered():
    bridge, _ = make(FakeApp(disabled=["Bash"]))
    offered = bridge.sdk_options()["tools"]
    assert "Bash" not in offered
    assert "Read" in offered


def test_no_native_tools_are_offered_when_tools_are_off():
    bridge, _ = make(FakeApp(tools_enabled=False))
    options = bridge.sdk_options()
    assert options["tools"] == []
    assert options["mcp_servers"] == {}


# -- which workspace a write is judged against --------------------------


@pytest.mark.asyncio
async def test_authorization_uses_the_configured_workspace(tmp_path):
    bridge, app = make(workspace=tmp_path)
    await pre(bridge, "Write", {"file_path": str(tmp_path / "x.py"), "content": "x"})
    assert app.authorized[0]["workspace"] == tmp_path


@pytest.mark.asyncio
async def test_the_workspace_is_never_the_process_cwd(tmp_path, monkeypatch):
    # classify_write splits WORKSPACE_WRITE from EXTERNAL_WRITE by containment,
    # so reading the launch directory made the authority a native write needed
    # depend on where LiteTUI happened to be started from — and differ from the
    # root every host tool is judged against.
    monkeypatch.chdir(tmp_path)
    bridge, app = make()
    await pre(bridge, "Write", {"file_path": "x.py", "content": "x"})
    assert app.authorized[0]["workspace"] == paths.ROOT
    assert app.authorized[0]["workspace"] != Path.cwd()


def test_native_policy_maps_arguments_onto_the_keys_the_classifiers_read():
    # tool_policy.classify_write reads args["path"]; classify_shell reads
    # args["command"]. A policy handed the wrong key classifies nothing.
    policy, args = native_policy("Edit", {"file_path": "C:/x/y.py", "old": "a"})
    assert policy is tool_policy.WRITE_POLICY
    assert args["path"] == "C:/x/y.py"
    assert args["old"] == "a"

    policy, args = native_policy("Bash", {"command": "rm -rf /"})
    assert policy is tool_policy.SHELL_POLICY
    assert args["command"] == "rm -rf /"

    assert native_policy("Read", {})[0] is tool_policy.READ_POLICY
    assert native_policy("WebFetch", {})[0] is tool_policy.NETWORK_READ_POLICY
    assert native_policy("Task", {})[0] is None


# -- the gate must always answer, and answer first ----------------------


def test_the_decision_deadline_is_strictly_below_the_hook_timeout():
    from litetui.tool_approval import APPROVAL_TIMEOUT_S

    decision_s, hook_s = _deadlines()
    assert decision_s < hook_s
    # The RPC approval path is allowed its full window before we give up on it.
    assert decision_s > APPROVAL_TIMEOUT_S


def test_the_hook_matcher_carries_that_timeout_rather_than_the_sdk_default():
    bridge, _ = make()
    _, hook_s = _deadlines()
    matcher = bridge.sdk_options()["hooks"]["PreToolUse"][0]
    assert matcher.timeout == hook_s
    assert matcher.timeout != 60  # the documented HookMatcher default


@pytest.mark.asyncio
async def test_a_decision_nobody_answers_is_denied_with_a_reason(monkeypatch):
    monkeypatch.setattr("litetui.claude_tools._deadlines", lambda: (0.05, 0.5))

    async def never(name, args, policy):
        await asyncio.sleep(30)

    bridge, _ = make(FakeApp(authorize=never))
    out = await asyncio.wait_for(pre(bridge, "Bash", {"command": "ls"}), 5)
    assert verdict(out) == "deny"
    assert "within" in because(out)


@pytest.mark.asyncio
async def test_stop_releases_a_decision_blocked_on_a_human():
    # Pressing stop while an approval dialog is open used to leave the hook
    # unanswered, which left the SDK unable to drain and the turn reported as
    # "delivery is uncertain" — over a dialog nobody had closed.
    started = asyncio.Event()

    async def blocks(name, args, policy):
        started.set()
        await asyncio.sleep(30)

    bridge, _ = make(FakeApp(authorize=blocks))
    pending = asyncio.ensure_future(pre(bridge, "Bash", {"command": "ls"}))
    await asyncio.wait_for(started.wait(), 5)
    bridge.stop()
    began = time.perf_counter()
    out = await asyncio.wait_for(pending, 5)
    elapsed = time.perf_counter() - began
    assert verdict(out) == "deny"
    assert "cancelled" in because(out)
    # 🔴 THE TIMING IS PART OF THE ASSERTION. Without it this test passed even
    # with stop()'s cancellation removed: the wait_for above cancelled `pending`
    # itself, _decide turned that into a tidy denial, and Python 3.11's wait_for
    # returns fut.result() when a task swallows its cancellation — so a five
    # second hang read as a pass. Both ends are fixed; this holds that line.
    assert elapsed < 1


@pytest.mark.asyncio
async def test_cancelling_the_hook_itself_is_not_swallowed():
    # The other side of the clause above. stop() cancelling the DECISION must
    # produce an answer, but the SDK or a shutdown cancelling the CALLBACK must
    # tear it down — a coroutine that answers a caller which has already gone
    # is an un-cancellable hook, and the same `except CancelledError` covers
    # both cases.
    started = asyncio.Event()

    async def blocks(name, args, policy):
        started.set()
        await asyncio.sleep(30)

    bridge, _ = make(FakeApp(authorize=blocks))
    pending = asyncio.ensure_future(pre(bridge, "Bash", {"command": "ls"}))
    await asyncio.wait_for(started.wait(), 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


@pytest.mark.asyncio
async def test_stop_still_settles_the_host_approval_waiters():
    # The host approval API is deliberately untouched by the cancellation
    # above; this is the arm that says so.
    from litetui.tool_approval import DENIED

    bridge, app = make()
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    app._approval_waiters = {"req-1": waiter}
    bridge.stop()
    assert waiter.done()
    assert waiter.result() is DENIED


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    bridge, _ = make()
    bridge.stop()
    bridge.stop()
    assert verdict(await pre(bridge, "Read", {"path": "a"})) == "deny"


# -- what the gate does let through -------------------------------------


@pytest.mark.asyncio
async def test_an_authorized_native_call_is_allowed_and_authorized_exactly_once():
    bridge, app = make()
    out = await pre(bridge, "Read", {"path": "a.txt"})
    assert verdict(out) == "allow"
    assert len(app.authorized) == 1
    assert app.authorized[0]["stop_on_denial"] is False
    assert app.authorized[0]["profile"] == "autonomous"


@pytest.mark.asyncio
async def test_a_policy_refusal_is_reported_verbatim():
    async def refuse(name, args, policy):
        return ("[denied] workspace write outside the project", False)

    bridge, _ = make(FakeApp(authorize=refuse))
    out = await pre(bridge, "Write", {"file_path": "x", "content": "y"})
    assert verdict(out) == "deny"
    assert because(out) == "[denied] workspace write outside the project"


@pytest.mark.asyncio
async def test_a_question_is_routed_to_the_answering_callback_not_merely_allowed():
    bridge, _ = make()
    out = await pre(bridge, "AskUserQuestion", {"questions": []})
    assert verdict(out) == "ask"


# -- questions ----------------------------------------------------------

GOOD_QUESTION = {"questions": [{
    "header": "Deploy", "question": "Ship it?", "multiSelect": False,
    "options": [{"label": "Yes", "description": "go"}, {"label": "No", "description": "stop"}],
}]}


def answer_with(payload):
    def run(args, app):
        question_result.publish(payload)
        return ""
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"questions": [{"header": "h", "options": [{"label": "Yes"}]}]},          # no question text
    {"questions": [{"question": "q", "options": [{"description": "d"}]}]},    # option without a label
    {"questions": [{"question": "q"}]},                                        # no options at all
    {"questions": "not a list"},
    {"questions": [None]},
    {},
])
async def test_a_malformed_question_is_denied_with_a_stated_reason(payload):
    # Raising here would still be fail-closed — the SDK turns it into a control
    # error — but it puts raw exception text on the wire and gives the user
    # nothing they can act on.
    bridge, _ = make()
    result = await bridge.can_use_tool("AskUserQuestion", payload, None)
    assert result.behavior == "deny"
    assert result.message
    assert "KeyError" not in result.message


@pytest.mark.asyncio
async def test_a_question_that_cannot_be_presented_is_denied(monkeypatch):
    def explode(args, app):
        raise RuntimeError("no screen")

    monkeypatch.setattr(ask_user_question, "run", explode)
    bridge, _ = make()
    result = await bridge.can_use_tool("AskUserQuestion", GOOD_QUESTION, None)
    assert result.behavior == "deny"
    assert "could not be presented" in result.message


@pytest.mark.asyncio
async def test_anything_other_than_a_question_is_refused_by_the_answering_callback():
    # If PreToolUse is ever bypassed, this is the fallback, and it refuses.
    bridge, _ = make()
    result = await bridge.can_use_tool("Bash", {"command": "ls"}, None)
    assert result.behavior == "deny"


@pytest.mark.asyncio
async def test_an_unsubmitted_question_is_not_an_answer(monkeypatch):
    monkeypatch.setattr(ask_user_question, "run", answer_with(
        {"action": "aborted", "questions": [{"answered": False, "options": [], "selected": []}]}
    ))
    bridge, _ = make()
    result = await bridge.can_use_tool("AskUserQuestion", GOOD_QUESTION, None)
    assert result.behavior == "deny"
    assert "explicitly submitted" in result.message


@pytest.mark.asyncio
async def test_a_submitted_question_is_allowed_with_its_structured_answer(monkeypatch):
    monkeypatch.setattr(ask_user_question, "run", answer_with({
        "action": "submit",
        "questions": [{"answered": True, "selected": [0],
                       "options": [{"title": "Yes"}, {"title": "No"}], "note": ""}],
    }))
    bridge, _ = make()
    result = await bridge.can_use_tool("AskUserQuestion", GOOD_QUESTION, None)
    assert result.behavior == "allow"
    assert result.updated_input["answers"] == {"Ship it?": "Yes"}


# -- the host bridge ----------------------------------------------------

CHROME_SPEC = INVENTORY[0]["function"]


@pytest.mark.asyncio
async def test_host_arguments_are_validated_against_the_published_schema():
    bridge, app = make()
    result = await bridge.host_call("chrome", {"action": 7}, CHROME_SPEC)
    assert result["isError"] is True
    assert "Invalid host arguments" in text_of(result)
    assert app.executed == []


@pytest.mark.asyncio
async def test_host_arguments_must_be_an_object():
    bridge, app = make()
    result = await bridge.host_call("chrome", ["action"], CHROME_SPEC)
    assert result["isError"] is True
    assert app.executed == []


@pytest.mark.asyncio
async def test_a_tool_outside_the_bridge_set_never_reaches_the_dispatcher():
    bridge, app = make()
    result = await bridge.host_call("read_file", {}, INVENTORY[2]["function"])
    assert result["isError"] is True
    assert "not bridged" in text_of(result)
    assert app.executed == []


@pytest.mark.asyncio
async def test_a_name_that_left_the_inventory_after_approval_is_refused():
    # The window this closes: approval is granted, a hook or the human changes
    # the inventory, and the call would otherwise still run.
    bridge, app = make()
    app._inventory = [t for t in INVENTORY if t["function"]["name"] != "chrome"]
    result = await bridge.host_call("chrome", {"action": "status"}, CHROME_SPEC)
    assert result["isError"] is True
    assert "inventory changed" in text_of(result)
    assert app.executed == []


@pytest.mark.asyncio
async def test_a_stopped_bridge_refuses_host_calls():
    bridge, app = make()
    bridge.stop()
    result = await bridge.host_call("chrome", {"action": "status"}, CHROME_SPEC)
    assert result["isError"] is True
    assert app.executed == []


@pytest.mark.asyncio
async def test_an_approved_host_call_executes_once_and_returns_its_result():
    bridge, app = make()
    result = await bridge.host_call("chrome", {"action": "status"}, CHROME_SPEC)
    assert result["isError"] is False
    assert text_of(result) == "done"
    assert app.executed == [("chrome", {"action": "status"})]


@pytest.mark.asyncio
async def test_a_failing_host_tool_is_reported_as_an_error_not_a_result():
    async def fails(name, args):
        return "[denied] profile refused", False

    bridge, _ = make(FakeApp(execute=fails))
    result = await bridge.host_call("chrome", {"action": "status"}, CHROME_SPEC)
    assert result["isError"] is True
    assert "[denied] profile refused" in text_of(result)


@pytest.mark.asyncio
async def test_a_host_result_carries_only_the_images_its_own_call_staged():
    async def stages(name, args):
        app._pending_tool_images.append(("shot.png", "MINE"))
        return "ok", True

    app = FakeApp()
    app._execute = stages
    app._pending_tool_images = [("stale.png", "SOMEONE-ELSES")]
    bridge, _ = make(app)
    result = await bridge.host_call("chrome", {"action": "screenshot"}, CHROME_SPEC)
    images = [part["data"] for part in result["content"] if part["type"] == "image"]
    assert images == ["MINE"]
    # And the buffer it borrowed is handed back exactly as it was found.
    assert app._pending_tool_images == [("stale.png", "SOMEONE-ELSES")]


@pytest.mark.asyncio
async def test_host_calls_are_serialized():
    order = []

    async def slow(name, args):
        order.append(f"start:{args['action']}")
        await asyncio.sleep(0.02)
        order.append(f"end:{args['action']}")
        return "ok", True

    bridge, _ = make(FakeApp(execute=slow))
    await asyncio.gather(
        bridge.host_call("chrome", {"action": "a"}, CHROME_SPEC),
        bridge.host_call("chrome", {"action": "b"}, CHROME_SPEC),
    )
    assert order in (["start:a", "end:a", "start:b", "end:b"],
                     ["start:b", "end:b", "start:a", "end:a"])


def test_only_bridged_host_tools_are_published_to_the_sdk():
    bridge, _ = make()
    options = bridge.sdk_options()
    assert set(NATIVE_TOOLS) >= set(options["tools"])
    assert "litetui" in options["mcp_servers"]
    # read_file is in the app inventory but not in the bridge set.
    assert "read_file" not in HOST_TOOLS
