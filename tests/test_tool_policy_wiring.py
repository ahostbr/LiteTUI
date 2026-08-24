from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import scheduler
from litetui.settings import Settings
from litetui.tool_approval import ToolApprovalScreen
from litetui.tool_policy import (
    INTERACTIVE,
    NETWORK_READ_POLICY,
    READ_POLICY,
    SCHEDULED,
    SHELL_POLICY,
    WRITE_POLICY,
)


class _Policies:
    def __init__(self, policy):
        self.policy = policy

    def policy_for(self, _name):
        return self.policy


def _host(policy, run, *, profile=INTERACTIVE, approve=True):
    seen = []

    async def confirm(screen):
        seen.append(screen)
        return approve

    return SimpleNamespace(
        # T073 put a toggle check ahead of the policy gate, so a host that
        # models the POLICY path has to say tools are on. Stated rather than
        # defaulted: `_execute_tool` reads the attribute directly instead of
        # `getattr(..., True)`, because a guard whose missing input means
        # "permit" is not a guard.
        tools_enabled=True,
        _active_tool_profile=profile,
        _dispatch_for=lambda _name: run,
        plugins=_Policies(policy),
        push_screen_wait=confirm,
    ), seen


@pytest.mark.asyncio
async def test_read_executes_without_a_modal():
    calls = []
    host, screens = _host(READ_POLICY, lambda args: calls.append(args) or "read")
    result, ok = await app_mod.LiteTUI._execute_tool(host, "read", {"path": "x"})
    assert (result, ok) == ("read", True)
    assert calls == [{"path": "x"}]
    assert screens == []


@pytest.mark.asyncio
async def test_sensitive_interactive_call_requires_one_host_decision():
    calls = []
    host, screens = _host(
        SHELL_POLICY,
        lambda args: calls.append(args) or "ran",
        approve=True,
    )
    result, ok = await app_mod.LiteTUI._execute_tool(
        host, "powershell", {"command": "git status"}
    )
    assert (result, ok) == ("ran", True)
    assert len(screens) == 1 and isinstance(screens[0], ToolApprovalScreen)
    assert calls == [{"command": "git status"}]


@pytest.mark.asyncio
async def test_denied_modal_and_scheduled_profile_never_execute(tmp_path):
    called = []
    host, screens = _host(
        WRITE_POLICY,
        lambda args: called.append(args) or "wrote",
        approve=False,
    )
    result, ok = await app_mod.LiteTUI._execute_tool(
        host, "write", {"path": str(tmp_path / "x"), "content": "x"}
    )
    assert not ok and "denied by user" in result
    assert len(screens) == 1 and called == []

    scheduled, scheduled_screens = _host(
        NETWORK_READ_POLICY,
        lambda args: called.append(args) or "fetched",
        profile=SCHEDULED,
    )
    result, ok = await app_mod.LiteTUI._execute_tool(
        scheduled, "web_fetch", {"url": "https://example.com"}
    )
    assert not ok and "scheduled profile" in result
    assert scheduled_screens == [] and called == []


def test_cron_profile_rides_with_queued_and_idle_turns(monkeypatch):
    monkeypatch.setattr(app_mod.sched_mod, "save", lambda *_a, **_k: None)
    job = scheduler.Job(prompt="inspect", schedule="@daily")

    queued = SimpleNamespace(
        jobs=[job],
        _chat_running=lambda: True,
        _user_bubble=lambda *_a, **_k: None,
        _pending_input=[],
        _handle_command=lambda _text: None,
    )
    app_mod.LiteTUI._fire_job(queued, job)
    assert queued._pending_input[0]["tool_profile"] == SCHEDULED

    streamed = []
    idle = SimpleNamespace(
        jobs=[job],
        _chat_running=lambda: False,
        _user_bubble=lambda *_a, **_k: None,
        _pending_input=[],
        _handle_command=lambda _text: None,
        _append=lambda msg: streamed.append(msg),
        _stream=lambda: streamed.append("stream"),
        _active_tool_profile=INTERACTIVE,
    )
    app_mod.LiteTUI._fire_job(idle, job)
    assert idle._active_tool_profile == SCHEDULED
    assert streamed[-1] == "stream"


def test_midturn_queue_adopts_the_delivered_items_profile():
    appended = []
    host = SimpleNamespace(
        _pending_input=[
            {"content": "scheduled", "text": "scheduled", "tool_profile": SCHEDULED}
        ],
        _stop_requested=False,
        _active_tool_profile=INTERACTIVE,
        settings=Settings(),
        _append=lambda msg: appended.append(msg),
    )
    assert app_mod.LiteTUI._deliver_queued_input(host)
    assert host._active_tool_profile == SCHEDULED
    assert appended == [{"role": "user", "content": "scheduled"}]
