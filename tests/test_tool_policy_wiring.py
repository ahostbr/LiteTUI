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
    READ_POLICY,
    SHELL_POLICY,
    STRICT,
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
        # commit-A loop breaker: _execute_tool consults _loop_refusal at the
        # seam, then _loop_record/_loop_warn after the run; this double
        # predates all three. Stated no-ops, not a getattr guard — a guard
        # whose missing input silently disables the breaker in a double is a
        # regression, and this host models the POLICY path, not loop-breaking.
        _loop_refusal=lambda name, args: None,
        _loop_record=lambda name, args, result: None,
        _loop_warn=lambda name, args, result: result,
        # e3957d9 (shot auto-stage) ends _execute_tool through
        # _maybe_stage_shot; this double predates it too (verified red on
        # clean HEAD — not a regression from the loop breaker). Stated
        # pass-through: none of these tools is a chrome shot, so staging is
        # a no-op by construction.
        _maybe_stage_shot=lambda name, args, result: result,
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
async def test_sensitive_strict_call_requires_one_host_decision():
    """STRICT is the approval level (Ryan's ruling 2026-09-19: "strict mode ...
    auto ... none at all"): ordinary process execution is in its confirm set,
    so the SAME call that runs freely under INTERACTIVE stops here for exactly
    one host decision.

    This is the arm the earlier `test_sensitive_interactive_call_requires_one_
    host_decision` asserted under INTERACTIVE — where it was red, because
    INTERACTIVE no longer confirms non-destructive shell. Retargeted, not
    relaxed: the modal-waits-before-execution contract is now proven in the
    mode that actually owns it.
    """
    calls = []
    host, screens = _host(
        SHELL_POLICY,
        lambda args: calls.append(args) or "ran",
        profile=STRICT,
        approve=ONCE,
    )
    result, ok = await app_mod.LiteTUI._execute_tool(
        host, "powershell", {"command": "git status"}
    )
    assert (result, ok) == ("ran", True)
    assert len(screens) == 1 and isinstance(screens[0], ToolApprovalScreen)
    assert calls == [{"command": "git status"}]


@pytest.mark.asyncio
async def test_interactive_non_destructive_shell_call_needs_no_host_decision():
    """INTERACTIVE is the middle level: a NON-destructive shell call runs
    without any modal. PROCESS_EXECUTION is in its allow set, and a call that
    stops here to ask would be the stale expectation the 2026-09-19 ruling
    removed. The `screens == []` assertion is the point — an approval test
    that only checks the result cannot distinguish "ran" from "asked, then
    ran".
    """
    calls = []
    host, screens = _host(
        SHELL_POLICY,
        lambda args: calls.append(args) or "ran",
        profile=INTERACTIVE,
    )
    result, ok = await app_mod.LiteTUI._execute_tool(
        host, "powershell", {"command": "git status"}
    )
    assert (result, ok) == ("ran", True)
    assert screens == [], "a non-destructive call must not open a modal under INTERACTIVE"
    assert calls == [{"command": "git status"}]


@pytest.mark.asyncio
async def test_interactive_destructive_shell_call_still_requires_one_host_decision():
    """The INTERACTIVE escape hatch: a DESTRUCTIVE-arg call still confirms,
    even though ordinary shell is allowed. classify_shell matches the
    destructive pattern and returns destructive_irreversible, which IS in
    INTERACTIVE's confirm set — so one approval of `git status` never
    authorises `rm -rf`.
    """
    calls = []
    host, screens = _host(
        SHELL_POLICY,
        lambda args: calls.append(args) or "ran",
        profile=INTERACTIVE,
        approve=DENIED,
    )
    result, ok = await app_mod.LiteTUI._execute_tool(
        host, "powershell", {"command": "rm -rf ./build"}
    )
    assert not ok and "denied by user" in result
    assert len(screens) == 1 and isinstance(screens[0], ToolApprovalScreen)
    assert calls == [], "a destructive call ran without its own approval"


@pytest.mark.asyncio
async def test_denied_modal_and_unattended_confirm_never_execute(tmp_path):
    """Was `..._and_scheduled_profile_never_execute`. The `scheduled` read-only
    floor is gone (Ryan 2026-09-24: "remove scheduled completely it makes no
    sense to me ... make interactive ask only for dangerous cmds any deletions
    or zip expansions weird procc runs that arent its tools and dangerous cmds
    threw PS and bash"). Its replacement is asserted here: an unattended turn
    KEEPS interactive -- ordinary calls run -- and only a CONFIRM, which nobody
    can answer, is refused in words, with no modal."""
    called = []
    host, screens = _host(
        WRITE_POLICY,
        lambda args: called.append(args) or "wrote",
        profile=STRICT,
        approve=DENIED,
    )
    result, ok = await app_mod.LiteTUI._execute_tool(
        host, "write", {"path": str(tmp_path / "x"), "content": "x"}
    )
    assert not ok and "denied by user" in result
    assert len(screens) == 1 and called == []

    unattended, unattended_screens = _host(
        SHELL_POLICY,
        lambda args: called.append(args) or "ran",
        profile=INTERACTIVE,
        approve=ONCE,  # a modal, if one opened, would approve: it must not open
    )
    unattended._hook_source = "harness"
    result, ok = await app_mod.LiteTUI._execute_tool(
        unattended, "powershell", {"command": "rm -rf ./build"}
    )
    assert not ok and "nobody is here to confirm" in result
    assert "deletion" in result
    assert unattended_screens == [] and called == []

    # CONTROL: the same unattended turn still has interactive's powers.
    result, ok = await app_mod.LiteTUI._execute_tool(
        unattended, "powershell", {"command": "git status"}
    )
    assert (result, ok) == ("ran", True)
    assert unattended_screens == []


