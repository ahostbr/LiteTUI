"""Host-enforced capability policy for agent tools.

The model may choose a tool, but it never decides whether that tool is safe to
run in the current turn.  Every registry entry carries immutable metadata; the
host combines that metadata with the call arguments and a named profile to
produce one of three outcomes: allow, confirm with the human, or deny.

This module is deliberately pure.  It does not import Textual, touch disk, or
execute a tool, so policy decisions are cheap to test and cannot accidentally
become side effects of the operation they are supposed to guard.
"""
from __future__ import annotations

import re
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

# THE MODULE, not the name. `from litetui.paths import CONVO_DIR` binds at
# import time, and the store is REDIRECTED by many tests (and could be by any
# future caller) — a snapshot would silently classify against a directory that
# is no longer the store. Same late-binding trap as the profile choices.
from litetui import paths


# The required vocabulary.  A tool may carry more than one capability: shell
# execution is process_execution, and a command that formats a disk is also
# destructive_irreversible.
READ_ONLY = "read_only"
#: The agent writing ITS OWN STORE — `.convos/<id>/**`: the transcript, the
#: memory index, soul.md, handoff.md, memories/. Distinct from workspace_write
#: because it is a different ACT, not a smaller one: persisting yourself is not
#: editing the user's code, and the two were indistinguishable until T083.
#:
#: 🔴 THE BUG THAT FORCED THE DISTINCTION. Compaction is the agent writing down
#: what survives it, and it runs on whatever profile the last turn left behind.
#: After a scheduled turn that is `scheduled`, which is read-only — so every
#: compaction after a cron or /loop job SILENTLY DISCARDED the handoff and the
#: memories, politely, while the turn reported success. Under `interactive` it
#: was no better in kind: workspace_write means CONFIRM, so an autocompact
#: opened an approval modal in the middle of compacting.
SELF_STORE = "self_store"
WORKSPACE_WRITE = "workspace_write"
EXTERNAL_WRITE = "external_write"
PROCESS_EXECUTION = "process_execution"
NETWORK = "network"
DESKTOP_CONTROL = "desktop_control"
DESTRUCTIVE_IRREVERSIBLE = "destructive_irreversible"

CAPABILITIES = frozenset(
    {
        READ_ONLY,
        SELF_STORE,
        WORKSPACE_WRITE,
        EXTERNAL_WRITE,
        PROCESS_EXECUTION,
        NETWORK,
        DESKTOP_CONTROL,
        DESTRUCTIVE_IRREVERSIBLE,
    }
)

ALLOW = "allow"
CONFIRM = "confirm"
DENY = "deny"

INTERACTIVE = "interactive"
STRICT = "strict"
AUTONOMOUS = "autonomous"
#: PROFILE_NAMES is DERIVED from PROFILES, below — see the note there. It used
#: to be a hand-written tuple here, which meant the same set of profiles was
#: spelled out in three places.

Classifier = Callable[[Mapping[str, object], Path], Iterable[str]]


@dataclass(frozen=True)
class ToolPolicy:
    """One tool's declared authority, refined by its arguments when needed."""

    capabilities: frozenset[str]
    summary: str
    classify_args: Classifier | None = None
    confirm_always: bool = False

    def classify(self, args: Mapping[str, object], workspace: Path) -> frozenset[str]:
        caps = set(self.capabilities)
        if self.classify_args is not None:
            caps.update(self.classify_args(args, workspace))
        unknown = caps - CAPABILITIES
        if unknown:
            raise ValueError(f"unknown tool capability metadata: {sorted(unknown)}")
        if not caps:
            raise ValueError("a tool policy must declare at least one capability")
        return frozenset(caps)


@dataclass(frozen=True)
class ToolProfile:
    """Authority available to one source of turns."""

    name: str
    allow: frozenset[str]
    confirm: frozenset[str]
    #: One clause, shown to the human in the settings dropdown. It lives HERE
    #: rather than beside the widget so a profile carries its own description:
    #: adding a profile cannot produce an option with no explanation, because
    #: there is no second list to forget to update.
    summary: str = ""


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    profile: str
    capabilities: frozenset[str]
    reason: str
    #: The danger CLASS behind a confirm (DANGER_TABLE's first column), so an
    #: unattended refusal can say what it needs a person for.
    danger: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.action == CONFIRM


