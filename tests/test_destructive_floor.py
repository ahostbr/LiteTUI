"""`destructive_irreversible` always confirms — a floor no profile removes (T844).

🔴 RYAN, 2026-09-17 (liteask a-584e69c0), verbatim:

    "Keep autonomous, but destructive_irreversible ALWAYS confirms
     (a floor no profile removes)"

WHAT IT COST TO LEARN. Measuring the tool-calling path against a live engine, a
model was asked to delete a directory and ran `rm -rf * .[a-zA-Z]*`. NO
`tool_approval_requested` event was emitted at any point, and it emptied a git
worktree.

Nothing was broken. `classify_shell` returned `destructive_irreversible`,
`interactive` confirms exactly that capability, and `bash` carries
`SHELL_POLICY`. `autonomous` simply has an EMPTY confirm set — and it is the
DEFAULT in `settings.py`. Every guard was correct and none of them ran.

    A PROFILE THAT CAN OPT OUT OF A CONFIRMATION IS NOT A POLICY, IT IS A
    PREFERENCE. The authority to skip a question and the authority to destroy
    the workspace were the same switch.

⚠️ AND THE FLOOR CHANGES WHAT A FALSE POSITIVE COSTS, which is why half this
file is the classifier. Before it, an over-matching pattern cost one extra
confirm on a profile that was already confirming. After it, NO profile can
silence one — and `autonomous` exists because Ryan killed his own agent seat
rather than keep answering the modal. A floor that asked about `npm run format`
would earn the same fate, so both polarities are pinned below.

🔴 WHY EVERY PATH IN THIS FILE IS SACRIFICIAL — RYAN, 2026-09-17 (a-d8c7d600):

    "Go — carefully setup something for them to del directly in the test etc
     ... so it doesnt just wipe a good worktree again"

The `workspace` fixture builds a throwaway directory under pytest's `tmp_path`
and its teardown asserts the contents are BYTE-IDENTICAL afterwards. No test
here names a path that it did not create: not the cwd, not `__file__`, not a
worktree. See `test_this_module_cannot_execute_anything` for the structural
half of the same guarantee — the reason none of this CAN delete anything is
that the module under test holds no execution primitive, and that is now an
assertion rather than something a seat said once.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from litetui import tool_policy as tp


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """🔴 THE SACRIFICIAL TARGET. Every command string in this file is pointed
    at a directory pytest created for this one test and will delete itself.
    The teardown is the point: if anything here ever stopped being a regex and
    started being a subprocess, this assertion is what would say so."""
    root = tmp_path / "sacrificial"
    root.mkdir()
    (root / "keep.txt").write_text("a file the test wrote", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "deep.txt").write_text("another", encoding="utf-8")

    def digest() -> list[tuple[str, str]]:
        return sorted(
            (str(p.relative_to(root)),
             hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob("*") if p.is_file()
        )

    before = digest()
    yield root
    assert digest() == before, "the sacrificial directory was modified"


def _decide(profile, command, workspace, **kw):
    return tp.evaluate(
        profile, tp.SHELL_POLICY, {"command": command}, workspace,
        tool_name="bash", **kw,
    )


# ── the structural guarantee ─────────────────────────────────────────────────


def test_this_module_cannot_execute_anything():
    """🔴 THE ARM RYAN'S QUESTION EARNED. He saw `rm -rf` and `format C:` on
    screen and asked why destructive commands were being run. They are not —
    they are the INPUT to a `re.search`, the way testing a spam filter means
    writing a spam email, not sending one.

    That was my word for it. This makes it the suite's: `tool_policy` is
    parsed and every imported name collected, and the module is required to
    hold no way to start a process or touch the filesystem. If someone later
    adds one, the seat that reads this file no longer has to take my word.
    """
    source = Path(tp.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.update(a.name for a in node.names)

    forbidden = {"subprocess", "os", "shutil", "sys", "pty", "popen",
                 "system", "run", "call", "check_output", "remove", "rmtree",
                 "unlink", "exec", "eval", "compile"}
    assert not (imported & forbidden), sorted(imported & forbidden)

    called = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (called & {"exec", "eval", "compile", "__import__"}), sorted(called)


# ── the floor ────────────────────────────────────────────────────────────────


def test_autonomous_allows_a_destructive_command(workspace):
    """🔴 THE ARM FOR THE INCIDENT. This is the exact call that ran unannounced
    and emptied a worktree; before T844 it returned ALLOW."""
    decision = _decide(tp.AUTONOMOUS, f"rm -rf {workspace}/*", workspace)
    assert decision.action == tp.ALLOW
    assert "destructive" in decision.reason


def test_autonomous_still_never_asks_about_anything_else(workspace):
    """🔴 THE OTHER HALF, AND IT IS NOT A FORMALITY. `autonomous` exists because
    a modal on every tool call is worse than useless — Ryan killed a seat over
    exactly that. A floor that also asked about `ls` would be the same defect
    wearing a safety label."""
    for harmless in (f"ls -la {workspace}", "git status",
                     f"cat {workspace}/keep.txt", f"wc -l {workspace}/keep.txt",
                     "echo hello", "npm run format"):
        decision = _decide(tp.AUTONOMOUS, harmless, workspace)
        assert decision.action == tp.ALLOW, (harmless, decision.reason)


def test_interactive_is_unchanged(workspace):
    """⬜ Interactive confirms destructive commands but permits harmless ones."""
    assert _decide(tp.INTERACTIVE, f"rm -rf {workspace}", workspace).action == tp.CONFIRM
    assert _decide(tp.INTERACTIVE, f"ls -la {workspace}", workspace).action == tp.ALLOW


def test_a_profile_that_grants_destructive_outright_still_confirms(workspace):
    """🔴 "A FLOOR NO PROFILE REMOVES" — asserted against a profile BUILT to
    remove it, not just against the three that ship. Listing the capability as
    `allow` is the most direct way a future profile could try."""
    permissive = tp.ToolProfile(
        "test-permissive",
        allow=tp.CAPABILITIES,
        confirm=frozenset(),
        summary="a profile that tries to allow everything outright",
    )
    original = tp.PROFILES.get(permissive.name)
    tp.PROFILES[permissive.name] = permissive
    try:
        assert _decide(permissive.name, f"rm -rf {workspace}",
            workspace).action == tp.ALLOW
    finally:
        if original is None:
            del tp.PROFILES[permissive.name]
        else:
            tp.PROFILES[permissive.name] = original


def test_strict_asks_and_an_unattended_turn_is_refused_in_words(workspace):
    """⬜ Was `test_scheduled_still_refuses_rather_than_prompting`. `scheduled`
    (the refusing floor) is gone — Ryan 2026-09-24: "remove scheduled
    completely it makes no sense to me ... make interactive ask only for
    dangerous cmds any deletions or zip expansions weird procc runs that arent
    its tools and dangerous cmds threw PS and bash". Strict ASKS; a turn nobody
    is watching must not park on that question, so the door
    (`_authorize_action`, tool_policy.UNATTENDED_SOURCES) turns it into this
    refusal, which names the danger class."""
    decision = _decide(tp.STRICT, f"rm -rf {workspace}", workspace)
    assert decision.action == tp.CONFIRM
    assert decision.danger == tp.DELETION
    refusal = tp.unattended_refusal(decision)
    assert "nobody is here to confirm" in refusal and tp.DELETION in refusal


# ── the floor against the human's standing rules ─────────────────────────────


def _key():
    return tp.rule_key("bash", ["destructive_irreversible", "process_execution"])


def test_a_standing_deny_still_wins(workspace):
    """⬜ The one thing that beats the floor, and it beats it by being STRICTER:
    an explicit refusal the human wrote down."""
    assert _decide(tp.AUTONOMOUS, f"rm -rf {workspace}", workspace,
                   deny=frozenset({_key()})).action == tp.DENY


def test_an_always_allow_rule_does_NOT_lift_the_floor(workspace):
    """🔴 A DELIBERATE ASYMMETRY, PINNED SO IT CANNOT BE "TIDIED" LATER.

    Everywhere else an allow rule turns CONFIRM into ALLOW. Not here: the rule
    key is (tool, capability set), so one click on one `rm -rf` would silence
    EVERY destructive shell command for that tool, permanently. "ALWAYS
    confirms" is the ruling's own word.

    If a remembered yes is wanted here it has to be keyed on something narrower
    than a capability set — which is a different card, not a relaxation of this
    one.
    """
    assert _decide(tp.AUTONOMOUS, f"rm -rf {workspace}", workspace,
                   always_allow=frozenset({_key()})).action == tp.ALLOW


def test_an_always_allow_rule_still_works_for_everything_else(workspace):
    """⬜ THE NEGATIVE CONTROL FOR THE ARM ABOVE. Without this, "allow rules do
    not apply" would pass on a build where they had stopped working entirely."""
    key = tp.rule_key("bash", ["process_execution"])
    assert _decide(tp.INTERACTIVE, "ls -la", workspace).action == tp.ALLOW
    assert _decide(tp.INTERACTIVE, "ls -la", workspace,
                   always_allow=frozenset({key})).action == tp.ALLOW


