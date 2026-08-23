"""Tool authority is metadata plus a host decision, never model discretion."""
from pathlib import Path

import app
import scheduler
from settings import Settings
from tool_policy import (
    ALLOW,
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
    SCHEDULED,
    SHELL_POLICY,
    STUDIO_POLICY,
    WORKSPACE_WRITE,
    WRITE_POLICY,
    evaluate,
)


def decide(profile, policy, args=None, root=None):
    return evaluate(profile, policy, args or {}, root or Path.cwd())


def test_interactive_read_is_silent_but_scheduled_network_is_narrower(tmp_path):
    assert decide(INTERACTIVE, READ_POLICY, root=tmp_path).action == ALLOW
    assert decide(SCHEDULED, READ_POLICY, root=tmp_path).action == ALLOW
    assert decide(INTERACTIVE, NETWORK_READ_POLICY, root=tmp_path).action == ALLOW
    scheduled = decide(SCHEDULED, NETWORK_READ_POLICY, root=tmp_path)
    assert scheduled.action == DENY
    assert "network" in scheduled.reason


def test_write_policy_distinguishes_workspace_from_external_paths(tmp_path):
    inside = decide(INTERACTIVE, WRITE_POLICY, {"path": "notes/x.md"}, tmp_path)
    assert inside.action == CONFIRM
    assert inside.capabilities == frozenset({WORKSPACE_WRITE})

    outside = decide(
        INTERACTIVE,
        WRITE_POLICY,
        {"path": str(tmp_path.parent / "outside.md")},
        tmp_path,
    )
    assert outside.action == CONFIRM
    assert outside.capabilities == frozenset({EXTERNAL_WRITE})
    assert decide(SCHEDULED, WRITE_POLICY, {"path": "x.md"}, tmp_path).action == DENY


def test_shell_is_confirmed_and_destructive_commands_are_named(tmp_path):
    ordinary = decide(INTERACTIVE, SHELL_POLICY, {"command": "git status"}, tmp_path)
    assert ordinary.action == CONFIRM
    assert DESTRUCTIVE_IRREVERSIBLE not in ordinary.capabilities

    destructive = decide(
        INTERACTIVE,
        SHELL_POLICY,
        {"command": "Remove-Item -Recurse old-output"},
        tmp_path,
    )
    assert destructive.action == CONFIRM
    assert DESTRUCTIVE_IRREVERSIBLE in destructive.capabilities
    assert decide(SCHEDULED, SHELL_POLICY, {"command": "git status"}, tmp_path).action == DENY


def test_argument_sensitive_desktop_and_harness_actions(tmp_path):
    screenshot = decide(
        INTERACTIVE, PCCONTROL_POLICY, {"action": "screenshot"}, tmp_path
    )
    assert screenshot.action == ALLOW
    click = decide(INTERACTIVE, PCCONTROL_POLICY, {"action": "click"}, tmp_path)
    assert click.action == CONFIRM

    who = decide(INTERACTIVE, HARNESS_POLICY, {"action": "whoami"}, tmp_path)
    assert who.action == ALLOW
    send = decide(INTERACTIVE, HARNESS_POLICY, {"action": "send"}, tmp_path)
    assert send.action == CONFIRM
    assert decide(SCHEDULED, HARNESS_POLICY, {"action": "send"}, tmp_path).action == DENY

    assert decide(
        INTERACTIVE, STUDIO_POLICY, {"app": "model", "action": "list_families"}, tmp_path
    ).action == ALLOW
    assert decide(
        INTERACTIVE, STUDIO_POLICY, {"app": "image", "action": "generate"}, tmp_path
    ).action == CONFIRM


def test_unknown_profiles_and_undeclared_mcp_effects_fail_closed(tmp_path):
    assert decide("typo", READ_POLICY, root=tmp_path).action == DENY
    assert decide(INTERACTIVE, MCP_UNKNOWN_POLICY, root=tmp_path).action == CONFIRM
    assert decide(SCHEDULED, MCP_UNKNOWN_POLICY, root=tmp_path).action == DENY


def test_every_first_party_registered_tool_has_explicit_policy():
    tui = app.LiteTUI()
    assert tui.plugins.tools
    unknown = [entry.name for entry in tui.plugins.tools if entry.policy is MCP_UNKNOWN_POLICY]
    assert unknown == [], f"first-party tools missing policy metadata: {unknown}"
    for entry in tui.plugins.tools:
        assert tui.plugins.policy_for(entry.name) is entry.policy


def test_conversations_are_interactive_and_jobs_are_narrow_by_default(tmp_path):
    assert Settings().tool_policy_profile == INTERACTIVE
    job = scheduler.Job(prompt="inspect status", schedule="@daily")
    assert job.tool_profile == SCHEDULED

    scheduler.save([job], tmp_path)
    restored = scheduler.load(tmp_path)
    assert len(restored) == 1
    assert restored[0].tool_profile == SCHEDULED
