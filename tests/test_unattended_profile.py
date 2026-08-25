"""The tool-authority setting governs UNATTENDED turns too. T084.

Ryan, with two screenshots: a seat was denied writing
`artifacts/ninfer-report.md` with "[policy denied] write: scheduled profile
does not grant workspace_write" while his Settings showed Conversation tool
authority = "autonomous - every capability, unattended, never asks".

🔴 THE LABEL IS WHY THIS IS A BUG AND NOT A DESIGN CHOICE. The word
*unattended* is IN THE OPTION TEXT (`tool_policy.py`, AUTONOMOUS_PROFILE
`summary=`). He told the app that unattended turns get every capability, and
`_deliver_inbox` hardcoded SCHEDULED, so the unattended path never read the
setting. **A control that names a case it does not govern is worse than an
absent one.**

⚠️ THE OBVIOUS ONE-LINE FIX IS WRONG. `getattr(settings, "tool_policy_profile",
SCHEDULED)` looks like it falls back to the read-only floor. It does not:
`tool_policy_profile` is a dataclass field with a default, so the attribute
ALWAYS exists and the getattr default NEVER fires. Measured as mutation arm B
before it was believed.

📌 RYAN THEN RULED THE DEFAULT ITSELF: `autonomous`, plus cron and loops on the
same set level, plus a footer that always names it and shift+tab to cycle it.
So Control 5 is no longer "degrade to read-only" -- it is ALLOWED **and no
confirm modal is ever constructed**. Both halves are asserted below, and the
second one is the one that keeps an unattended turn from hanging.
"""
from __future__ import annotations

import pytest

from litetui import paths, tool_policy
from litetui.app import LiteTUI
from litetui.settings import Settings
from litetui.tool_policy import (
    ALLOW,
    AUTONOMOUS,
    DENY,
    INTERACTIVE,
    SCHEDULED,
    ToolProfile,
    WRITE_POLICY,
    evaluate,
    unattended,
)

ROOT = paths.ROOT


def _woken_by_mail(profile_name: str) -> str:
    """Drive the REAL `_deliver_inbox` and return the profile it stamped.

    🔴 Never sets `_active_tool_profile` itself. A test that constructs the
    input it then checks proves the function works and says nothing about
    whether anything calls it that way -- the green-that-cannot-run this
    codebase already paid for once in T079.
    """
    app = LiteTUI()
    app._connect = lambda: None
    app.settings.tool_policy_profile = profile_name
    app._chat_running = lambda: False
    app._user_bubble = lambda *a, **k: None
    app._append = lambda *a, **k: None
    app._stream = lambda *a, **k: None
    app._deliver_inbox({"from": "ba736bd4", "priority": "normal", "body": "go"})
    return app._active_tool_profile


def _write_outside_the_store(profile_name: str):
    """The exact call Ryan was denied: a workspace file that is not `.convos`."""
    return evaluate(
        profile_name,
        WRITE_POLICY,
        {"path": str(ROOT / "artifacts" / "ninfer-report.md")},
        ROOT,
    )


# -- CONTROL 1: the setting reaches the unattended path ----------------------

def test_autonomous_reaches_an_INBOX_WOKEN_turn():
    """Ryan's exact case, end to end through the real delivery method."""
    stamped = _woken_by_mail(AUTONOMOUS)
    assert stamped == AUTONOMOUS, "the setting never reached the unattended turn"
    assert _write_outside_the_store(stamped).action == ALLOW


# -- CONTROL 2: the fix is not "just grant it" ------------------------------

def test_scheduled_still_DENIES_the_same_write():
    """Without this, the fix is indistinguishable from removing the guard."""
    stamped = _woken_by_mail(SCHEDULED)
    assert stamped == SCHEDULED
    decision = _write_outside_the_store(stamped)
    assert decision.action == DENY
    assert "workspace_write" in decision.reason


# -- CONTROL 5: no saved setting -> ALLOWED, and NO MODAL. BOTH halves. ----

