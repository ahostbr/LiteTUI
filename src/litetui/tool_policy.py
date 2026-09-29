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
from litetui import deny_floor, paths, trusted_executables


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

    def classify(self, args: Mapping[str, object], workspace: Path, *, shell: str | None = None) -> frozenset[str]:
        caps = set(self.capabilities)
        if self.classify_args is classify_shell:
            caps.update(classify_shell(args, workspace, shell=shell))
        elif self.classify_args is not None:
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


# Interactive asks ONLY for the danger table (the user, 2026-09-24: "make
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

#: Everything, unattended, no questions. The point of the row: the user killed his
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
#: still wins — the deny gate runs before the profile is consulted at all —
#: and so does the deny floor (deny_floor.py, T1026), which no rule can lift.
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
#: `scheduled` (the read-only floor) is GONE, the user 2026-09-24: "remove
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
#: keeps its FULL profile (the user 2026-09-24: interactive powers, not a read-only
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


def inbox_turn_rule(route: str, spawner_id: str | None = None) -> str:
    """Tell an inbox-woken seat where its existing CONFIRM route actually leads."""
    if route in ("spawner", "host"):
        destination = spawner_id if route == "spawner" and spawner_id else "your host"
        return (
            "[This turn came from the inbox; nobody is at the keyboard. Act normally. "
            "Anything that needs approval will be sent to " + destination +
            " for approval; attempt it and wait for the answer.]"
        )
    return INBOX_TURN_RULE


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
    list. the user's model is Claude Code's footer, where the leading glyph tells
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
    gone (the user 2026-09-24), so every profile is on the cycle.

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
    shell: str | None = None,
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
    capabilities = policy.classify(args or {}, Path(workspace).resolve(), shell=shell)
    if policy.classify_args is classify_write:
        capabilities = frozenset(policy.capabilities) | frozenset(classify_write(args or {}, Path(workspace).resolve(), active_conversation=active_conversation))
    names = ", ".join(sorted(capabilities))
    # 🔴 THE DENY FLOOR RUNS FIRST: before the profile, before any standing
    # rule, for every turn source. Every turn (typed, inbox, cron, goal loop,
    # RPC) reaches its tools through this function, so the floor holds
    # whichever profile string the turn carries (T1027: an inbox turn and a
    # typed turn can disagree on it) and adds no prompt: it only refuses.
    if floor := _floor(args, workspace):
        return PolicyDecision(DENY, profile_name, capabilities, floor, danger=DELETION)
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
        what = _danger_of(policy, args or {}, Path(workspace).resolve(), shell=shell)
        return PolicyDecision(
            CONFIRM,
            profile.name,
            capabilities,
            f"human confirmation required for {what or names}",
            danger=what,
        )
    return PolicyDecision(ALLOW, profile.name, capabilities, f"allowed: {names}")


def _danger_of(policy: ToolPolicy, args: Mapping[str, object], workspace: Path, *, shell: str | None = None) -> str:
    """Which DANGER_TABLE class a confirm is for, in words a person reads."""
    if policy.classify_args is classify_shell:
        return danger(str(args.get("command") or ""), workspace, shell=shell) or ""
    if policy.classify_args is classify_pccontrol:
        return FOREIGN_PROCESS
    if policy.classify_args is classify_fleet_mcp:
        if str(args.get("action") or "").lower() == "launch":
            return FOREIGN_PROCESS
        for key in ("command", "cmd", "script", "code"):
            text = args.get(key)
            if isinstance(text, str) and (what := danger(text, workspace)):
                return what
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

#: T1098 — reuse the SAME command-position definition as every `_C` danger
#: row. A shell can execute 'format', "rm" or \rm; only the executable token
#: at a command position is unwrapped, never a quoted argument to printf/echo.
_QUOTED_COMMAND = re.compile(
    r"(?ix)" + _CMD_POSITION
    + r"(?:(?P<slash>\\)(?P<bare>[a-z0-9][\w.-]*)|"
    + r"(?P<quote>['\"])(?P<quoted>[a-z0-9][\w.-]*)(?P=quote))(?=[\s;&|()]|$)"
)


