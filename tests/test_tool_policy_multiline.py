"""T0251: shell command boundaries classify effects without promoting quoted data.

T0116 already replaced whole-command regex searches with quote-aware per-segment
argv classification. These regressions pin both polarities of the original
multiline report through the public classifier and every profile. No shell
command is executed, and deny-floor parsing/authority is not changed here.
"""
from pathlib import Path

import pytest

from litetui import tool_policy as tp

WS = Path("C:/work/project") if Path("C:/").exists() else Path("/work/project")


def assert_policy(command, shell, label):
    assert tp.danger(command, WS, shell=shell) == label
    assert set(tp.classify_shell({"command": command}, WS, shell=shell)) == (
        {tp.DESTRUCTIVE_IRREVERSIBLE} if label else set()
    )
    for profile in tp.PROFILE_NAMES:
        decision = tp.evaluate(
            profile, tp.SHELL_POLICY, {"command": command}, WS,
            tool_name=shell or "bash", shell=shell,
        )
        assert (tp.DESTRUCTIVE_IRREVERSIBLE in decision.capabilities) == bool(label)
        # Classification applies to all profiles; autonomous deliberately never
        # asks, while human-selected strict also asks for ordinary launches.
        expected = (
            tp.CONFIRM if profile == tp.STRICT or (profile == tp.INTERACTIVE and label)
            else tp.ALLOW
        )
        assert decision.action == expected, (profile, shell, command, decision)
        if decision.action == tp.CONFIRM:
            assert decision.danger == (label or "")


@pytest.mark.parametrize("shell", ["bash", "powershell", None])
@pytest.mark.parametrize("separator", ["\n", "\r\n", ";", "|", "&&"])
@pytest.mark.parametrize("command,label", [
    ("rm -rf X", tp.DELETION),
    ("unzip archive.zip", tp.ARCHIVE),
    ("cp source destination", tp.OVERWRITE),
    ("format C:", tp.DANGEROUS),
])
def test_later_command_segments_keep_their_danger_class(shell, separator, command, label):
    assert_policy(f"echo a{separator}  {command}", shell, label)


@pytest.mark.parametrize("shell", ["bash", "powershell", None])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("command", [
    "echo 'a{newline}rm -rf X'",
    'echo "a{newline}rm -rf X"',
    "echo 'a{newline}unzip archive.zip'",
    'echo "a{newline}format C:"',
    "echo a{newline}echo rm -rf X",
    "git status{newline}git log --format=%h",
])
def test_multiline_data_and_ordinary_commands_do_not_gain_danger(shell, newline, command):
    assert_policy(command.format(newline=newline), shell, None)


@pytest.mark.parametrize("shell", ["bash", "powershell", None])
@pytest.mark.parametrize("separator", ["\n", "\r\n", ";"])
@pytest.mark.parametrize("command,label", [
    ("git rm --cached first{sep}git rm second", tp.DELETION),
    ("git rm first{sep}git rm --cached second", tp.DELETION),
    ("unzip first.zip{sep}unzip -l second.zip", tp.ARCHIVE),
    ("git restore --staged first{sep}git restore second", tp.DANGEROUS),
    ("git restore first{sep}git restore --staged second", tp.DANGEROUS),
    ("git rm --cached first{sep}git restore --staged second{sep}unzip -l a.zip", None),
])
def test_flags_do_not_leak_between_command_segments(shell, separator, command, label):
    assert_policy(command.format(sep=separator), shell, label)


@pytest.mark.parametrize("shell,command", [
    ("bash", 'echo a\n"rm" -rf X'),
    ("powershell", "echo a\r\nRemove-Item X -Recurse"),
    ("bash", "bash -c 'echo a\nrm -rf X'"),
    ("powershell", "powershell -Command 'echo a\nRemove-Item X'"),
    ("bash", 'echo "$(echo a\nrm -rf X)"'),
    ("powershell", 'Write-Output "$(echo a\nRemove-Item X)"'),
])
def test_multiline_quoted_verbs_wrappers_and_substitutions_are_executable(shell, command):
    assert_policy(command, shell, tp.DELETION)


@pytest.mark.parametrize("shell,command", [
    ("bash", "echo a\\\nrm -rf X"),
    ("powershell", "echo a`\nrm -rf X"),
])
def test_escaped_newline_is_continuation_not_a_command_boundary(shell, command):
    assert_policy(command, shell, None)


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quoted", [False, True])
def test_multiline_classification_is_independent_of_own_worktree_exemption(
    tmp_path, shell, newline, quoted,
):
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    own = main / ".worktrees" / "Seat-T0251"
    own.mkdir(parents=True)
    (own / ".git").write_text(
        f"gitdir: {main}/.git/worktrees/Seat-T0251\n", encoding="utf-8",
    )
    command = f"echo 'a{newline}rm -rf X'" if quoted else f"echo a{newline}rm -rf X"
    label = None if quoted else tp.DELETION
    for workspace in (main, own):
        assert tp.danger(command, workspace, shell=shell) == label
        for profile in tp.PROFILE_NAMES:
            decision = tp.evaluate(
                profile, tp.SHELL_POLICY, {"command": command}, workspace,
                tool_name=shell, shell=shell, seat_name="Seat",
            )
            assert (tp.DESTRUCTIVE_IRREVERSIBLE in decision.capabilities) == (not quoted)
            needs_prompt = (
                profile == tp.STRICT
                or (profile == tp.INTERACTIVE and not quoted and workspace == main)
            )
            assert decision.action == (tp.CONFIRM if needs_prompt else tp.ALLOW)
            if profile == tp.INTERACTIVE and not quoted and workspace == own:
                assert "own worktree" in decision.reason