def test_a_user_who_never_chose_gets_AUTONOMOUS_unattended():
    """Ryan's ruling, asked as an explicit either/or and answered "b default
    to auto" -- modelled on Claude Code, whose own default mode is `auto`.

    ⚠️ THIS TEST ASSERTED THE OPPOSITE AN HOUR AGO. The first version of
    Control 5 read "no saved setting -> read-only, never a modal", and it was
    written before Ryan ruled. He was shown the permissiveness in the option
    text he chose from -- an unset user gets workspace_write on an unattended
    turn -- and chose it anyway. The `premise moved` assertion below is what
    made the change loud instead of silent when the default flipped.
    """
    assert Settings().tool_policy_profile == AUTONOMOUS, "premise moved"
    stamped = _woken_by_mail(Settings().tool_policy_profile)
    assert stamped == AUTONOMOUS
    assert _write_outside_the_store(stamped).action == ALLOW


def test_the_default_can_never_CONSTRUCT_a_modal():
    """THE HALF THAT IS NOT ABOUT PERMISSIVENESS, AND IT IS THE LOAD-BEARING ONE.

    `autonomous` is safe to run unattended because its `confirm` set is EMPTY,
    not because of its name. `evaluate` guards the prompt branch with
    `if profile.confirm and (policy.confirm_always or ...)`, and that leading
    clause is now what stands between EVERY DEFAULT USER and a modal opening
    on a turn nobody is watching -- it is no longer an edge case.

    The case that pins it is `confirm_always`, which forces a prompt
    REGARDLESS of capabilities: an unknown MCP tool. Drop `profile.confirm and`
    and this is the test that goes red rather than the app hanging in front of
    Ryan with no way to answer.
    """
    for name in tool_policy.PROFILE_NAMES:
        resolved = unattended(name)
        assert not tool_policy.PROFILES[resolved].confirm, (
            f"{name} resolves unattended to {resolved}, which can prompt"
        )
        decision = evaluate(
            resolved,
            tool_policy.MCP_UNKNOWN_POLICY,
            {"anything": 1},
            ROOT,
        )
        assert decision.action != tool_policy.CONFIRM, (
            f"{resolved} constructed a modal for an unknown MCP tool on an "
            "unattended turn -- nobody is there to answer it"
        )


def test_an_explicitly_chosen_interactive_still_degrades_unattended():
    """The default moved; this guard did not become unnecessary.

    A user who deliberately picks `interactive` still receives inbox mail, and
    that turn still has nobody to answer a modal.
    """
    assert _woken_by_mail(INTERACTIVE) == SCHEDULED


# -- CONTROL 3: T083 is unbroken under every profile ------------------------

@pytest.mark.parametrize("name", tool_policy.PROFILE_NAMES)
def test_the_agent_may_still_write_its_own_store(name):
    """Derived over PROFILE_NAMES so a 4th profile cannot skip this."""
    decision = evaluate(
        unattended(name),
        WRITE_POLICY,
        {"path": str(paths.CONVO_DIR / "c1" / "handoff.md")},
        ROOT,
    )
    assert decision.action == ALLOW, f"{name} lost its self-store: {decision.reason}"


# -- CONTROL 4: the escape hatch is still classified as workspace -----------

def test_a_path_escaping_the_store_is_not_self_store():
    decision = evaluate(
        _woken_by_mail(SCHEDULED),
        WRITE_POLICY,
        {"path": str(paths.CONVO_DIR / ".." / "src" / "litetui" / "app.py")},
        ROOT,
    )
    assert decision.action == DENY, "a `..` escape out of .convos was allowed"


# -- the rule is DERIVED, not a second table of names -----------------------