# Interactive asks ONLY for the danger table (Ryan, 2026-09-24: "make
# interactive ask only for dangerous cmds any deletions or zip expansions weird
# procc runs that arent its tools and dangerous cmds threw PS and bash").
# Its own tools, file writes anywhere, desktop control and ordinary commands run
# without asking; `destructive_irreversible` is what DANGER_TABLE (and a
# pccontrol launch, and an MCP tool's undeclared effects) classify as dangerous.
INTERACTIVE_PROFILE = ToolProfile(
    INTERACTIVE,
    allow=frozenset(CAPABILITIES - {DESTRUCTIVE_IRREVERSIBLE}),
    confirm=frozenset({DESTRUCTIVE_IRREVERSIBLE}),
    summary="acts freely, asks before deleting, extracting, launching programs or dangerous commands",
)

# Strict supervision confirms every sensitive action, including ordinary
# process execution. Interactive mode is the middle level: shell commands are
# allowed unless their arguments are classified as destructive.
STRICT_PROFILE = ToolProfile(
    STRICT,
    allow=frozenset({READ_ONLY, NETWORK, SELF_STORE}),
    confirm=frozenset({
        WORKSPACE_WRITE,
        EXTERNAL_WRITE,
        PROCESS_EXECUTION,
        DESKTOP_CONTROL,
        DESTRUCTIVE_IRREVERSIBLE,
    }),
    summary="confirm every command and sensitive action",
)

#: Everything, unattended, no questions. The point of the row: Ryan killed his
#: own agent seat rather than keep answering the modal, and a guard that gets
#: ROUTED AROUND protects nothing. This is the supported way to say "do not ask
#: me", instead of the unsupported one (kill the agent, or leave tools off).
#:
#: `allow=CAPABILITIES` is DERIVED, never a copy of the names, for the same
#: reason PROFILE_NAMES is: a capability added later would otherwise land
#: OUTSIDE this profile's allow AND confirm, and "autonomous" would start
#: refusing something. Fail-safe, but it would mean the profile quietly stopped
#: meaning what it says.
#:
#: ⚠️ THIS PROFILE HAS NO CONFIRM STEP OF ITS OWN. A standing `deny` rule
#: still wins — the deny gate runs before the profile is consulted at all.
#:
#: Autonomous is intentionally the unattended, no-approval profile. The
#: interactive profile owns confirmation of sensitive capabilities; autonomous
#: must not reintroduce a prompt through an argument classifier.
AUTONOMOUS_PROFILE = ToolProfile(
    AUTONOMOUS,
    allow=CAPABILITIES,
    confirm=frozenset(),
    # No em dash: the label is rendered as "<name> — <summary>", so a dash here
    # produces "autonomous — never asks — every capability", which reads as two
    # separate clauses bolted together.
    summary="every capability, unattended, never asks",
)

#: 🔴 THE ONE SOURCE OF PROFILES. Everything else is derived from it.
#:
#: This used to be three hand-written lists — this dict, a PROFILE_NAMES tuple,
#: and TOOL_PROFILE_CHOICES in settings_screen.py — and they could drift in a
#: direction nothing caught: a profile added here but not to the others EXISTS,
#: is refused by the settings validator, and cannot be selected by anyone. It
#: was reachable and silent. Adding a profile is now one edit, in one place.
#:
#: ⚠️ INSERTION ORDER IS THE DROPDOWN ORDER, so it is a human-facing decision
#: rather than a formality (Sentinel raised exactly this). Ordered by AUTHORITY
#: GRANTED, ASCENDING — the list reads as a scale of trust and the widest
#: option sits visibly at the end rather than beside its neighbours. Insertion
#: order would have put `autonomous` next to `interactive`, which hides that it
#: is the extreme.
#:
#: `scheduled` (the read-only floor) is GONE, Ryan 2026-09-24: "remove
#: scheduled completely it makes no sense to me". A stored "scheduled" migrates
#: to interactive (settings._selectable_profile); a turn nobody is watching keeps
#: its profile and only its CONFIRMs are refused (UNATTENDED_SOURCES).
PROFILES = {
    STRICT: STRICT_PROFILE,
    INTERACTIVE: INTERACTIVE_PROFILE,
    AUTONOMOUS: AUTONOMOUS_PROFILE,
}

#: Derived, never hand-written. `tuple` so it stays immutable and ordered.
PROFILE_NAMES = tuple(PROFILES)

