from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import scheduler
from litetui.settings import Settings
from litetui.tool_approval import DENIED, ONCE, ToolApprovalScreen
from litetui.tool_policy import (
    AUTONOMOUS,
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


def _host(policy, run, *, profile=INTERACTIVE, approve=ONCE):
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
        # 1146f68 (rpc T3) emits a tool_call at the seam; the double
        # predates it. Recorded, not swallowed — see test_tools_disabled.
        _rpc_emit=lambda data: None,
        # T594 put a headless gate on the turn path; this double is not a
        # headless child, and getattr's default cannot help a SimpleNamespace
        # that raises rather than returning a default.
        _rpc=False,
        _active_tool_profile=profile,
        _dispatch_for=lambda _name: run,
        plugins=_Policies(policy),
        push_screen_wait=confirm,
        # Standing allow/deny rules are read straight off settings, by the same
        # reasoning as tools_enabled above: a missing rule set must not mean
        # "no rules apply" by accident. Defaults are empty, so this host models
        # a machine where the human has never answered "always".
        settings=Settings(),
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
        approve=ONCE,
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
        approve=DENIED,
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


def test_the_SET_level_rides_with_queued_and_idle_cron_turns(monkeypatch):
    """The queued/idle mechanism is unchanged; WHERE the profile comes from is.

    ⚠️ RENAMED FROM `test_cron_profile_rides_...`, because "the cron profile"
    was `job.tool_profile` and that is no longer consulted. Ryan ruled: "cron
    and loops run at same set profile level my ruling". The mechanism this
    test protects -- that the profile reaches BOTH the queued item and
    `_active_tool_profile` on the idle path -- is worth keeping and is why the
    body survives nearly intact.

    🔴 THE SETTING IS AUTONOMOUS AND THE JOB IS SCHEDULED, DELIBERATELY. With
    both set to `scheduled` this test would pass whether the ruling was
    implemented or not -- it would be asserting a value two sources agree on
    and could not say which one it came from.
    """
    monkeypatch.setattr(app_mod.sched_mod, "save", lambda *_a, **_k: None)
    job = scheduler.Job(prompt="inspect", schedule="@daily")
    assert job.tool_profile == SCHEDULED, "premise: the job's own answer differs"

    settings = Settings()
    settings.tool_policy_profile = AUTONOMOUS

    queued = SimpleNamespace(
        jobs=[job],
        settings=settings,
        _chat_running=lambda: True,
        _user_bubble=lambda *_a, **_k: None,
        _pending_input=[],
        _handle_command=lambda _text: None,
    )
    app_mod.LiteTUI._fire_job(queued, job)
    assert queued._pending_input[0]["tool_profile"] == AUTONOMOUS

    streamed = []
    idle = SimpleNamespace(
        jobs=[job],
        settings=settings,
        _chat_running=lambda: False,
        _user_bubble=lambda *_a, **_k: None,
        _pending_input=[],
        _handle_command=lambda _text: None,
        _append=lambda msg: streamed.append(msg),
        _stream=lambda: streamed.append("stream"),
        _active_tool_profile=INTERACTIVE,
    )
    app_mod.LiteTUI._fire_job(idle, job)
    assert idle._active_tool_profile == AUTONOMOUS
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
