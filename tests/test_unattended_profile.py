"""The tool-authority setting governs UNATTENDED turns too. T084.

the user, with two screenshots: a seat was denied writing
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

📌 THE USER THEN RULED THE DEFAULT ITSELF: `autonomous`, plus cron and loops on the
same set level, plus a footer that always names it and shift+tab to cycle it.
So Control 5 is no longer "degrade to read-only" -- it is ALLOWED **and no
confirm modal is ever constructed**. Both halves are asserted below, and the
second one is the one that keeps an unattended turn from hanging.

📌 2026-09-24: THE READ-ONLY FLOOR IS GONE. the user: "remove scheduled completely
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


def _ryans(app):
    """T1049: these arms are about OWNER'S OWN instance (owner-marked, not
    spawned), the only one that may run autonomous. conftest clears the mark."""
    app._spawned_seat, app._owner_seat, app._pty_term = False, True, None
    return app


def _mail_app(profile_name: str, *, route: str | None = None):
    """Drive the REAL `_deliver_inbox`; return the app, the model-bound
    messages and the user bubbles it produced.

    🔴 Never sets `_active_tool_profile` itself. A test that constructs the
    input it then checks proves the function works and says nothing about
    whether anything calls it that way -- the green-that-cannot-run this
    codebase already paid for once in T079.
    """
    app = _ryans(LiteTUI())
    if route == "spawner":
        app._owner_seat = False
        app._spawned_seat = True
        app._agent_launched = True
        app._spawner_id = "leader-id"
    elif route == "refuse":
        app._owner_seat = False
        app._spawned_seat = True
        app._agent_launched = True
        app._spawner_id = None
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
    """The exact call the user was denied: a workspace file that is not `.convos`."""
    return evaluate(
        profile_name,
        WRITE_POLICY,
        {"path": str(ROOT / "artifacts" / "ninfer-report.md")},
        ROOT,
    )


# -- CONTROL 1: the setting reaches the unattended path ----------------------

def test_autonomous_reaches_an_INBOX_WOKEN_turn():
    """the user's exact case, end to end through the real delivery method."""
    stamped = _woken_by_mail(AUTONOMOUS)
    assert stamped == AUTONOMOUS, "the setting never reached the unattended turn"
    assert _write_outside_the_store(stamped).action == ALLOW


# -- CONTROL 2: the fix is not "just grant it" ------------------------------