def selectable_profile_names() -> tuple[str, ...]:
    """The levels a human may CHOOSE -- shift+tab and the settings dropdown.

    Every profile, since `scheduled` (the one non-selectable floor) was removed.

    🔴 A FUNCTION, NOT A MODULE CONSTANT, AND I SHIPPED THE CONSTANT FIRST.
    `settings_screen.tool_profile_choices` already carried this exact warning
    -- "a constant is computed once at import and a test cannot then add a
    profile and watch it appear, which is the property that makes the drift
    impossible rather than merely fixed" -- and I introduced a constant one
    module away and broke three of its tests. Same shape as the class list in
    side_panel: the rule was written down, in a file I had read, about the very
    thing I was doing.
    """
    return PROFILE_NAMES


#: Turn sources with nobody at the keyboard (hook_host stamps `_hook_source`):
#: inbox mail, cron/loop fires, a child's result waking its parent. Such a turn
#: keeps its FULL profile (Ryan 2026-09-24: interactive powers, not a read-only
#: floor); only a CONFIRM, which nobody can answer, becomes a refusal.
UNATTENDED_SOURCES = frozenset({"harness", "scheduled", "child-result"})


#: Appended to an inbox turn's message when its profile can ask, so the model
#: knows the rule before it tries (it used to retry the shell six times).
INBOX_TURN_RULE = (
    "[This turn came from the inbox; nobody is at the keyboard. Act normally, "
    "but anything that needs a person's OK -- deletions, archive extraction, "
    "launching programs that aren't your tools, dangerous system commands -- "
    "will be refused this turn. Leave those for a person and say so.]"
)


def unattended_refusal(decision: PolicyDecision) -> str:
    """The sentence an unattended turn gets instead of a modal. The rest of the
    turn proceeds; the model is told to leave the action for a person."""
    what = decision.danger or "a sensitive action"
    return f"needs a person's OK ({what}) and nobody is here to confirm; leave it for them"


def stops_you(profile_name: str) -> bool:
    """Does this level ever STOP the agent -- by asking, or by refusing?

    The footer glyph is derived from this rather than from a table of names,
    for the reason `PROFILE_NAMES` is derived: a fourth profile must be
    classified by WHAT IT DOES, not by having been remembered in a second
    list. Ryan's model is Claude Code's footer, where the leading glyph tells
    you at a glance whether this level will interrupt you, without reading the
    words.

    📌 NOTE THIS IS NOT `profile.confirm`: a profile that REFUSES stops you as
    surely as one that asks. The glyph answers the user's question ("will this
    run?"), not the implementation's.
    """
    profile = PROFILES.get(profile_name)
    if profile is None:
        return True  # unknown authority is not something to advertise as free
    return profile.allow < CAPABILITIES


def cycle(profile_name: str) -> str:
    """shift+tab: one step DOWN the authority scale, wrapping.

    Today: interactive -> strict -> autonomous -> interactive. `scheduled` is
    gone (Ryan 2026-09-24), so every profile is on the cycle.

    `PROFILES` is ordered by authority ASCENDING, so descending is that same
    order stepped backwards -- the direction is expressed ONCE here rather
    than as a second hand-written tuple that has to agree with `PROFILES`.

    ⚠️ AN UNKNOWN CURRENT VALUE LANDS ON THE FLOOR, NOT ON THE DEFAULT. This
    returned AUTONOMOUS -- the WIDEST authority -- when settings held a name
    that is not a profile. Nothing was actually granted by it (`evaluate`
    denies an unknown profile outright, so such a turn has no authority to
    escalate FROM), but it is the same permissive-on-an-error-path shape that
    T084 removed from three `app.py` fallbacks in the same commit. Corrupt
    settings plus one keypress should not be a route to full authority.
    """
    order = selectable_profile_names()
    if profile_name not in order:
        # An old "scheduled" or a hand-edited name: land on the narrowest
        # level, never the widest.
        return order[0]
    i = order.index(profile_name)
    return order[(i - 1) % len(order)]


def rule_key(tool_name: str, capabilities: Iterable[str]) -> str:
    """The identity of a standing allow/deny rule.

    SCOPED TO THE TOOL **AND** THE CAPABILITIES THAT TRIGGERED THE PROMPT, not
    to the tool name alone, and that is the whole safety of the feature.
    A tool's authority is not fixed: `shell` classifies as process_execution
    normally and ALSO as destructive_irreversible when its arguments match the
    destructive-command pattern. A name-only rule would take one approval of
    "run a shell command" and silently grant, on some later call, an authority
    the human never saw and never agreed to.

    So "always allow" means: this tool, doing the thing I was just shown. The
    same tool asking for MORE authority prompts again, which is the only
    version of the feature that is still a guard.
    """
    caps = ",".join(sorted(capabilities))
    return f"{tool_name}:{caps}"