def _unwrap_command_verbs(command: str) -> str:
    def unwrap(match: re.Match[str]) -> str:
        prefix_group, verb_group = ("slash", "bare") if match.group("slash") else ("quote", "quoted")
        return command[match.start():match.start(prefix_group)] + match.group(verb_group)

    return _QUOTED_COMMAND.sub(unwrap, command)

#: 🔴 EVERY MATCH IS NOW AN UNSKIPPABLE PROMPT, so this pattern is held to
#: BOTH polarities. Before the floor a false positive cost an extra confirm
#: on a profile that was already confirming; after it, no profile can
#: silence one -- which is the modal the user killed a seat over. Measured,
#: both directions, in tests/test_destructive_floor.py.
#:
#: TWO CHANGES, EACH FROM A MEASUREMENT (2026-09-17):
#:
#:   FALSE POSITIVE, FIXED. `\bformat\b` matched `npm run format`,
#:   `npm run format:check`, `git log --format=%h` and `printf 'format'`.
#:   `format` now has to be at a command position; a disk wipe is
#:   `format C:`, never the third word of a script name.
#:
#:   T1094, Ryan 1ed3b84: interactive asks only for dangerous commands.
#:   Python's `\b` accepts the hyphen in PowerShell Verb-Noun names as an end
#:   of word: Format-Table, Kill-Process and Del-Item were false prompts.
#:   Bare command names must end before both word characters AND hyphens.
#:
#:   MISSES, FIXED. `rm -rf` with no target (the trailing `\s+` made the
#:   argument mandatory) and the long flags `rm --recursive --force`. Both
#:   are the same command by another spelling.
#:
#: SIX MORE ADDED 2026-09-17 ON THE USER'S WORD (liteask a-d8c7d600, "Go"): every
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
#: `git checkout -- <pathspec>` does, so the class the user named covers it -- but
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
#: the user, 2026-09-24 (via Sentinel 068bf9c7): "make interactive ask only for
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
    (DELETION, _C + r"(?:rm|del|erase|rmdir|rd|ri|unlink|shred)(?![\w-])"),
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
    #    Explicit launch verbs only; scripts and direct tool paths are ordinary.
    (FOREIGN_PROCESS, r"\b(?:start-process|invoke-item)\b"),
    (FOREIGN_PROCESS, _C + r"(?:saps|ii|start)\s"),
    (FOREIGN_PROCESS, _C + r"cmd(?:\.exe)?\s+/[ck]\b"),
    (FOREIGN_PROCESS, _C + r"(?:wscript|cscript|mshta|rundll32|regsvr32|msiexec|runas|psexec(?:64)?)\b"),
    (FOREIGN_PROCESS, r"\bschtasks\b[^;&|]*/create\b|\bregister-scheduledtask\b"),
    # ── D. dangerous system commands ─────────────────────────────────────────
    (DANGEROUS, _C + r"format(?:\.com)?(?![\w-])"),
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
    (DANGEROUS, _C + r"(?:kill|pkill|killall|taskkill|tskill)(?![\w-])|\bstop-process\b"),
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

# Explicit executables by path remain foreign launches unless their resolved
# location is trusted or the ENTIRE command is a declared read-only inspection.
# A matching basename alone can be spoofed and never grants the exception.
_PATH_RUN = re.compile(
    r"(?ix)" + _CMD_POSITION
    + r"(?:&\s*)?(?:[\"'](?P<quoted>(?:\.{1,2}[\\/]|[a-z]:[\\/]|[\\/]|~[\\/])[^\"']+)[\"']"
    + r"|(?P<bare>(?:\.{1,2}[\\/]|[a-z]:[\\/]|[\\/]|~[\\/])[^\s;&|\"']+))"
)
_TOOL_EXECUTABLES = frozenset({"python", "python3", "py", "node", "bun", "deno",
                               "pwsh", "powershell", "git", "uv", "ruff", "pytest", "npx", "pnpm"})


