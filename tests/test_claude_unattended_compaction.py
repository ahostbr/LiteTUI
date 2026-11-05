"""A Claude compaction nobody reviewed, under the native Work frontend.

Autocompaction (and any `_compact` that is not the reviewed F6 action) runs after the turn that
caused it, with no Work execution and no active chat in the Rust frontend. The Textual app lets that
summary turn use its tools, and the STEP 1 store writes are SELF_STORE (never asked). Under Work the
bridge used to deny every native callback of it ("Stale or unowned Claude callback") because the
summary turn never bound a tool turn.

These pin the decisions: the summary turn owns its callbacks, an approval or a question cannot be
hosted by the Rust frontend there (an unattributed approval is an out-of-order event for it), so
those refuse in words, and no host tool runs.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest

from litetui import claude_tools
from litetui.claude_tools import ClaudeTools
from litetui.textfmt import tool_denied

STORE_WRITE = {"file_path": "C:/convos/c1/handoff.md", "content": "in flight"}


class FakeApp:
    def __init__(self, *, refuse_prompts_with=None):
        self.convo_id = "convo-1"
        self.backend = NS(segment_id="seg-1", session=object())
        self.tools_enabled = True
        self.settings = NS(tools_disabled=[], tool_always_allow=[], tool_deny=[])
        self._stop_requested = False
        self._active_tool_profile = "strict"
        self._pending_tool_images = []
        self._approval_waiters = {}
        self._rpc = True
        self.authorized = []
        self.executed = []
        #: What a CONFIRM decision would do if prompting were allowed: the tool needs a human.
        self.needs_human = False

    def _all_tools(self):
        return [{"type": "function", "function": {"name": "convo_search", "description": "x",
                                                 "parameters": {"type": "object"}}}]

    async def _authorize_action(self, name, args, policy, *, profile=None, workspace=None, allow_prompt=True,
                                stop_on_denial=True, prompt_refusal=None):
        self.authorized.append({"name": name, "allow_prompt": allow_prompt, "stop_on_denial": stop_on_denial,
                                "prompt_refusal": prompt_refusal})
        if not self.needs_human:
            return None  # an ALLOW decision (SELF_STORE): no prompt, no event
        if not allow_prompt:
            return tool_denied("profile", name=name, reason=prompt_refusal or "approval unavailable during shutdown"), False
        raise AssertionError("an unattended compaction must never raise an approval event the frontend cannot host")

    async def _execute_tool(self, name, args, *, hooks_enabled=True):
        self.executed.append(name)
        return "done", True


@pytest.fixture
def work_mode(monkeypatch):
    """The Work frontend's authorization door, reduced to what the bridge reads of it."""
    ticket = NS(tools_enabled=True, disabled=frozenset(), profile="strict", state=NS(closing=False))

    @asynccontextmanager
    async def authorization(app, *, origin=None, **kwargs):
        assert origin is None, "nothing attributes an unreviewed compaction's callbacks to a Work execution"
        yield ticket

    monkeypatch.setattr(claude_tools.controls, "enabled", lambda app: True)
    monkeypatch.setattr(claude_tools.controls, "authorization", authorization)
    monkeypatch.setattr(claude_tools.controls, "_state", lambda app: NS(closing=False))
    monkeypatch.setattr(claude_tools.controls, "retain_authorization", lambda app: NS(close=lambda: None))


def bridge_for(app):
    return ClaudeTools(app, app.backend, "seg-1")


async def hook(bridge, name, args, tool_id="toolu_1"):
    return (await bridge.pre_tool({"tool_name": name, "tool_input": args}, tool_id, None))["hookSpecificOutput"]


@pytest.mark.asyncio
async def test_an_unattended_summary_turn_may_use_the_store_writes_the_profile_allows(work_mode):
    app = FakeApp()
    bridge = bridge_for(app)
    turn = bridge.bind_turn(None, unattended=True)
    verdict = await hook(bridge, "Write", STORE_WRITE)
    assert verdict["permissionDecision"] == "allow", verdict
    (asked,) = app.authorized
    assert asked["name"] == "Write" and asked["stop_on_denial"] is False
    bridge.finish_turn(turn)


@pytest.mark.asyncio
async def test_a_native_tool_that_needs_a_human_is_refused_in_words_and_raises_no_approval(work_mode):
    app = FakeApp()
    app.needs_human = True
    bridge = bridge_for(app)
    bridge.bind_turn(None, unattended=True)
    verdict = await hook(bridge, "Bash", {"command": "git status"})
    assert verdict["permissionDecision"] == "deny"
    assert "unattended" in verdict["permissionDecisionReason"].lower()
    assert "shutdown" not in verdict["permissionDecisionReason"]
    (asked,) = app.authorized
    assert asked["allow_prompt"] is False and asked["prompt_refusal"]


@pytest.mark.asyncio
async def test_a_question_cannot_be_hosted_by_an_unattended_compaction(work_mode):
    app = FakeApp()
    bridge = bridge_for(app)
    bridge.bind_turn(None, unattended=True)
    question = {"questions": [{"question": "Which?", "header": "h", "multiSelect": False,
                               "options": [{"label": "a"}, {"label": "b"}]}]}
    verdict = await hook(bridge, "AskUserQuestion", question)
    assert verdict["permissionDecision"] == "deny" and "unattended" in verdict["permissionDecisionReason"].lower()
    denied = await bridge.can_use_tool("AskUserQuestion", question, NS(tool_use_id="toolu_1"))
    assert "unattended" in denied.message.lower()


@pytest.mark.asyncio
async def test_no_host_tool_runs_in_an_unattended_compaction(work_mode):
    app = FakeApp()
    bridge = bridge_for(app)
    bridge.bind_turn(None, unattended=True)
    result = await bridge.host_call("convo_search", {}, app._all_tools()[0]["function"])
    assert result["isError"] and "unattended" in result["content"][0]["text"].lower()
    assert app.executed == []


@pytest.mark.asyncio
async def test_finishing_the_turn_ends_the_authority_and_a_later_callback_is_stale(work_mode):
    app = FakeApp()
    bridge = bridge_for(app)
    turn = bridge.bind_turn(None, unattended=True)
    bridge.finish_turn(turn)
    verdict = await hook(bridge, "Write", STORE_WRITE)
    assert verdict["permissionDecision"] == "deny" and "Stale" in verdict["permissionDecisionReason"]
    # A later ordinary turn is not unattended: its approvals are the chat's own to host.
    bridge.bind_turn(None)
    app.authorized.clear()
    assert (await hook(bridge, "Write", STORE_WRITE))["permissionDecision"] == "allow"
    assert app.authorized[0]["allow_prompt"] is True and app.authorized[0]["prompt_refusal"] is None


@pytest.mark.asyncio
async def test_without_the_work_frontend_nothing_changes(monkeypatch):
    monkeypatch.setattr(claude_tools.controls, "enabled", lambda app: False)
    app = FakeApp()
    bridge = bridge_for(app)
    verdict = await hook(bridge, "Write", STORE_WRITE)  # no bound turn: Textual never needed one
    assert verdict["permissionDecision"] == "allow"
    assert app.authorized[0]["allow_prompt"] is True and app.authorized[0]["prompt_refusal"] is None