def evaluate(
    profile_name: str,
    policy: ToolPolicy,
    args: Mapping[str, object] | None,
    workspace: Path,
    *,
    tool_name: str = "",
    active_conversation: Path | None = None,
    always_allow: frozenset[str] = frozenset(),
    deny: frozenset[str] = frozenset(),
) -> PolicyDecision:
    """Return the host action for one proposed tool call.

    Unknown profiles fail closed.  A settings typo must never turn scheduled
    work into an unrestricted interactive turn.

    `always_allow` and `deny` are the human's standing rules, keyed by
    `rule_key`.  Two orderings here are load-bearing:

      DENY WINS.  An explicit deny rule is consulted before anything else and
      overrides both the profile and any allow rule.  A refusal the human wrote
      down must not be reachable by adding a second rule that disagrees.

      AN ALLOW RULE TURNS **CONFIRM** INTO ALLOW, AND NEVER **DENY** INTO
      ALLOW.  Clicking "always allow" in an interactive modal must not hand the
      unattended `scheduled` profile an authority it deliberately refuses --
      the rule records that the human stopped being asked, not that the profile
      changed.  Authority still comes from the profile; the rule only silences
      a question the human has already answered.

    `tool_name` defaults to empty so existing callers keep their behaviour
    exactly: an empty name can never match a stored rule, so no rule applies.
    That is the fail-safe direction.
    """
    profile = PROFILES.get(profile_name)
    capabilities = policy.classify(args or {}, Path(workspace).resolve())
    if policy.classify_args is classify_write:
        capabilities = frozenset(policy.capabilities) | frozenset(classify_write(args or {}, Path(workspace).resolve(), active_conversation=active_conversation))
    names = ", ".join(sorted(capabilities))
    if profile is None:
        return PolicyDecision(
            DENY,
            profile_name,
            capabilities,
            f"unknown tool profile {profile_name!r}; denied ({names})",
        )

    key = rule_key(tool_name, capabilities)
    if tool_name and key in deny:
        return PolicyDecision(
            DENY,
            profile.name,
            capabilities,
            f"denied by a standing rule for {key}",
        )

    outside = capabilities - profile.allow - profile.confirm
    if outside:
        return PolicyDecision(
            DENY,
            profile.name,
            capabilities,
            f"{profile.name} profile does not grant {', '.join(sorted(outside))}",
        )
    # 🔴 A PROFILE WITH AN EMPTY `confirm` SET NEVER OPENS A MODAL, and that
    # guard is `profile.confirm and ...` rather than the capability test alone.
    # `confirm_always` (MCP_UNKNOWN_POLICY) forces a prompt REGARDLESS of
    # capabilities, so without this an unattended profile that allows
    # everything would reach this branch and block on a human who is not there
    # — the turn waits forever. `scheduled` never hit it only by construction:
    # everything it does not allow is refused above, before this line.
    if profile.confirm and (policy.confirm_always or capabilities & profile.confirm):
        if tool_name and key in always_allow:
            return PolicyDecision(
                ALLOW,
                profile.name,
                capabilities,
                f"allowed by a standing rule for {key}",
            )
        what = _danger_of(policy, args or {}, Path(workspace).resolve())
        return PolicyDecision(
            CONFIRM,
            profile.name,
            capabilities,
            f"human confirmation required for {what or names}",
            danger=what,
        )
    return PolicyDecision(ALLOW, profile.name, capabilities, f"allowed: {names}")


def _danger_of(policy: ToolPolicy, args: Mapping[str, object], workspace: Path) -> str:
    """Which DANGER_TABLE class a confirm is for, in words a person reads."""
    if policy.classify_args is classify_shell:
        return danger(str(args.get("command") or ""), workspace) or ""
    if policy.classify_args is classify_pccontrol:
        return FOREIGN_PROCESS
    if policy.confirm_always:
        return UNDECLARED
    return ""


_SECRET_KEY = re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key|credential)")


def approval_preview(args: Mapping[str, object] | None, limit: int = 1800) -> str:
    """Human-readable arguments for the host approval modal.

    Confirmation must explain what will run without turning the confirmation
    surface into a credential leak.  Obvious secret-bearing fields are redacted
    and very large writes/prompts are bounded.
    """
    clean: dict[str, object] = {}
    for key, value in (args or {}).items():
        if _SECRET_KEY.search(str(key)):
            clean[str(key)] = "[redacted]"
            continue
        if isinstance(value, str) and len(value) > 600:
            clean[str(key)] = value[:600] + f"… [{len(value) - 600} more chars]"
        else:
            clean[str(key)] = value
    text = json.dumps(clean, ensure_ascii=False, indent=2, default=str)
    return text if len(text) <= limit else text[:limit] + "\n… [preview truncated]"