def test_a_cron_turn_is_AUTONOMOUS_whatever_the_conversation_is_set_to(monkeypatch):
    """A scheduled turn resolves to AUTONOMOUS, ignoring both other sources.

    ⚠️ RENAMED TWICE NOW, AND THE SECOND RENAME IS THE INTERESTING ONE.
    It was `test_cron_profile_rides_...` when `job.tool_profile` decided; then
    `test_the_SET_level_rides_with_queued_and_idle_cron_turns` when Ryan ruled
    the conversation setting decided. Ryan's ruling of 2026-09-11 removed the
    choice entirely -- "for a cron it has to run auto because nobody will be
    there to hitl it" -- so `_fire_job` hardcodes AUTONOMOUS and the SET level
    no longer rides with anything. The old NAME asserted the old ruling, which
    is the whole defect T595 names: production was right and the test's title
    described a policy that had been overturned.

    🔴 THE SETTING IS DELIBERATELY *NOT* AUTONOMOUS, AND THAT IS THE FIX.
    The previous version set `settings.tool_policy_profile = AUTONOMOUS` and
    asserted the delivered profile was AUTONOMOUS -- while `_fire_job`
    hardcodes AUTONOMOUS. Two sources agreeing on one value, so the assertion
    could not say which one it came from and would have passed with the
    hardcode deleted. Its own docstring warned about exactly this shape for a
    different pair of values, one paragraph above where it then did it:
        AN ASSERTION SATISFIABLE BY THE WRONG ANSWER IS NOT A MEASUREMENT.
    Setting the conversation to `interactive` makes the three candidate
    sources -- the job's field (strict), the conversation setting
    (interactive) and the hardcode (autonomous) -- mutually distinct, so the
    asserted value names its own origin.

    📌 2026-09-24: `scheduled` was removed and `Job.tool_profile` now DEFAULTS
    to AUTONOMOUS, which would collapse the job's field onto the hardcode. The
    job is therefore built with STRICT explicitly, keeping all three distinct.
    """
    monkeypatch.setattr(app_mod.sched_mod, "save", lambda *_a, **_k: None)
    job = scheduler.Job(prompt="inspect", schedule="@daily", tool_profile=STRICT)
    assert job.tool_profile == STRICT, "premise: the job's own answer differs"

    settings = Settings()
    # ASK-FIRST, so a delivered AUTONOMOUS can only have come from the hardcode.
    settings.tool_policy_profile = INTERACTIVE
    assert len({INTERACTIVE, AUTONOMOUS, STRICT}) == 3, "premise: all three differ"

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


@pytest.mark.asyncio
async def test_a_cron_turn_asks_NOBODY_even_when_the_conversation_is_ask_first(monkeypatch):
    """The reason the hardcode exists, asserted as behaviour instead of a value.

    🔴 THE ARM ABOVE PINS A STRING; THIS ONE PINS THE CONSEQUENCE. A profile
    constant travelling correctly is only interesting because of what it stops
    happening -- Ryan: "for a cron it has to run auto because nobody will be
    there to hitl it". The failure this guards against is not a wrong label, it
    is a scheduled job at 3am sitting on a modal nobody will ever answer, which
    presents as "the automation silently stopped running" and NEVER as an error.

    The pairing is what makes it a measurement:
      - `test_sensitive_strict_call_requires_one_host_decision` fires the
        SAME policy and the SAME tool under STRICT and gets exactly one
        ToolApprovalScreen.
      - this one takes the profile a CRON actually delivered, with the
        conversation set to ask-first, and gets none.
    Same tool, same policy, opposite outcome, and the only difference is which
    profile the scheduled path resolved.
    """
    monkeypatch.setattr(app_mod.sched_mod, "save", lambda *_a, **_k: None)
    job = scheduler.Job(prompt="inspect", schedule="@daily")

    settings = Settings()
    settings.tool_policy_profile = INTERACTIVE  # the human asked to be asked

    idle = SimpleNamespace(
        jobs=[job],
        settings=settings,
        _chat_running=lambda: False,
        _user_bubble=lambda *_a, **_k: None,
        _pending_input=[],
        _handle_command=lambda _text: None,
        _append=lambda _msg: None,
        _stream=lambda: None,
        _active_tool_profile=INTERACTIVE,
    )
    app_mod.LiteTUI._fire_job(idle, job)

    # Not re-derived: whatever the scheduled path put there is what the turn runs
    # under. Hardcoding AUTONOMOUS here would test this test, not _fire_job.
    delivered = idle._active_tool_profile

    ran = []
    host, screens = _host(
        SHELL_POLICY,
        lambda args: ran.append(args) or "ran",
        profile=delivered,
        # If a modal DID appear this would approve it, so the arm cannot pass by
        # the tool merely being blocked -- it has to pass by nobody being asked.
        approve=ONCE,
    )
    result, ok = await app_mod.LiteTUI._execute_tool(
        host, "powershell", {"command": "git status"}
    )

    assert (result, ok) == ("ran", True)
    assert ran == [{"command": "git status"}]
    assert screens == [], (
        "a scheduled turn stopped to ask for approval; at 3am nobody answers it "
        "and the job hangs instead of running"
    )


def test_midturn_queue_adopts_the_delivered_items_profile():
    appended = []
    host = SimpleNamespace(
        _pending_input=[
            {"content": "scheduled", "text": "scheduled", "tool_profile": STRICT}
        ],
        _stop_requested=False,
        _active_tool_profile=INTERACTIVE,
        settings=Settings(),
        _append=lambda msg: appended.append(msg),
    )
    assert app_mod.LiteTUI._deliver_queued_input(host)
    assert host._active_tool_profile == STRICT
    assert appended == [{"role": "user", "content": "scheduled"}]
