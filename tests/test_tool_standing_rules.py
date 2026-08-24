"""Standing allow/deny rules: the human stops being asked, without gaining authority.

WHY THIS EXISTS, and it is a field measurement rather than a design preference.
Ryan killed his own LiteTUI agent rather than keep answering the modal --
"i killed that qwen agent ... tool prompts were annoying me lol". A guard that
gets ROUTED AROUND protects nothing, so the modal had to become answerable once.

THE TWO ORDERINGS BELOW ARE THE WHOLE SAFETY OF THE FEATURE:

  a rule is keyed by TOOL **AND CAPABILITIES**, never by tool name alone.
  `shell` is process_execution normally and ALSO destructive_irreversible when
  its arguments match the destructive pattern. A name-only rule would take one
  approval of "run a command" and silently grant, later, an authority the human
  never saw.

  an allow rule turns CONFIRM into ALLOW and NEVER DENY into ALLOW. Otherwise
  clicking "always" in an interactive modal would hand the unattended scheduled
  profile something it deliberately refuses. The rule records that a question
  was answered; it does not move the authority boundary.
"""
from pathlib import Path

from litetui.tool_policy import (
    ALLOW,
    CONFIRM,
    DENY,
    DESTRUCTIVE_IRREVERSIBLE,
    INTERACTIVE,
    PROCESS_EXECUTION,
    SCHEDULED,
    SHELL_POLICY,
    WRITE_POLICY,
    evaluate,
    rule_key,
)


def decide(profile, policy, args=None, root=None, **kw):
    return evaluate(profile, policy, args or {}, root or Path.cwd(), **kw)


def test_a_rule_is_scoped_to_the_capabilities_not_just_the_tool():
    plain = rule_key("shell", {PROCESS_EXECUTION})
    escalated = rule_key("shell", {PROCESS_EXECUTION, DESTRUCTIVE_IRREVERSIBLE})
    assert plain != escalated, (
        "a name-only key would let one approval cover a strictly larger authority"
    )
    assert rule_key("shell", {"b", "a"}) == rule_key("shell", {"a", "b"}), (
        "key must not depend on set iteration order, or a rule stops matching itself"
    )


def test_an_allow_rule_silences_a_confirm(tmp_path):
    asked = decide(INTERACTIVE, WRITE_POLICY, {"path": str(tmp_path / "f")}, tmp_path,
                   tool_name="write")
    assert asked.action == CONFIRM, "precondition: this call normally prompts"
    key = rule_key("write", asked.capabilities)
    quiet = decide(INTERACTIVE, WRITE_POLICY, {"path": str(tmp_path / "f")}, tmp_path,
                   tool_name="write", always_allow=frozenset({key}))
    assert quiet.action == ALLOW
    assert "standing rule" in quiet.reason


def test_an_allow_rule_for_LESS_authority_does_not_cover_MORE(tmp_path):
    """The escalation case, and the reason the key carries capabilities.

    Approve `shell` for process_execution, then call it with a destructive
    command. The stored rule must NOT match, and the human must be asked again.
    """
    plain = decide(INTERACTIVE, SHELL_POLICY, {"command": "echo hi"}, tmp_path,
                   tool_name="shell")
    approved = rule_key("shell", plain.capabilities)

    escalated = decide(INTERACTIVE, SHELL_POLICY, {"command": "rm -rf /"}, tmp_path,
                       tool_name="shell", always_allow=frozenset({approved}))
    assert DESTRUCTIVE_IRREVERSIBLE in escalated.capabilities, "precondition: args escalate it"
    assert escalated.action == CONFIRM, (
        "a rule approved for process_execution silently covered a destructive call"
    )


def test_an_allow_rule_never_turns_a_profile_denial_into_allow(tmp_path):
    """Scheduled is unattended by design. A rule made in an interactive modal
    must not widen it."""
    refused = decide(SCHEDULED, WRITE_POLICY, {"path": str(tmp_path / "f")}, tmp_path,
                     tool_name="write")
    assert refused.action == DENY, "precondition: scheduled does not grant writes"
    key = rule_key("write", refused.capabilities)
    still = decide(SCHEDULED, WRITE_POLICY, {"path": str(tmp_path / "f")}, tmp_path,
                   tool_name="write", always_allow=frozenset({key}))
    assert still.action == DENY, "an allow rule widened an unattended profile"


def test_deny_wins_over_an_allow_rule_and_over_the_profile(tmp_path):
    d = decide(INTERACTIVE, WRITE_POLICY, {"path": str(tmp_path / "f")}, tmp_path,
               tool_name="write")
    key = rule_key("write", d.capabilities)
    both = decide(INTERACTIVE, WRITE_POLICY, {"path": str(tmp_path / "f")}, tmp_path,
                  tool_name="write", always_allow=frozenset({key}), deny=frozenset({key}))
    assert both.action == DENY, "a written-down refusal was reachable by adding an allow rule"
    assert "standing rule" in both.reason


def test_rules_cannot_apply_without_a_tool_name(tmp_path):
    """The fail-safe direction: an unnamed call matches no rule at all."""
    d = decide(INTERACTIVE, WRITE_POLICY, {"path": str(tmp_path / "f")}, tmp_path)
    key = rule_key("", d.capabilities)
    same = decide(INTERACTIVE, WRITE_POLICY, {"path": str(tmp_path / "f")}, tmp_path,
                  always_allow=frozenset({key}))
    assert same.action == CONFIRM, "a nameless call matched a stored rule"
