"""Tool authority is metadata plus a host decision, never model discretion."""
from pathlib import Path

from litetui import app
from litetui import scheduler
from litetui.settings import Settings
from litetui.tool_policy import (
    ALLOW,
    AUTONOMOUS,
    CONFIRM,
    DENY,
    DESTRUCTIVE_IRREVERSIBLE,
    EXTERNAL_WRITE,
    HARNESS_POLICY,
    INTERACTIVE,
    MCP_UNKNOWN_POLICY,
    NETWORK_READ_POLICY,
    PCCONTROL_POLICY,
    READ_POLICY,
    SHELL_POLICY,
    STRICT,
    STUDIO_POLICY,
    WORKSPACE_WRITE,
    WRITE_POLICY,
    evaluate,
)


def decide(profile, policy, args=None, root=None):
    return evaluate(profile, policy, args or {}, root or Path.cwd())


def test_reads_and_network_reads_are_silent_on_every_profile(tmp_path):
    """Was `..._but_scheduled_network_is_narrower`: `scheduled` (the read-only
    floor that DENIED network reads) is gone -- Ryan 2026-09-24, "remove
    scheduled completely it makes no sense to me". The narrowest level left,
    strict, allows reads and network reads without asking."""
    for profile in (STRICT, INTERACTIVE, AUTONOMOUS):
        assert decide(profile, READ_POLICY, root=tmp_path).action == ALLOW
        assert decide(profile, NETWORK_READ_POLICY, root=tmp_path).action == ALLOW


def test_write_policy_distinguishes_workspace_from_external_paths(tmp_path):
    """The CLASSIFICATION still tells the two apart; interactive no longer
    asks about either (Ryan 2026-09-24: "make interactive ask only for
    dangerous cmds any deletions or zip expansions weird procc runs that arent
    its tools and dangerous cmds threw PS and bash"). Strict still confirms."""
    inside = decide(INTERACTIVE, WRITE_POLICY, {"path": "notes/x.md"}, tmp_path)
    assert inside.action == ALLOW
    assert inside.capabilities == frozenset({WORKSPACE_WRITE})

    outside = decide(
        INTERACTIVE,
        WRITE_POLICY,
        {"path": str(tmp_path.parent / "outside.md")},
        tmp_path,
    )
    assert outside.action == ALLOW
    assert outside.capabilities == frozenset({EXTERNAL_WRITE})
    assert decide(STRICT, WRITE_POLICY, {"path": "x.md"}, tmp_path).action == CONFIRM


def test_shell_is_confirmed_and_destructive_commands_are_named(tmp_path):
    ordinary = decide(INTERACTIVE, SHELL_POLICY, {"command": "git status"}, tmp_path)
    assert ordinary.action == ALLOW
    strict = decide("strict", SHELL_POLICY, {"command": "git status"}, tmp_path)
    assert strict.action == CONFIRM
    assert DESTRUCTIVE_IRREVERSIBLE not in ordinary.capabilities

    destructive = decide(
        INTERACTIVE,
        SHELL_POLICY,
        {"command": "Remove-Item -Recurse old-output"},
        tmp_path,
    )
    assert destructive.action == CONFIRM
    assert DESTRUCTIVE_IRREVERSIBLE in destructive.capabilities
    # The confirm names its danger class, so an unattended refusal can too.
    assert destructive.danger == "deletion"
    assert "deletion" in destructive.reason


def test_argument_sensitive_desktop_and_harness_actions(tmp_path):
    """Interactive runs its own desktop/harness/studio tools without asking
    (Ryan 2026-09-24); only a pccontrol LAUNCH -- "weird procc runs that
    arent its tools" -- still confirms. Strict confirms all of them."""
    screenshot = decide(
        INTERACTIVE, PCCONTROL_POLICY, {"action": "screenshot"}, tmp_path
    )
    assert screenshot.action == ALLOW
    click = decide(INTERACTIVE, PCCONTROL_POLICY, {"action": "click"}, tmp_path)
    assert click.action == ALLOW
    assert decide(STRICT, PCCONTROL_POLICY, {"action": "click"}, tmp_path).action == CONFIRM
    launch = decide(INTERACTIVE, PCCONTROL_POLICY, {"action": "launch"}, tmp_path)
    assert launch.action == CONFIRM
    assert DESTRUCTIVE_IRREVERSIBLE in launch.capabilities

    who = decide(INTERACTIVE, HARNESS_POLICY, {"action": "whoami"}, tmp_path)
    assert who.action == ALLOW
    send = decide(INTERACTIVE, HARNESS_POLICY, {"action": "send"}, tmp_path)
    assert send.action == ALLOW
    assert decide(STRICT, HARNESS_POLICY, {"action": "send"}, tmp_path).action == CONFIRM

    assert decide(
        INTERACTIVE, STUDIO_POLICY, {"app": "model", "action": "list_families"}, tmp_path
    ).action == ALLOW
    assert decide(
        INTERACTIVE, STUDIO_POLICY, {"app": "image", "action": "generate"}, tmp_path
    ).action == ALLOW
    assert decide(
        STRICT, STUDIO_POLICY, {"app": "image", "action": "generate"}, tmp_path
    ).action == CONFIRM


def test_unknown_profiles_and_undeclared_mcp_effects_fail_closed(tmp_path):
    assert decide("typo", READ_POLICY, root=tmp_path).action == DENY
    assert decide(INTERACTIVE, MCP_UNKNOWN_POLICY, root=tmp_path).action == CONFIRM
    assert decide(STRICT, MCP_UNKNOWN_POLICY, root=tmp_path).action == CONFIRM
    # A removed profile name is just an unknown one now: denied, never widened.
    assert decide("scheduled", MCP_UNKNOWN_POLICY, root=tmp_path).action == DENY


def test_every_first_party_registered_tool_has_explicit_policy():
    tui = app.LiteTUI()
    assert tui.plugins.tools
    unknown = [entry.name for entry in tui.plugins.tools if entry.policy is MCP_UNKNOWN_POLICY]
    assert unknown == [], f"first-party tools missing policy metadata: {unknown}"
    for entry in tui.plugins.tools:
        assert tui.plugins.policy_for(entry.name) is entry.policy


def test_every_turn_defaults_to_autonomous_and_the_job_knob_is_dead(tmp_path):
    """RENAMED, because the old name asserted a design Ryan overruled.

    It was `..._conversations_are_interactive_and_jobs_are_narrow_by_default`,
    and both halves are now wrong: the conversation default is `autonomous`
    ("b default to auto"), and a job's own `tool_profile` is no longer read
    when the job fires ("cron and loops run at same set profile level").

    The field itself still exists and still round-trips, which is why the
    persistence half of this test is kept verbatim below -- removing a stored
    field is a schema change and did not belong in the authority fix.

    Its default is now AUTONOMOUS, the level a job actually fires at, since
    `scheduled` was removed (Ryan 2026-09-24).
    """
    assert Settings().tool_policy_profile == AUTONOMOUS
    job = scheduler.Job(prompt="inspect status", schedule="@daily")
    assert job.tool_profile == AUTONOMOUS

    scheduler.save([job], tmp_path)
    restored = scheduler.load(tmp_path)
    assert len(restored) == 1
    assert restored[0].tool_profile == AUTONOMOUS
