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

📌 2026-09-24: THE READ-ONLY FLOOR IS GONE. Ryan: "remove scheduled completely
it makes no sense to me ... make interactive ask only for dangerous cmds any
deletions or zip expansions weird procc runs that arent its tools and dangerous
cmds threw PS and bash". `tool_policy.unattended()` no longer exists: an
unattended turn KEEPS the chosen profile, and `_authorize_action` turns a
CONFIRM into a worded refusal when `_hook_source` is in UNATTENDED_SOURCES --
so "never a modal" still holds, by a different door.
"""
from __future__ import annotations

import pytest

from litetui import paths, tool_policy
from litetui.app import LiteTUI
from litetui.settings import Settings
from litetui.tool_policy import (
    ALLOW,
    AUTONOMOUS,
    CONFIRM,
    INTERACTIVE,
    SELF_STORE,
    STRICT,
    WRITE_POLICY,
    evaluate,
)

ROOT = paths.ROOT


def _mail_app(profile_name: str):
    """Drive the REAL `_deliver_inbox`; return the app, the model-bound
    messages and the user bubbles it produced.

    🔴 Never sets `_active_tool_profile` itself. A test that constructs the
    input it then checks proves the function works and says nothing about
    whether anything calls it that way -- the green-that-cannot-run this
    codebase already paid for once in T079.
    """
    app = LiteTUI()
    app._connect = lambda: None
    app.settings.tool_policy_profile = profile_name
    app._chat_running = lambda: False
    bubbles, appended = [], []
    app._user_bubble = lambda text, *a, **k: bubbles.append(text)
    app._append = lambda msg, *a, **k: appended.append(msg)
    app._stream = lambda *a, **k: None
    app._deliver_inbox({"from": "ba736bd4", "priority": "normal", "body": "go"})
    return app, appended, bubbles


def _woken_by_mail(profile_name: str) -> str:
    """The profile the real `_deliver_inbox` stamped."""
    return _mail_app(profile_name)[0]._active_tool_profile


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

def test_strict_still_ASKS_for_the_same_write():
    """Without this, the fix is indistinguishable from removing the guard.

    Was `test_scheduled_still_DENIES_the_same_write`; `scheduled` is gone
    (Ryan 2026-09-24), and strict is the narrowest level left. It reaches the
    inbox turn intact and still asks for the write -- which, unattended, the
    door below turns into a refusal.
    """
    stamped = _woken_by_mail(STRICT)
    assert stamped == STRICT
    decision = _write_outside_the_store(stamped)
    assert decision.action == CONFIRM
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

    (The loop over every profile via `unattended()` is gone with that
    function; the profiles that CAN confirm are covered by the door test
    below, which is where their unattended confirms are now refused.)
    """
    default = Settings().tool_policy_profile
    assert not tool_policy.PROFILES[default].confirm
    decision = evaluate(default, tool_policy.MCP_UNKNOWN_POLICY, {"anything": 1}, ROOT)
    assert decision.action != tool_policy.CONFIRM, (
        f"{default} constructed a modal for an unknown MCP tool on an "
        "unattended turn -- nobody is there to answer it"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", sorted(tool_policy.UNATTENDED_SOURCES))
@pytest.mark.parametrize("name", tool_policy.PROFILE_NAMES)
async def test_an_unattended_CONFIRM_is_refused_in_words_never_a_modal(
        monkeypatch, name, source):
    """The replacement for the read-only floor (Ryan 2026-09-24).

    Every profile, every unattended source, the one tool that forces a
    prompt (an undeclared MCP tool). The outcome is ALLOW (autonomous) or a
    refusal that says why -- the dialog door is booby-trapped, so a modal
    cannot be how this passes.
    """
    from litetui import app as app_mod

    def _no_modal(*_a, **_k):
        raise AssertionError(f"{name}/{source} opened a modal nobody can answer")

    monkeypatch.setattr(app_mod, "show_dialog", _no_modal)
    app = LiteTUI()
    app._active_tool_profile = name
    app._hook_source = source
    refusal = await app._authorize_action(
        "mcp_probe", {"anything": 1}, tool_policy.MCP_UNKNOWN_POLICY)
    if tool_policy.PROFILES[name].confirm:
        text, ok = refusal
        assert not ok
        assert "nobody is here to confirm" in text
        assert tool_policy.UNDECLARED in text
    else:
        assert refusal is None


def test_an_explicitly_chosen_interactive_KEEPS_interactive_unattended():
    """Was `..._still_degrades_unattended` (to `scheduled`). Ryan 2026-09-24
    removed that floor: the mail turn keeps the profile he chose, and the
    model is told the one rule up front (INBOX_TURN_RULE) -- appended to what
    the MODEL reads, never to the bubble the human sees.
    """
    app, appended, bubbles = _mail_app(INTERACTIVE)
    assert app._active_tool_profile == INTERACTIVE
    assert app._hook_source in tool_policy.UNATTENDED_SOURCES
    sent = appended[-1]["content"]
    assert sent.endswith("\n\n" + tool_policy.INBOX_TURN_RULE)
    assert tool_policy.INBOX_TURN_RULE not in bubbles[-1]


def test_an_autonomous_mail_turn_carries_no_rule_it_cannot_break():
    """CONTROL: autonomous has no confirm set, so there is nothing that will
    be refused and the rule is not appended."""
    _app, appended, _bubbles = _mail_app(AUTONOMOUS)
    assert tool_policy.INBOX_TURN_RULE not in appended[-1]["content"]


# -- CONTROL 3: T083 is unbroken under every profile ------------------------

@pytest.mark.parametrize("name", tool_policy.PROFILE_NAMES)
def test_the_agent_may_still_write_its_own_store(name):
    """Derived over PROFILE_NAMES so a 4th profile cannot skip this.

    The unattended turn now keeps `name` itself (no `unattended()` degrade,
    2026-09-24), and the store is the ACTIVE conversation's, as the one
    policy door passes it (a62a153).
    """
    own = paths.CONVO_DIR / "c1"
    decision = evaluate(
        name,
        WRITE_POLICY,
        {"path": str(own / "handoff.md")},
        ROOT,
        active_conversation=own,
    )
    assert decision.action == ALLOW, f"{name} lost its self-store: {decision.reason}"


# -- CONTROL 4: the escape hatch is still classified as workspace -----------

def test_a_path_escaping_the_store_is_not_self_store():
    """On strict (the narrowest level since `scheduled` went, 2026-09-24) the
    escape is a workspace write, so it ASKS -- and is never self-store."""
    own = paths.CONVO_DIR / "c1"
    decision = evaluate(
        _woken_by_mail(STRICT),
        WRITE_POLICY,
        {"path": str(own / ".." / ".." / "src" / "litetui" / "app.py")},
        ROOT,
        active_conversation=own,
    )
    assert SELF_STORE not in decision.capabilities, "a `..` escape out of .convos was self-store"
    assert decision.action == CONFIRM, "a `..` escape out of .convos was allowed silently"


# -- T085: EVERY SCHEDULED TURN RUNS AUTO ----------------------------------
#
# ⚠️ THIS SECTION HAS BEEN REWRITTEN TWICE IN ONE EVENING and the churn is the
# point of the comment. Ryan ruled, in order:
#   1. "cron and loops run at same set profile level"      -> read the setting
#   2. "select either auto mode or interactive" per job    -> read the job
#   3. "just change it so schedule only runs auto mode"    -> autonomous, always
# Each earlier version was correct when written. (3) is the live one, and it is
# strictly simpler: it deletes the question "what does an interactive job do at
# 3am", which is the question that would otherwise need attendance detection.

def _fired_by_a_job(monkeypatch, *, setting=INTERACTIVE, job_level=None):
    """Drive the REAL `_fire_job` and return the profile it stamped.

    `setting` defaults to something OTHER than autonomous, and `job_level` can
    be set to something else again, so an assertion of `autonomous` cannot be
    satisfied by either source leaking through — it can only pass if the fire
    path resolves to autonomous on its own.
    """
    from litetui import scheduler
    from litetui import app as app_mod

    app = LiteTUI()
    app._connect = lambda: None
    app.settings.tool_policy_profile = setting
    app._chat_running = lambda: False
    app._user_bubble = lambda *a, **k: None
    app._append = lambda *a, **k: None
    app._stream = lambda *a, **k: None
    app._system = lambda *a, **k: None
    monkeypatch.setattr(app_mod.sched_mod, "save", lambda jobs, root=None: None)

    job = scheduler.Job(prompt="nightly", schedule="@daily")
    if job_level is not None:
        job.tool_profile = job_level
    app.jobs.clear()
    app.jobs.append(job)
    app._fire_job(job)
    return app._active_tool_profile


def test_a_scheduled_turn_runs_AUTO_and_may_write_the_workspace(monkeypatch):
    """Ryan: "just change it so schedule only runs auto mode"."""
    stamped = _fired_by_a_job(monkeypatch)
    assert stamped == AUTONOMOUS
    assert _write_outside_the_store(stamped).action == ALLOW


def test_the_global_setting_does_NOT_reach_a_scheduled_turn(monkeypatch):
    """Changing how autonomous the CHAT is must not change what every saved
    automation may do. Asserted at BOTH ends of the range so this cannot pass
    by the setting happening to agree."""
    assert _fired_by_a_job(monkeypatch, setting=STRICT) == AUTONOMOUS
    assert _fired_by_a_job(monkeypatch, setting=INTERACTIVE) == AUTONOMOUS


def test_a_stale_per_job_level_does_NOT_reach_a_scheduled_turn(monkeypatch):
    """`Job.tool_profile` still exists and still round-trips, so an old job
    file can carry any value. It is vestigial and must not govern."""
    assert _fired_by_a_job(monkeypatch, job_level=STRICT) == AUTONOMOUS
    assert _fired_by_a_job(monkeypatch, job_level=INTERACTIVE) == AUTONOMOUS


def test_a_scheduled_turn_can_never_construct_a_modal(monkeypatch):
    """The reason the answer is `autonomous` rather than a choice: nobody is
    there to answer. Autonomous is safe here BECAUSE its confirm set is empty,
    not because of its name — pinned so a later edit to the profile is caught."""
    stamped = _fired_by_a_job(monkeypatch)
    assert not tool_policy.PROFILES[stamped].confirm
    decision = evaluate(stamped, tool_policy.MCP_UNKNOWN_POLICY, {"x": 1}, ROOT)
    assert decision.action != tool_policy.CONFIRM


def test_an_old_job_file_carrying_the_dead_key_still_loads(tmp_path):
    """The knob stays on disk for now, so loading must not regress."""
    import json
    from litetui import scheduler

    (tmp_path / "jobs.json").write_text(json.dumps([
        {"prompt": "p", "schedule": "@daily", "tool_profile": "autonomous"}
    ]), encoding="utf-8")
    jobs = scheduler.load(tmp_path)
    assert len(jobs) == 1 and jobs[0].prompt == "p"