def _resolve_path(raw: object, workspace: Path) -> Path:
    p = Path(str(raw or "")).expanduser()
    if not p.is_absolute():
        p = workspace / p
    return p.resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def classify_write(args: Mapping[str, object], workspace: Path, *, active_conversation: Path | None = None) -> Iterable[str]:
    """Three answers, not two: the agent's own store, the workspace, elsewhere.

    🔴 THE SELF-STORE CHECK MUST COME FIRST, because `.convos` lives INSIDE the
    workspace — asking "is it in the workspace?" first would answer yes for
    every self-store write and the third case would be unreachable.

    The host supplies the active conversation directory. Only its memory index,
    soul, handoff, and memories subtree qualify; unknown context grants no
    self-store exception. Configuration and transcript remain ordinary writes.

    Escapes are handled by `_resolve_path`, which resolves before either test:
    `.convos/<id>/../../src/x.py` resolves out of the store and is classified
    as the workspace write it actually is.
    """
    target = _resolve_path(args.get("path"), workspace)
    if active_conversation is not None:
        own = Path(active_conversation).resolve()
        if target in {own / name for name in ('memory.md', 'soul.md', 'handoff.md')} or _inside(target, own / 'memories'):
            return (SELF_STORE,)
    return (WORKSPACE_WRITE,) if _inside(target, workspace) else (EXTERNAL_WRITE,)


#: Where a COMMAND can begin: the start of the string, after a shell
#: separator or `(`, after `sudo`/`xargs`, or as the first word of a
#: `-c "..."` / `-Command "..."` / `cmd /c "..."` string. Only the bare verbs
#: that are also ordinary words need it.
_CMD_POSITION = (r"(?:^|[;&|(]\s*|\bsudo\s+|\bxargs\s+(?:-\S+\s+)*"
                 r"|\s(?:-c|-command|/c)\s+[\"'])")

#: 🔴 EVERY MATCH IS NOW AN UNSKIPPABLE PROMPT, so this pattern is held to
#: BOTH polarities. Before the floor a false positive cost an extra confirm
#: on a profile that was already confirming; after it, no profile can
#: silence one -- which is the modal Ryan killed a seat over. Measured,
#: both directions, in tests/test_destructive_floor.py.
#:
#: TWO CHANGES, EACH FROM A MEASUREMENT (2026-09-17):
#:
#:   FALSE POSITIVE, FIXED. `\bformat\b` matched `npm run format`,
#:   `npm run format:check`, `git log --format=%h` and `printf 'format'`.
#:   `format` now has to be at a command position; a disk wipe is
#:   `format C:`, never the third word of a script name.
#:
#:   MISSES, FIXED. `rm -rf` with no target (the trailing `\s+` made the
#:   argument mandatory) and the long flags `rm --recursive --force`. Both
#:   are the same command by another spelling.
#:
#: SIX MORE ADDED 2026-09-17 ON RYAN'S WORD (liteask a-d8c7d600, "Go"): every
#: one classified as HARMLESS before this, and each is a way to destroy work
#: that no `rm` pattern can see. `git checkout -- .` is the one that has
#: already cost something here -- it silently discarded an uncommitted fix on
#: this seat the same day.
#:
#: ⚠️ FOUR OF THE SIX ARE CONSTRAINED, NOT BARE, and the reason is the
#: `format` lesson above -- a match is now a prompt nobody can skip:
#:   `dd`        needs a command position AND an `of=` argument, because
#:               `\bdd\b` alone matches the `dd` of `yyyy-mm-dd`.
#:   `truncate`  needs a command position; it is also a SQL verb and a
#:               Python method name.
#:   `find`      needs `-delete` in the SAME command (no `;&|` between).
#:   `checkout`  needs the `--` pathspec separator: `git checkout -b x` and
#:               `git checkout main` move a branch, they destroy nothing.
#: `shred` and `mkfs` are left unanchored: neither is an ordinary word, and
#: anchoring them would miss `find . | xargs shred`.
#:
#: 🔴 `git restore` IS THE ONE PATTERN WITH A MODE, added 2026-09-17 on
#: Sentinel's ruling: it discards working-tree edits exactly as
#: `git checkout -- <pathspec>` does, so the class Ryan named covers it -- but
#: ONLY in some of its modes, and a flat `\bgit\s+restore\b` would put an
#: unskippable prompt on the harmless one:
#:
#:   git restore <pathspec>              DESTRUCTIVE (overwrites the worktree)
#:   git restore --worktree <pathspec>   DESTRUCTIVE (the same, spelled out)
#:   git restore --staged --worktree x   DESTRUCTIVE (both trees)
#:   git restore --staged <pathspec>     HARMLESS   (unstages; the file is
#:                                                   untouched on disk)
#:   git restore                         HARMLESS   (no pathspec, git errors)
#:
#: ⚠️ THE SHORT FLAGS ARE CASE-SENSITIVE AND THE PATTERN IS `(?i)`. `-S` is
#: `--staged` but `-s` is `--source`, and `git restore -s HEAD~1 x` DOES
#: overwrite the worktree -- so an `(?i)` test for `-S` would have excused the
#: destructive one. Both short flags are matched inside `(?-i:...)`.
_C = _CMD_POSITION