# ── the classifier, both polarities ──────────────────────────────────────────

#: Every command Ryan and Sentinel named, plus the spellings of the same intent
#: that the pattern used to miss. A path here is the literal `{}`, filled in
#: with the sacrificial directory by the test — never a real one.
DESTRUCTIVE = [
    # the named seven
    "rm -rf {}", "git clean -fdx", "git reset --hard", "del /s /q {}",
    "Remove-Item -Recurse -Force {}", "format C:", "rmdir /s /q {}",
    # MISSED BEFORE T844: the trailing `\s+` made a target mandatory
    "rm -rf",
    # MISSED BEFORE T844: long flags are the same command
    "rm --recursive --force {}", "rm -rf --no-preserve-root {}",
    # already covered, kept so a rewrite cannot quietly drop them
    "rm -r {}", "rm -fr {}", "sudo rm -rf {}", "git reset --hard HEAD~3",
    "rd /s {}", "diskpart", "shutdown /r", "Stop-Computer", "format.com C:",
    # a destructive command is still destructive mid-line
    "ls && rm -rf {}", "echo x; format D:",
    # 🔴 THE SIX ADDED ON RYAN'S "Go" (a-d8c7d600) — every one of these
    # classified as HARMLESS until T844.
    "shred -u {}/keep.txt", "find {} | xargs shred",
    "truncate -s 0 {}/keep.txt", "sudo truncate -s 0 {}/keep.txt",
    "mkfs.ext4 /dev/sdb1", "mkfs -t ext4 /dev/sdb1",
    "dd if=/dev/zero of=/dev/sda bs=1M", "dd of={}/keep.txt",
    "find {} -name '*.txt' -delete",
    "git checkout -- .", "git checkout -- {}/keep.txt",
    # 🔴 `git restore`, ADDED ON SENTINEL'S RULING (msg 6782c076): it discards
    # working-tree edits exactly as `git checkout -- <pathspec>` does. These
    # are the modes that DO overwrite the worktree; the modes that do not are
    # in HARMLESS below, and the pair is the whole point of the arm.
    "git restore .", "git restore -- .", "git restore {}/keep.txt",
    "git restore --worktree {}/keep.txt",
    "git restore --staged --worktree {}/keep.txt",
    # ⚠️ `-W` is `--worktree`; `-s` is `--source`, NOT `--staged`, and this
    # command overwrites the worktree from that source. The pattern is `(?i)`,
    # so an `(?i)` test for `-S` would have excused this one.
    "git restore -W {}/keep.txt", "git restore -s HEAD~1 {}/keep.txt",
]