def test_strict_still_ASKS_for_the_same_write():
    """Without this, the fix is indistinguishable from removing the guard.

    Was `test_scheduled_still_DENIES_the_same_write`; `scheduled` is gone
    (the user 2026-09-24), and strict is the narrowest level left. It reaches the
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
    """the user's ruling, asked as an explicit either/or and answered "b default
    to auto" -- modelled on Claude Code, whose own default mode is `auto`.

    ⚠️ THIS TEST ASSERTED THE OPPOSITE AN HOUR AGO. The first version of
    Control 5 read "no saved setting -> read-only, never a modal", and it was
    written before the user ruled. He was shown the permissiveness in the option
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
    the user with no way to answer.

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
    """The replacement for the read-only floor (the user 2026-09-24).

    Every profile, every unattended source, the one tool that forces a
    prompt (an undeclared MCP tool). The outcome is ALLOW (autonomous) or a
    refusal that says why -- the dialog door is booby-trapped, so a modal
    cannot be how this passes.
    """
    from litetui import app as app_mod

    def _no_modal(*_a, **_k):
        raise AssertionError(f"{name}/{source} opened a modal nobody can answer")

    monkeypatch.setattr(app_mod, "show_dialog", _no_modal)
    app = _ryans(LiteTUI())
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
    """Was `..._still_degrades_unattended` (to `scheduled`). the user 2026-09-24
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


def test_spawned_inbox_turn_names_its_approval_relay():
    print("IMPORTED_APP=" + __import__("litetui.app", fromlist=["__file__"]).__file__)
    _app, appended, bubbles = _mail_app(INTERACTIVE, route="spawner")
    sent = appended[-1]["content"]
    assert "sent to leader-id for approval; attempt it and wait for the answer" in sent
    assert "will be refused this turn" not in sent
    assert sent not in bubbles


def test_spawnerless_inbox_keeps_refusal_guidance():
    _app, appended, _bubbles = _mail_app(INTERACTIVE, route="refuse")
    assert appended[-1]["content"].endswith(tool_policy.INBOX_TURN_RULE)


def test_hosted_inbox_turn_names_host_relay():
    app = _ryans(LiteTUI())
    app._owner_seat = False
    app._spawned_seat = True
    app._agent_launched = True
    app._rpc = True
    app._approval_host = True
    app._connect = lambda: None
    app.settings.tool_policy_profile = INTERACTIVE
    app._chat_running = lambda: True
    app._pending_input = []
    app._user_bubble = lambda *a, **k: None
    app._deliver_inbox({"from": "host", "priority": "normal", "body": "go"})
    assert "sent to your host for approval; attempt it and wait for the answer" in app._pending_input[-1]["content"]


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


# -- T1082: A SCHEDULED TURN RUNS AT THE LEVEL ITS JOB RECORDED -------------
#
# ⚠️ THIS SECTION HAS NOW BEEN REWRITTEN THREE TIMES, and the churn is the point of
# the comment. The user ruled, in order:
#   1. "cron and loops run at same set profile level"      -> read the setting
#   2. "select either auto mode or interactive" per job    -> read the job
#   3. "just change it so schedule only runs auto mode"    -> autonomous, always (T085)
#   4. "we need new settings to set this at the time u create the schedule ...
#      it runs at the scheduled level"                      -> read the job (T1082)
# Each earlier version was correct when written. (4) is the live one. The 3am
# question (3) deleted is answered by the routing: a CONFIRM on a scheduled turn
# never builds a modal (tool_policy.UNATTENDED_SOURCES): in Owner's own seat it is
# refused, and in an agent-spawned seat it goes to the launching agent (T1049-B).

def _fired_by_a_job(monkeypatch, *, setting=INTERACTIVE, job_level=None):
    """Drive the REAL `_fire_job` in Owner's own seat and return the profile it
    stamped.

    `setting` and `job_level` are chosen per arm so the three candidate origins
    (the chat setting, the job's recorded level, T085's retired AUTONOMOUS)
    differ, and an asserted level can only have come from the one it names.
    """
    from litetui import scheduler
    from litetui import app as app_mod

    app = _ryans(LiteTUI())
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


def test_a_job_recorded_AUTONOMOUS_runs_auto_and_may_write_the_workspace(monkeypatch):
    """Owner's own job, recorded autonomous, runs autonomous. It was T085's
    `test_a_scheduled_turn_runs_AUTO_...` for EVERY job."""
    stamped = _fired_by_a_job(monkeypatch, job_level=AUTONOMOUS)
    assert stamped == AUTONOMOUS
    assert _write_outside_the_store(stamped).action == ALLOW


def test_the_global_setting_does_NOT_reach_a_scheduled_turn(monkeypatch):
    """Changing how autonomous the CHAT is must not change what every saved
    automation may do (still true under T1082). Asserted at BOTH ends of the
    range, the job's level differing from the setting each time."""
    assert _fired_by_a_job(monkeypatch, setting=STRICT, job_level=AUTONOMOUS) == AUTONOMOUS
    assert _fired_by_a_job(monkeypatch, setting=AUTONOMOUS, job_level=STRICT) == STRICT


def test_the_per_job_level_GOVERNS_a_scheduled_turn(monkeypatch):
    """INVERTED by T1082. It was `test_a_stale_per_job_level_does_NOT_reach_...`,
    when `Job.tool_profile` was vestigial. With the setting AUTONOMOUS (the old
    hardcode's value too), a narrower level can only have come from the job."""
    assert _fired_by_a_job(monkeypatch, setting=AUTONOMOUS, job_level=STRICT) == STRICT
    assert _fired_by_a_job(monkeypatch, setting=AUTONOMOUS, job_level=INTERACTIVE) == INTERACTIVE


def test_a_scheduled_turn_can_never_construct_a_modal(monkeypatch):
    """Nobody is there to answer. An AUTONOMOUS job is safe BECAUSE its confirm set
    is empty, not because of its name (pinned so a later edit to the profile is
    caught). A job at any other level reaches `_authorize_action` as a "scheduled"
    turn, an UNATTENDED source, which refuses or relays a CONFIRM and never builds
    a modal (T1082 took away the guarantee that every job is autonomous)."""
    stamped = _fired_by_a_job(monkeypatch, job_level=AUTONOMOUS)
    assert not tool_policy.PROFILES[stamped].confirm
    decision = evaluate(stamped, tool_policy.MCP_UNKNOWN_POLICY, {"x": 1}, ROOT)
    assert decision.action != tool_policy.CONFIRM
    assert "scheduled" in tool_policy.UNATTENDED_SOURCES


def test_an_old_job_file_keeps_the_level_it_recorded(tmp_path):
    """The key was dead from T085 until T1082, and is live again: loading must keep
    it as recorded."""
    import json
    from litetui import scheduler

    (tmp_path / "jobs.json").write_text(json.dumps([
        {"prompt": "p", "schedule": "@daily", "tool_profile": "autonomous"}
    ]), encoding="utf-8")
    jobs = scheduler.load(tmp_path)
    assert len(jobs) == 1 and jobs[0].prompt == "p"
    assert jobs[0].tool_profile == "autonomous"