def _foreign_path_launch(raw: str, workspace: Path) -> bool:
    path = _resolve_path(raw, workspace)
    if path.suffix.lower() not in {"", ".exe", ".com", ".bat", ".cmd"}:
        # A second suffix must not turn an inspection-name impostor into a non-launch.
        return path.name.lower().split(".", 1)[0] in {"ffprobe", "git", "rg"}
    if path.suffix.lower() in {".bat", ".cmd"}:
        return not _inside(path, workspace.resolve())
    home = Path.home()
    roots = (workspace, home / ".claude" / "skills",
             home / ".claude" / "plugins" / "cache" / "liteharness")
    if any(_inside(path, root.resolve()) for root in roots):
        return False
    if path.stem.lower() in _TOOL_EXECUTABLES and trusted_executables.is_installed_tool(path):
        return False
    return True


def _inspection_segments(command: str, shell: str | None) -> list[list[str]] | None:
    """A finite literal argv view, not a general shell parser. Unknown syntax keeps gates."""
    segments: list[list[str]] = [[]]
    token = ""
    active = False
    quote = ""
    for ch in command.strip():
        # No expansions, script blocks, redirects or escaping in the exception. In
        # particular quoted option values must not hide an executable substitution.
        if ch in "$`<>\n\r(){}#" or (ch == "\\" and shell != "powershell"):
            return None
        if quote:
            if ch == quote:
                quote = ""
            else:
                token += ch
            continue
        if ch in "\"'":
            if active:  # concatenated shell tokens are deliberately outside this grammar
                return None
            quote = ch
            active = True
        elif ch.isspace():
            if active:
                segments[-1].append(token)
                token, active = "", False
        elif ch in ";|":
            if active:
                segments[-1].append(token)
                token, active = "", False
            if not segments[-1]:
                return None
            segments.append([])
        elif ch == "&":
            if shell != "powershell" or active or segments[-1]:
                return None
            segments[-1].append("&")
        else:
            token += ch
            active = True
    if quote:
        return None
    if active:
        segments[-1].append(token)
    return segments if all(segments) else None


def _inspection_options(args: list[str], flags: frozenset[str], values: frozenset[str]) -> list[str] | None:
    """Return operands only when every option is a known inspection option."""
    operands: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return operands + args[i + 1:]
        if arg.startswith("-"):
            key, equal, value = arg.partition("=")
            if key in flags and not equal:
                pass
            elif key in values:
                if not equal:
                    i += 1
                    if i == len(args) or args[i].startswith("-"):
                        return None
                elif not value:
                    return None
            else:
                return None
        else:
            operands.append(arg)
        i += 1
    return operands