#: 🔴 THE DANGER TABLE: the ONLY things `interactive` asks about.
#: Ryan, 2026-09-24 (via Sentinel 068bf9c7): "make interactive ask only for
#: dangerous cmds any deletions or zip expansions weird procc runs that arent
#: its tools and dangerous cmds threw PS and bash". One row per pattern; the
#: first element is the CLASS the unattended refusal names. Both shells: the
#: patterns are case-insensitive and PowerShell's aliases sit beside the bash
#: verbs. Positives AND negatives are pinned per class and shell in
#: tests/test_danger_table.py, because a false positive is a prompt on an
#: ordinary command (the `format` lesson above).
DELETION = "deletion"
ARCHIVE = "archive expansion"
FOREIGN_PROCESS = "launching a program that isn't one of its tools"
DANGEROUS = "a dangerous system command"
UNDECLARED = "a tool whose effects aren't declared"

DANGER_TABLE: tuple[tuple[str, str], ...] = (
    # ── A. deletion ─────────────────────────────────────────────────────────
    (DELETION, _C + r"(?:rm|del|erase|rmdir|rd|ri|unlink|shred)\b"),
    (DELETION, r"\b(?:remove-item|rimraf)\b"),
    (DELETION, (r"\bgit\s+(?:clean\b|rm\b(?![^;&|]*--cached)|worktree\s+remove\b"
                r"|branch\s+(?:[^;&|]*\s)?(?:-[a-z]*d[a-z]*|--delete)\b)")),
    (DELETION, r"\bfind\b[^;&|]*\s(?:-delete\b|-exec\s+rm\b)"),
    (DELETION, _C + r"truncate\b"),
    (DELETION, r"\b(?:shutil\.rmtree|os\.(?:remove|unlink|rmdir|removedirs))\s*\(|\.unlink\s*\("),
    # ── B. archive expansion ─────────────────────────────────────────────────
    (ARCHIVE, r"\bexpand-archive\b"),
    (ARCHIVE, _C + r"unzip\b(?![^;&|]*\s-[lvz]\b)"),
    (ARCHIVE, _C + r"(?:bsd)?tar\s+(?:-?[a-z]*x[a-z]*\b|[^;&|]*\s-[a-z]*x[a-z]*\b|[^;&|]*--(?:extract|get)\b)"),
    (ARCHIVE, _C + r"7z[a-z]?(?:\.exe)?\s+[xe]\b"),
    (ARCHIVE, _C + r"(?:gunzip|bunzip2|unxz|unrar|unar|unlzma|unzstd)\b"),
    (ARCHIVE, _C + r"(?:gzip|xz|bzip2|zstd)\s+(?:[^;&|]*\s)?-[a-z]*d"),
    (ARCHIVE, _C + r"expand(?:\.exe)?\s+[^;&|]*(?:-f:|\.cab\b)"),
    (ARCHIVE, r"\b(?:extractall|unpack_archive)\s*\("),
    # ── C. launching a program that isn't one of its tools ───────────────────
    #    (plus the workspace-aware path/script check in `danger()` below)
    (FOREIGN_PROCESS, r"\b(?:start-process|invoke-item)\b"),
    (FOREIGN_PROCESS, _C + r"(?:saps|ii|start)\s"),
    (FOREIGN_PROCESS, _C + r"cmd(?:\.exe)?\s+/[ck]\b"),
    (FOREIGN_PROCESS, r"(?:^|[;|(]\s*)&\s*[\"'$]"),
    (FOREIGN_PROCESS, _C + r"(?:pwsh|powershell)(?:\.exe)?\s+(?:[^;&|]*\s)?-f(?:ile)?\s"),
    (FOREIGN_PROCESS, _C + r"(?:wscript|cscript|mshta|rundll32|regsvr32|msiexec|runas|psexec(?:64)?)\b"),
    (FOREIGN_PROCESS, r"\bschtasks\b[^;&|]*/create\b|\bregister-scheduledtask\b"),
    # ── D. dangerous system commands ─────────────────────────────────────────
    (DANGEROUS, _C + r"format(?:\.com)?\b"),
    (DANGEROUS, r"\b(?:diskpart|bcdedit|takeown)\b|\bmkfs(?:\.[a-z0-9]+)?\b"),
    (DANGEROUS, _C + r"dd\s+(?:[^;&|]*\s)?of="),
    (DANGEROUS, r"\bvssadmin\s+delete\b|\bcipher(?:\.exe)?\s+/w\b|\bwevtutil\s+cl\b|\bclear-eventlog\b"),
    (DANGEROUS, _C + r"reg(?:\.exe)?\s+(?:delete|add|import|load|restore)\b"),
    (DANGEROUS, r"\b(?:set|remove|new)-itemproperty\b[^;&|]*\bhk(?:lm|cu|cr|u|cc):"),
    (DANGEROUS, r"\bset-executionpolicy\b"),
    (DANGEROUS, _C + r"(?:chmod|chown)\s+(?:[^;&|]*\s)?-[a-z]*r"),
    (DANGEROUS, r"\bicacls\b[^;&|]*/(?:grant|reset|setowner|remove|deny|t)\b"),
    (DANGEROUS, r"\b(?:curl|wget)\b[^;]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b"),
    (DANGEROUS, r"\b(?:iex|invoke-expression)\b"),
    (DANGEROUS, r"\b(?:shutdown|restart-computer|stop-computer)\b"),
    (DANGEROUS, _C + r"(?:kill|pkill|killall|taskkill|tskill)\b|\bstop-process\b"),
    (DANGEROUS, r"\bgit\s+push\b[^;&|]*(?:\s--force(?:-with-lease)?\b|(?-i:\s-f\b)|\s\+\S)"),
    (DANGEROUS, r"\bgit\s+(?:reset\s+--hard\b|checkout\s+--\s|filter-branch\b|filter-repo\b|reflog\s+expire\b)"),
    (DANGEROUS, (r"\bgit\s+restore\b(?:"
                 r"[^;&|]*(?:--worktree\b|(?-i:\s-W\b))|"
                 r"(?![^;&|]*(?:--staged\b|(?-i:\s-S\b)))\s+[^;&|]*\S"
                 r")")),
    (DANGEROUS, (r"\bnetsh\s+(?:advfirewall|firewall)\b|\bset-mppreference\b[^;&|]*-disable"
                 r"|\badd-mppreference\b[^;&|]*-exclusion")),
    (DANGEROUS, _C + r"sc(?:\.exe)?\s+(?:delete|config)\b"),
)
_DANGER = tuple((label, re.compile(r"(?ix)" + pattern)) for label, pattern in DANGER_TABLE)

