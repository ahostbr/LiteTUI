"""The AUTONOMOUS profile — everything, unattended, no questions.

T073 item 4. The point of the row: Ryan killed his own agent seat rather than
keep answering the approval modal, and a guard that gets ROUTED AROUND protects
nothing. This is the supported way to say "do not ask me".

🔴 THE HANG THIS FILE EXISTS TO PREVENT. `MCP_UNKNOWN_POLICY` sets
`confirm_always=True`, which forces a prompt REGARDLESS of capabilities.
`scheduled` never reached that branch only by construction — everything it does
not allow is refused earlier. `autonomous` allows everything, so nothing is
refused earlier, and it WOULD have reached it and opened a modal in an
unattended run. The turn then waits forever for a human who is not there. An
autonomous profile that can block on a prompt is not autonomous.

The fix is `profile.confirm and (...)` in `evaluate`: a profile with an empty
confirm set never opens a modal. Every test below that asserts "no CONFIRM" is
paired with a control asserting `interactive` STILL confirms the same call —
without those, deleting the confirm branch entirely would pass this whole file.
"""
from pathlib import Path

import pytest

from litetui.tool_policy import (
    ALLOW,
    AUTONOMOUS,
    CAPABILITIES,
    CONFIRM,
    DENY,
    INTERACTIVE,
    MCP_UNKNOWN_POLICY,
    PROFILES,
    READ_POLICY,
    SCHEDULED,
    SHELL_POLICY,
    WRITE_POLICY,
    evaluate,
    rule_key,
)


@pytest.fixture
def ws(tmp_path):
    return Path(tmp_path)


def _act(profile, policy, args, ws, **kw):
    return evaluate(profile, policy, args, ws, **kw).action


# ── the profile itself ─────────────────────────────────────────────────────

def test_autonomous_grants_every_declared_capability():
    """DERIVED from CAPABILITIES, not a copy of the seven names.

    A capability added later would otherwise land outside this profile's allow
    AND confirm, and `autonomous` would start REFUSING something — fail-safe,
    but it would quietly stop meaning what its name says.
    """
    assert PROFILES[AUTONOMOUS].allow == CAPABILITIES
    assert PROFILES[AUTONOMOUS].confirm == frozenset()


def test_autonomous_allows_what_interactive_would_stop_to_ask_about(ws):
    for policy, args in (
        (SHELL_POLICY, {"command": "git status"}),
        (WRITE_POLICY, {"path": "/etc/hosts"}),
        (READ_POLICY, {}),
    ):
        assert _act(AUTONOMOUS, policy, args, ws) == ALLOW
        # CONTROL: the same call under interactive is still a question, so the
        # ALLOW above is the profile working and not the gate being gone.
        assert _act(INTERACTIVE, policy, args, ws) in (ALLOW, CONFIRM)

    assert _act(INTERACTIVE, SHELL_POLICY, {"command": "rm -rf ./build"}, ws) == CONFIRM

    # 🔴 THE ONE EXCEPTION, AND IT USED TO BE IN THE LIST ABOVE. `rm -rf` was an
    # ALLOW here until T844; then a model ran `rm -rf * .[a-zA-Z]*` under this
    # profile with no approval event and emptied a git worktree. Ryan,
    # 2026-09-17 (liteask a-584e69c0): "Keep autonomous, but
    # destructive_irreversible ALWAYS confirms (a floor no profile removes)".
    # Moved rather than deleted, so the line that changed is visible.
    assert _act(AUTONOMOUS, SHELL_POLICY, {"command": "rm -rf ./build"}, ws) == CONFIRM


# ── the hang ───────────────────────────────────────────────────────────────

def test_a_confirm_always_tool_does_not_prompt_an_absent_human(ws):
    """THE ONE THAT MATTERS. MCP_UNKNOWN_POLICY forces a prompt regardless of
    capabilities; under a profile with no confirm step there is nobody to
    answer it."""
    assert _act(AUTONOMOUS, MCP_UNKNOWN_POLICY, {}, ws) == ALLOW


def test_CONTROL_interactive_still_confirms_the_same_confirm_always_tool(ws):
    """Without this, deleting the confirm branch outright would satisfy the
    test above — and every other 'does not prompt' assertion in this file."""
    assert _act(INTERACTIVE, MCP_UNKNOWN_POLICY, {}, ws) == CONFIRM


def test_CONTROL_scheduled_is_unchanged_by_the_confirm_guard(ws):
    """The `profile.confirm and (...)` edit touches every profile with an empty
    confirm set, and `scheduled` is one. It must still REFUSE rather than
    silently start allowing — it reaches DENY earlier, above that branch."""
    assert _act(SCHEDULED, MCP_UNKNOWN_POLICY, {}, ws) == DENY
    assert _act(SCHEDULED, SHELL_POLICY, {"command": "git status"}, ws) == DENY
    assert _act(SCHEDULED, READ_POLICY, {}, ws) == ALLOW


# ── the only brake left ────────────────────────────────────────────────────

def test_a_standing_deny_rule_still_wins_under_autonomous(ws):
    """A deny rule beats this profile, because the deny gate runs before the
    profile is consulted at all.

    ⚠️ THIS DOCSTRING USED TO SAY a deny rule was "the ONLY thing between this
    profile and any tool". Since T844 that is false: `destructive_irreversible`
    also confirms here, and no profile can remove it. Corrected rather than
    left standing — a comment that states a superseded rule is how the next
    reader learns the wrong contract.
    """
    key = rule_key("shell", ["destructive_irreversible", "process_execution"])
    assert (
        _act(
            AUTONOMOUS,
            SHELL_POLICY,
            {"command": "rm -rf ./build"},
            ws,
            tool_name="shell",
            deny=frozenset({key}),
        )
        == DENY
    )
    # CONTROL: without the rule the same call does NOT reach DENY, so the DENY
    # above is the rule and not the profile refusing on its own. It was ALLOW
    # before T844 and is CONFIRM after it — either way it is not DENY, which is
    # the whole discriminator this control needs.
    assert (
        _act(AUTONOMOUS, SHELL_POLICY, {"command": "rm -rf ./build"}, ws,
             tool_name="shell")
        == CONFIRM
    )


def test_a_deny_rule_is_still_capability_scoped_under_autonomous(ws):
    """The escalation guard does not loosen just because the profile is wide:
    denying shell-at-process_execution must not also deny the destructive
    variant by accident, nor the reverse. Different authority, different key."""
    narrow = rule_key("shell", ["process_execution"])
    assert (
        _act(AUTONOMOUS, SHELL_POLICY, {"command": "git status"}, ws,
             tool_name="shell", deny=frozenset({narrow}))
        == DENY
    )
    # The destructive variant carries a different key, so the narrow rule does
    # not reach it. NOT-DENY is the assertion; T844's floor turned the exact
    # value from ALLOW into CONFIRM without touching the key scoping this arm
    # is about.
    assert (
        _act(AUTONOMOUS, SHELL_POLICY, {"command": "rm -rf ./build"}, ws,
             tool_name="shell", deny=frozenset({narrow}))
        == CONFIRM
    )


def test_an_unknown_profile_still_fails_closed(ws):
    """Adding a third profile must not turn a settings typo into a wide one."""
    assert _act("autonomus", SHELL_POLICY, {"command": "x"}, ws) == DENY
    assert _act("", SHELL_POLICY, {"command": "x"}, ws) == DENY