def _read_only_inspection(command: str, shell: str | None) -> bool:
    """Declared command forms, NOT proof about an arbitrary binary's implementation.

    Ryan T0197: default/interactive read-only inspection should not prompt. Strict
    still confirms process execution. Entire-command validation means an inspection
    cannot excuse a chained write/launch; danger-table checks always run first.
    """
    segments = _inspection_segments(command, shell)
    if segments is None:
        return False
    for argv in segments:
        if argv[0] == "&":
            argv = argv[1:]
        if not argv:
            return False
        name = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if name.endswith(".exe"):
            name = name[:-4]
        args = argv[1:]
        if name == "ffprobe":
            if "--" in args:  # ffprobe is not the GNU option-parser contract
                return False
            operands = _inspection_options(args,
                frozenset({"-show_format", "-show_streams", "-show_frames", "-show_packets",
                           "-show_programs", "-show_chapters", "-show_error", "-show_versions",
                           "-count_frames", "-count_packets", "-hide_banner", "-sexagesimal",
                           "-pretty", "-unit", "-prefix", "-byte_binary_prefix"}),
                frozenset({"-v", "-loglevel", "-show_entries", "-select_streams", "-read_intervals",
                           "-of", "-print_format"}))
            if operands is None or len(operands) != 1:
                return False
        elif name == "git":
            # A foreign git must not launch a configured pager or diff/textconv tool.
            if not args or args[0] != "--no-pager":
                return False
            args = args[1:]
            if not args or args[0] not in {"status", "log", "show", "diff", "rev-parse"}:
                return False
            verb, args = args[0], args[1:]
            option_args = args[:args.index("--")] if "--" in args else args
            if verb in {"diff", "show"} or (verb == "log" and any(a in {"-p", "--patch"} for a in option_args)):
                if not {"--no-ext-diff", "--no-textconv"}.issubset(option_args):
                    return False
            flags = {
                "status": {"--short", "-s", "--branch", "-b", "--porcelain", "--untracked-files", "-uno"},
                "log": {"--oneline", "--all", "--graph", "--decorate", "--no-decorate", "-p", "--patch",
                        "--stat", "--name-only", "--name-status", "--no-ext-diff", "--no-textconv"},
                "show": {"--stat", "--name-only", "--name-status", "--no-patch", "--no-ext-diff", "--no-textconv"},
                "diff": {"--stat", "--name-only", "--name-status", "--cached", "--staged", "--check",
                         "--no-ext-diff", "--no-textconv", "--no-patch"},
                "rev-parse": {"--show-toplevel", "--abbrev-ref", "--verify", "--short", "--git-dir", "--is-inside-work-tree"},
            }
            values = {"log": {"--format", "--pretty", "--max-count", "-n"},
                      "status": {"--porcelain", "--untracked-files"}, "rev-parse": {"--short"}}
            if _inspection_options(args, frozenset(flags[verb]), frozenset(values.get(verb, set()))) is None:
                return False
        elif name == "rg":
            if _inspection_options(args,
                frozenset({"--files", "--hidden", "--no-ignore", "--no-ignore-vcs", "-n", "--line-number",
                           "-l", "--files-with-matches", "-i", "--ignore-case", "-s", "--case-sensitive",
                           "-F", "--fixed-strings", "-w", "--word-regexp", "--count", "-c", "--json",
                           "--no-heading", "--heading", "--with-filename", "-H", "--version"}),
                frozenset({"-e", "--regexp", "-g", "--glob", "--iglob", "-t", "--type", "--type-not",
                           "--max-count", "-m", "--context", "-C", "--before-context", "-B",
                           "--after-context", "-A", "--color"})) is None:
                return False
        elif shell == "powershell" and name in {"get-childitem", "get-content", "select-object"}:
            flags = {"get-childitem": {"-Recurse", "-Force", "-File", "-Directory", "-Name"},
                     "get-content": {"-Raw", "-Wait"}, "select-object": {"-Unique"}}
            values = {"get-childitem": {"-Path", "-LiteralPath", "-Filter", "-Include", "-Exclude", "-Depth"},
                      "get-content": {"-Path", "-LiteralPath", "-TotalCount", "-Tail", "-Encoding"},
                      "select-object": {"-Property", "-First", "-Last", "-Skip", "-ExpandProperty"}}
            if _inspection_options([a.lower() for a in args], frozenset(f.lower() for f in flags[name]),
                                   frozenset(v.lower() for v in values[name])) is None:
                return False
        else:
            return False
    return True


# This scanner is a conservative VIEW for the danger table, not a shell parser.
# Inert single-quoted arguments disappear, separators in double-quoted data
# cannot manufacture command positions, but $() and backticks within double
# quotes are recursively scanned as executable command text. Unknown/unclosed
# quoting returns the original span (fail CLOSED, never grant it data status).
# Unknown shell identity retains the conservative mixed-shell interpretation.
_REAL_COMMAND = re.compile(
    r"(?ix)(?:^|[;&|(]\s*|\bsudo\s+|\bxargs\s+(?:-\S+\s+)*|\s(?:-c|-command|/c)\s+)$"
)
_COMMAND_PAYLOAD = re.compile(r"(?i)(?:^|\s)(?:-c|-command|/c)\s+$")