def test_unattended_tests_confirm_NOT_the_profile_name(monkeypatch):
    """A future profile is classified by WHAT IT DOES.

    If this were `if name == INTERACTIVE`, a fourth confirm-based profile
    would sail through and hang an unattended turn -- the same
    two-structures-that-must-agree defect this file exists to close.
    """
    asks = ToolProfile(
        name="supervised",
        allow=frozenset({tool_policy.READ_ONLY}),
        confirm=frozenset({tool_policy.WORKSPACE_WRITE}),
        summary="a NEW profile that asks",
    )
    silent = ToolProfile(
        name="trusted",
        allow=frozenset({tool_policy.READ_ONLY, tool_policy.WORKSPACE_WRITE}),
        confirm=frozenset(),
        summary="a NEW profile that does not ask",
    )
    monkeypatch.setitem(tool_policy.PROFILES, "supervised", asks)
    monkeypatch.setitem(tool_policy.PROFILES, "trusted", silent)

    assert unattended("supervised") == SCHEDULED, "a new confirm profile ran unattended"
    assert unattended("trusted") == "trusted", "a silent profile was needlessly demoted"


def test_an_unknown_profile_degrades_DOWN():
    """Hand-edited settings.json, or a profile dropped in a later version."""
    assert unattended("nonsense-not-a-profile") == SCHEDULED


# -- CONTROL 2: cron/loop turns honour the set level (RYAN'S RULING) --------

def _fired_by_a_job(profile_name: str, monkeypatch) -> str:
    """Drive the REAL `_fire_job` and return the profile it stamped.

    Same instrument as `_woken_by_mail`, for the same reason: a constructed
    profile would prove nothing about which value the firing path picks.
    """
    from litetui import scheduler
    from litetui import app as app_mod

    app = LiteTUI()
    app._connect = lambda: None
    app.settings.tool_policy_profile = profile_name
    app._chat_running = lambda: False
    app._user_bubble = lambda *a, **k: None
    app._append = lambda *a, **k: None
    app._stream = lambda *a, **k: None
    app._system = lambda *a, **k: None
    monkeypatch.setattr(app_mod.sched_mod, "save", lambda jobs, root=None: None)

    job = scheduler.Job(prompt="nightly", schedule="@daily")
    app.jobs.clear()
    app.jobs.append(job)
    app._fire_job(job)
    return app._active_tool_profile


def test_autonomous_reaches_a_CRON_turn(monkeypatch):
    """Ryan: "cron and loops run at same set profile level my ruling"."""
    stamped = _fired_by_a_job(AUTONOMOUS, monkeypatch)
    assert stamped == AUTONOMOUS, "a cron turn ignored the set level"
    assert _write_outside_the_store(stamped).action == ALLOW


def test_a_cron_turn_with_NO_saved_setting_gets_no_modal(monkeypatch):
    """Control 5 again, by the route the ruling opened.

    Before the ruling a job was pinned to `scheduled` and could not reach a
    confirm profile at all. Honouring the set level is what put an unattended
    cron turn one step away from a modal nobody can answer.
    """
    stamped = _fired_by_a_job(INTERACTIVE, monkeypatch)
    assert stamped == SCHEDULED
    assert not tool_policy.PROFILES[stamped].confirm


def test_scheduled_setting_still_denies_a_CRON_write(monkeypatch):
    """The ruling widened who is heard, not what `scheduled` grants."""
    stamped = _fired_by_a_job(SCHEDULED, monkeypatch)
    decision = _write_outside_the_store(stamped)
    assert decision.action == DENY
    assert "workspace_write" in decision.reason


def test_the_dead_per_job_knob_no_longer_overrides_the_setting(monkeypatch):
    """`job.tool_profile` defaults to `scheduled` and is NOT consulted.

    If firing still read it, every job on disk would pin itself to `scheduled`
    and the ruling would be silently unimplemented -- which is exactly how the
    inbox bug looked from Settings.
    """
    from litetui import scheduler

    assert scheduler.Job(prompt="p", schedule="@daily").tool_profile == SCHEDULED
    assert _fired_by_a_job(AUTONOMOUS, monkeypatch) == AUTONOMOUS


def test_an_old_job_file_carrying_the_dead_key_still_loads(tmp_path):
    """The knob stays on disk for now, so loading must not regress."""
    import json
    from litetui import scheduler

    (tmp_path / "jobs.json").write_text(json.dumps([
        {"prompt": "p", "schedule": "@daily", "tool_profile": "autonomous"}
    ]), encoding="utf-8")
    jobs = scheduler.load(tmp_path)
    assert len(jobs) == 1 and jobs[0].prompt == "p"