#: A program run BY PATH, or an interpreter running a script FILE. Ordinary when
#: the file is inside the workspace (Sentinel b362b4ed: "his project scripts
#: aren't weird"); class C when it is anywhere else.
_PATH_RUN = re.compile(
    r"(?ix)" + _C + r"(?:&\s*)?[\"']?(?P<path>(?:\.{1,2}[\\/]|[a-z]:[\\/]|[\\/]|~[\\/])[^\s;&|\"']+)")
_SCRIPT_RUN = re.compile(
    r"(?ix)" + _C + r"(?:python3?|py|node|bun|deno|ruby|perl|php|bash|sh|zsh|pwsh|powershell)(?:\.exe)?"
    r"(?:\s+-{1,2}[a-z][\w-]*)*\s+[\"']?(?P<path>[^\s;&|\"']+\.(?:py|js|mjs|cjs|ts|rb|pl|php|sh|bash|ps1))\b")


def _outside_workspace(raw: str, workspace: Path) -> bool:
    msys = re.match(r"^/([a-z])/(.*)$", raw, re.IGNORECASE)  # git-bash /c/Projects -> C:/Projects
    if msys:
        raw = f"{msys.group(1)}:/{msys.group(2)}"
    return not _inside(_resolve_path(raw, workspace), Path(workspace).resolve())


def danger(command: str, workspace: Path) -> str | None:
    """The danger CLASS of a shell command, or None when it is ordinary."""
    for label, pattern in _DANGER:
        if pattern.search(command):
            return label
    for pattern in (_SCRIPT_RUN, _PATH_RUN):
        for match in pattern.finditer(command):
            if _outside_workspace(match.group("path"), workspace):
                return FOREIGN_PROCESS
    return None