def _command_view(command: str, shell: str | None = None) -> str:
    def scan(start: int, end: str = "", double: bool = False, depth: int = 0) -> tuple[str, int, bool]:
        # Beyond this bound, stop masking: unknown nesting is command text,
        # never a reason to silently allow a dangerous word in an argument.
        if depth >= 32:
            return command[start:], len(command), False
        out: list[str] = []
        i = start
        while i < len(command):
            ch = command[i]
            if ch == "\\" and i + 1 < len(command) and shell != "powershell":
                # Bash escapes the next character; unknown shell identity
                # conservatively leaves a double-quoted span unmasked.
                if double and command[i + 1] == '"' and shell != "bash":
                    return "".join(out) + command[i:], len(command), False
                out.append("  " if double else command[i:i + 2])
                i += 2
                continue
            if ch == "`" and shell == "powershell":
                # PowerShell backtick escapes one character, not a command.
                if i + 1 < len(command):
                    out.append("  " if double else command[i:i + 2])
                    i += 2
                else:
                    out.append(ch)
                    i += 1
                continue
            if end and ch == end:
                return "".join(out), i + 1, True
            if ch == "'" and not double:
                payload = bool(_COMMAND_PAYLOAD.search("".join(out)))
                bash_ansi_command = i > 0 and command[i - 1] == "$" and _REAL_COMMAND.search("".join(out[:-1]))
                j = i + 1
                while j < len(command) and command[j] != "'":
                    # Bash single quotes have NO escape syntax: backslash is literal.
                    j += 1
                if j == len(command):
                    return "".join(out) + command[i:], len(command), False
                quoted = command[i:j + 1]
                if payload:
                    out.append('"' + command[i + 1:j] + '"')  # quoted -c / -Command is executable code
                elif bash_ansi_command:
                    out.append(";" + command[i + 1:j])  # bash $'rm' can name a command
                elif _REAL_COMMAND.search("".join(out)):
                    out.append(quoted)  # e.g. 'format' C: is a real executable token
                else:
                    out.append(" " * len(quoted))
                i = j + 1
                continue
            if ch == '"':
                prefix = "".join(out)
                executable = bool(_COMMAND_PAYLOAD.search(prefix) or _REAL_COMMAND.search(prefix))
                inner, after, closed = scan(i + 1, '"', not executable, depth + 1)
                if not closed:
                    return "".join(out) + command[i:], len(command), False
                # An inert quoted argument contributes no command verbs. Keep
                # any executable substitutions emitted by the recursive scan.
                out.append('"' + inner + '"')
                i = after
                continue
            if ch == "$" and command[i:i + 2] == "$(":
                inner, after, closed = scan(i + 2, ")", depth=depth + 1)
                if not closed:
                    return "".join(out) + command[i:], len(command), False
                out.append("$(" + inner + ")")
                i = after
                continue
            if ch == "`":
                inner, after, closed = scan(i + 1, "`", depth=depth + 1)
                if not closed:
                    return "".join(out) + command[i:], len(command), False
                # A backtick substitution starts a fresh command even inside
                # double quotes. The semicolon is a command-position marker.
                out.append(";" + inner + " ")
                i = after
                continue
            out.append(" " if double and ch in ";&|" else (" " if double else ch))
            i += 1
        return "".join(out), i, not end

    return scan(0)[0]