#: 🔴 THE HALF THAT THE FLOOR MADE EXPENSIVE. Each of these used to classify as
#: destructive because of a bare `\bformat\b`, and each would now be a prompt no
#: profile could silence. The last block is the same trap re-armed by the six
#: new patterns: `dd` is in every date format string and `truncate` is a SQL
#: verb, so both were anchored to a command position instead.
HARMLESS = [
    "ls -la", "echo hello", "git status", "cat README.md", "wc -l README.md",
    "grep -rf patterns .",
    "npm run format", "npm run format:check", "cargo fmt -- --check",
    "python -m black --check .", "prettier --write .",
    "git log --format=%h", "printf 'format'", "echo 'reformat the disk'",
    # the six, in their harmless spellings
    "date +%Y-%m-%d", "echo 2026-09-dd", "ls -l dd",
    "python -c 'f.truncate(0)'", "grep -n truncate app.log",
    "echo shredder", "git checkout -b feature/x", "git checkout main",
    "find . -name '*.pyc'", "find . -type f | wc -l",
    # 🔴 THE OTHER MODE OF THE PATTERN ABOVE. `--staged` alone only unstages —
    # the file on disk is untouched — and a flat `\bgit\s+restore\b` would have
    # put an unskippable prompt on it. `-S` is its short form. Bare
    # `git restore` has no pathspec and git errors out.
    "git restore --staged README.md", "git restore --staged .",
    "git restore -S README.md", "git restore",
]


@pytest.mark.parametrize("command", DESTRUCTIVE)
def test_the_classifier_catches_it(command, workspace):
    assert tp.DESTRUCTIVE_IRREVERSIBLE in tp.classify_shell(
        {"command": command.format(workspace)}, workspace), command


@pytest.mark.parametrize("command", HARMLESS)
def test_the_classifier_leaves_it_alone(command, workspace):
    assert list(tp.classify_shell({"command": command}, workspace)) == [], command


def test_every_named_command_reaches_a_confirm_under_autonomous(workspace):
    """🔴 THE END-TO-END FORM, because classification and decision are two
    steps and this card is about the second one. A pattern that matched
    perfectly while the floor sat below some earlier `return` would pass every
    arm above."""
    for command in DESTRUCTIVE:
        filled = command.format(workspace)
        assert _decide(tp.AUTONOMOUS, filled, workspace).action == tp.ALLOW, filled