def classify_shell(args: Mapping[str, object], workspace: Path) -> Iterable[str]:
    if danger(str(args.get("command") or ""), workspace):
        return (DESTRUCTIVE_IRREVERSIBLE,)
    return ()


def classify_pccontrol(args: Mapping[str, object], _workspace: Path) -> Iterable[str]:
    action = str(args.get("action") or "").lower()
    if action in {"windows", "status", "screenshot"}:
        return (READ_ONLY,)
    if action == "launch":
        # Launching an application is danger class C ("weird procc runs that
        # arent its tools"); clicking and typing are its own desktop tools.
        return (DESKTOP_CONTROL, PROCESS_EXECUTION, DESTRUCTIVE_IRREVERSIBLE)
    return (DESKTOP_CONTROL,)


def classify_chrome(args: Mapping[str, object], _workspace: Path) -> Iterable[str]:
    action = str(args.get("action") or "").lower()
    if action in {"ping", "tabs", "text", "shot", "status"}:
        return (READ_ONLY, NETWORK)
    if action == "nav":
        return (NETWORK, DESKTOP_CONTROL)
    if action in {"start", "stop"}:
        return (PROCESS_EXECUTION,)
    return (DESKTOP_CONTROL,)


def classify_studio(args: Mapping[str, object], _workspace: Path) -> Iterable[str]:
    action = str(args.get("action") or "").lower()
    if action in {
        "status", "models", "config", "job", "jobs", "gallery", "backends",
        "families", "list_families", "list_models", "help",
    }:
        return (READ_ONLY, NETWORK)
    return (NETWORK, EXTERNAL_WRITE, PROCESS_EXECUTION)


def classify_listen(args: Mapping[str, object], _workspace: Path) -> Iterable[str]:
    action = str(args.get("action") or "").lower()
    if action == "status":
        return (READ_ONLY,)
    # A real listen launches llama-server and suspends the agent's own seat —
    # visible machine changes, same class as studio's generate actions.
    return (NETWORK, PROCESS_EXECUTION)


def classify_harness(args: Mapping[str, object], _workspace: Path) -> Iterable[str]:
    action = str(args.get("action") or "").lower()
    if action in {"whoami", "discover", "check"}:
        return (READ_ONLY,)
    # Sending changes another agent's state and must be a visible human choice.
    return (NETWORK, EXTERNAL_WRITE)


READ_POLICY = ToolPolicy(frozenset({READ_ONLY}), "Read-only local inspection")
NETWORK_READ_POLICY = ToolPolicy(
    frozenset({READ_ONLY, NETWORK}), "Read information over a network boundary"
)
WRITE_POLICY = ToolPolicy(
    frozenset(),
    "Write a file",
    classify_args=classify_write,
)
SHELL_POLICY = ToolPolicy(
    frozenset({PROCESS_EXECUTION}),
    "Execute a child process or shell command",
    classify_args=classify_shell,
)
PCCONTROL_POLICY = ToolPolicy(
    frozenset({READ_ONLY}),
    "Inspect or control the Windows desktop",
    classify_args=classify_pccontrol,
)
CHROME_POLICY = ToolPolicy(
    frozenset({READ_ONLY}),
    "Inspect or control the user's Chrome session",
    classify_args=classify_chrome,
)
STUDIO_POLICY = ToolPolicy(
    frozenset({READ_ONLY}),
    "Inspect or generate with local studio applications",
    classify_args=classify_studio,
)
LISTEN_POLICY = ToolPolicy(
    frozenset({READ_ONLY}),
    "Listen to audio with the local Qwen2-Audio model",
    classify_args=classify_listen,
)
HARNESS_POLICY = ToolPolicy(
    frozenset({READ_ONLY}),
    "Inspect or message the LiteHarness fleet",
    classify_args=classify_harness,
    confirm_always=False,
)
USER_QUESTION_POLICY = ToolPolicy(
    frozenset({READ_ONLY}), "Ask the user a question inside LiteTUI"
)

# MCP servers are external capability providers whose individual effects are
# not described by LiteTUI.  They are offered, but never silently trusted.
MCP_UNKNOWN_POLICY = ToolPolicy(
    frozenset({NETWORK, EXTERNAL_WRITE, PROCESS_EXECUTION}),
    "MCP capability with effects not declared to LiteTUI",
    confirm_always=True,
)