def danger(command: str, workspace: Path, *, shell: str | None = None) -> str | None:
    """The danger CLASS of a shell command, or None when it is ordinary."""
    view = _command_view(command, shell)
    unwrapped = _unwrap_command_verbs(view)
    for label, pattern in _DANGER:
        if pattern.search(view) or (unwrapped != view and pattern.search(unwrapped)):
            return label
    for match in _PATH_RUN.finditer(view):
        if _foreign_path_launch(match.group("quoted") or match.group("bare"), workspace):
            if not _read_only_inspection(command, shell):
                return FOREIGN_PROCESS
    return None


def _command_text(args: Mapping[str, object] | None) -> str:
    """The command a shell call runs, as text (Codex may pass an argv list)."""
    command = (args or {}).get("command") or ""
    return command if isinstance(command, str) else " ".join(map(str, command))


def _floor(args: Mapping[str, object] | None, workspace: Path) -> str | None:
    """deny_floor's refusal for any call that carries a `command`, or None.

    ANY POLICY, NOT ONLY SHELL_POLICY (review 2198d4ab F2): an MCP shell such
    as litesuite-tools `shell` arrives under MCP_UNKNOWN_POLICY with the same
    {command, cwd} arguments.

    EVERY FOLDER THE COMMAND MAY RUN IN (F1): `workspace` is what the caller
    judged against (paths.ROOT for LiteTUI's own tools), but the bash and
    powershell tools run in Path.cwd(), the seat's --cwd, and an MCP shell may
    name its own `cwd`. The command is refused if it is refused against any of
    them. Reading the cwd is the one process-state read in this module.
    """
    command = (args or {}).get("command")
    if not command or not isinstance(command, (str, list, tuple)):
        return None
    text = _command_text(args)
    bases = [Path(workspace), Path.cwd()]
    tool_cwd = (args or {}).get("cwd")
    if isinstance(tool_cwd, str) and tool_cwd:
        bases.append(Path(workspace) / tool_cwd)   # an absolute cwd replaces workspace
    for base in dict.fromkeys(b.resolve() for b in bases):
        if reason := deny_floor.refusal(text, base):
            return reason
    return None


def classify_shell(args: Mapping[str, object], workspace: Path, *, shell: str | None = None) -> Iterable[str]:
    if danger(str(args.get("command") or ""), workspace, shell=shell):
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

# The fleet's own MCP servers are tools, not undeclared foreign capabilities.
FLEET_MCP_SERVERS = frozenset({"litesuite-tools", "VibeUE", "SOTS_MCP_CORE", "SOTS_BPGEN"})


def classify_fleet_mcp(args: Mapping[str, object], workspace: Path) -> Iterable[str]:
    """Inspect executable payload fields, not quoted data in inbox messages."""
    if str(args.get("action") or "").lower() == "launch":
        return (DESTRUCTIVE_IRREVERSIBLE,)
    if any(isinstance(args.get(key), str) and danger(args[key], workspace)
           for key in ("command", "cmd", "script", "code")):
        return (DESTRUCTIVE_IRREVERSIBLE,)
    return ()


FLEET_MCP_POLICY = ToolPolicy(
    frozenset({NETWORK, EXTERNAL_WRITE, PROCESS_EXECUTION}),
    "Fleet-owned MCP tool",
    classify_args=classify_fleet_mcp,
)


def mcp_policy_for(name: str) -> ToolPolicy:
    # Names are produced by the registry as mcp__<server identity>__<tool>.
    # Compare that exact server component, never a substring of the tool name.
    parts = name.split("__", 2)
    if len(parts) == 3 and parts[0] == "mcp" and parts[1] in FLEET_MCP_SERVERS and parts[2]:
        return FLEET_MCP_POLICY
    return MCP_UNKNOWN_POLICY


# MCP servers are external capability providers whose individual effects are
# not described by LiteTUI.  They are offered, but never silently trusted.
MCP_UNKNOWN_POLICY = ToolPolicy(
    frozenset({NETWORK, EXTERNAL_WRITE, PROCESS_EXECUTION}),
    "MCP capability with effects not declared to LiteTUI",
    confirm_always=True,
)
