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

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from litetui import deny_floor, worktree_scope

# The required vocabulary.  A tool may carry more than one capability: shell
# execution is process_execution, and a command that formats a disk is also
# destructive_irreversible.
READ_ONLY = "read_only"
#: The agent writing ITS OWN STORE — its owned `.agents/<Name>/` home:
#: memory index, soul.md, handoff.md, memories/ and conversation children. Distinct from workspace_write
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

    def classify(self, args: Mapping[str, object], workspace: Path, *, shell: str | None = None, trusted_interpreters: object = ()) -> frozenset[str]:
        caps = set(self.capabilities)
        if self.classify_args is classify_shell:
            caps.update(classify_shell(args, workspace, shell=shell, trusted_interpreters=trusted_interpreters))
        elif self.classify_args is classify_fleet_mcp:
            caps.update(classify_fleet_mcp(args, workspace, trusted_interpreters=trusted_interpreters))
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


# T0116: default/interactive asks only for destructive shell actions. Read-only
# ordinary commands and program launches never ask merely because of their
# executable's location. Undeclared MCP effects retain their separate gate.
# Rare/unrepresented shell forms ask (Owner: "Ship, rare shapes ask").
# Explicit human-selected strict supervision is deliberately MORE asking.
INTERACTIVE_PROFILE = ToolProfile(
    INTERACTIVE,
    allow=frozenset(CAPABILITIES - {DESTRUCTIVE_IRREVERSIBLE}),
    confirm=frozenset({DESTRUCTIVE_IRREVERSIBLE}),
    summary="ordinary read-only commands never ask; destructive actions and rare/unrepresented shell forms ask",
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
    summary="extra human-selected supervision: confirms even read-only commands and sensitive actions",
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
    active_agent_memory_root: Path | None = None,
    always_allow: frozenset[str] = frozenset(),
    deny: frozenset[str] = frozenset(),
    shell: str | None = None,
    seat_name: str | None = None,
    trusted_interpreters: object = (),
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
    capabilities = policy.classify(args or {}, Path(workspace).resolve(), shell=shell,
                                   trusted_interpreters=trusted_interpreters)
    if policy.classify_args is classify_write:
        capabilities = frozenset(policy.capabilities) | frozenset(classify_write(
            args or {}, Path(workspace).resolve(), active_conversation=active_conversation,
            active_agent_memory_root=active_agent_memory_root))
    names = ", ".join(sorted(capabilities))
    # 🔴 THE DENY FLOOR RUNS FIRST: before the profile, before any standing
    # rule, for every turn source. Every turn (typed, inbox, cron, goal loop,
    # RPC) reaches its tools through this function, so the floor holds
    # whichever profile string the turn carries (T1027: an inbox turn and a
    # typed turn can disagree on it) and adds no prompt: it only refuses.
    # Only the core PowerShell command route proves its interpreter. An MCP
    # name / agent-supplied shell field must never gain this exemption.
    floor_shell = "powershell" if tool_name == "powershell" and policy is SHELL_POLICY else None
    if floor := _floor(args, workspace, shell=floor_shell):
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
        # T0246: a shell command confined to the seat's OWN worktree needs no human on
        # `interactive` (Owner: "cmds inside its worktree that arent removal of the
        # tree... it should never need approval"). After the floor and the standing
        # rules above, so neither can be bypassed by it; and interactive only.
        if (profile.name == INTERACTIVE and policy.classify_args is classify_shell
                and _powershell_output_allows(args or {}, Path(workspace).resolve(), seat_name, shell)):
            return PolicyDecision(
                ALLOW, profile.name, capabilities,
                "allowed: output confined to the seat's own worktree/card scratch or system TEMP",
            )
        if (profile.name == INTERACTIVE and policy.classify_args is classify_shell
                and _own_worktree_allows(args or {}, Path(workspace).resolve(), seat_name, shell)):
            return PolicyDecision(
                ALLOW, profile.name, capabilities,
                "allowed: confined to the seat's own worktree",
            )
        what = _danger_of(policy, args or {}, Path(workspace).resolve(), shell=shell,
                          trusted_interpreters=trusted_interpreters)
        return PolicyDecision(
            CONFIRM,
            profile.name,
            capabilities,
            f"human confirmation required for {what or names}",
            danger=what,
        )
    return PolicyDecision(ALLOW, profile.name, capabilities, f"allowed: {names}")


def _danger_of(policy: ToolPolicy, args: Mapping[str, object], workspace: Path, *, shell: str | None = None, trusted_interpreters: object = ()) -> str:
    """Which DANGER_TABLE class a confirm is for, in words a person reads."""
    if policy.classify_args is classify_shell:
        return danger(str(args.get("command") or ""), workspace, shell=shell,
                      trusted_interpreters=trusted_interpreters) or ""
    if policy.classify_args is classify_fleet_mcp:
        return _fleet_danger(args, workspace, trusted_interpreters) or ""
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


def classify_write(args: Mapping[str, object], workspace: Path, *,
                   active_conversation: Path | None = None,
                   active_agent_memory_root: Path | None = None) -> Iterable[str]:
    """Narrow host-owned memory permission, never authority from tool args.

    An agent memory root comes only from the host's validated owned AgentSession.
    When supplied it REPLACES legacy conversation memory permission; transcripts,
    settings, catalog and sibling conversations never become SELF_STORE. Absent
    agent context preserves the existing legacy conversation behavior.
    Linked/reparse agent memory paths fail closed, including ancestors. Resolution
    and ownership checks are not a TOCTOU filesystem sandbox.
    """
    target = _resolve_path(args.get("path"), workspace)
    if active_agent_memory_root is not None:
        from litetui.agent_store import StoreError, _unlinked
        try:
            own = _unlinked(Path(active_agent_memory_root)).resolve()
            raw = Path(str(args.get("path") or "")).expanduser()
            checked = _unlinked(raw if raw.is_absolute() else workspace / raw)
            try:
                if checked.lstat().st_nlink > 1:
                    raise StoreError("Hardlinked agent memory is not self-store")
            except FileNotFoundError:
                pass
        except (OSError, StoreError):
            return (WORKSPACE_WRITE,) if _inside(target, workspace) else (EXTERNAL_WRITE,)
    elif active_conversation is not None:
        own = Path(active_conversation).resolve()
    else:
        own = None
    if own is not None:
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
#:   T1094, Owner 1ed3b84: interactive asks only for dangerous commands.
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
UNKNOWN_SHAPE = "an unrepresented shell construct"
OVERWRITE = "file overwrite"
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
    # Actual file writers, not mere executable launches.
    (OVERWRITE, _C + r"(?:set-content|add-content|out-file|tee|new-item|copy-item|move-item|cp|mv|copy|move|sed\s+-i)(?![\w-])"),
    # ── D. dangerous system commands ─────────────────────────────────────────
    # Install/registration writes are effects, not executable-location gates.
    (DANGEROUS, r"\bschtasks\b[^;&|]*/create\b|\bregister-scheduledtask\b"),
    (DANGEROUS, _C + r"(?:msiexec\s+/(?:i|x|a|j)|regsvr32\b)"),
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
    (DANGEROUS, r"\bgit\s+push\b"),
    (DANGEROUS, r"\bgit\s+(?:reset\b|checkout\b[^;&|]*\s--\s|filter-branch\b|filter-repo\b|reflog\s+expire\b)"),
    (DANGEROUS, (r"\bgit\s+restore\b(?:"
                 r"[^;&|]*(?:--worktree\b|(?-i:\s-W\b))|"
                 r"(?![^;&|]*(?:--staged\b|(?-i:\s-S\b)))\s+[^;&|]*\S"
                 r")")),
    (DANGEROUS, (r"\bnetsh\s+(?:advfirewall|firewall)\b|\bset-mppreference\b[^;&|]*-disable"
                 r"|\badd-mppreference\b[^;&|]*-exclusion")),
    (DANGEROUS, _C + r"sc(?:\.exe)?\s+(?:delete|config)\b"),
)
_DANGER = tuple((label, re.compile(r"(?ix)" + pattern)) for label, pattern in DANGER_TABLE)

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


# Only a literal Python -c argument gets a separate path-launch view. This
# recognizes the outer argv spelling, not Python syntax or inner launch targets.
_PYTHON_PAYLOAD = re.compile(
    r"(?ix)(?:^|[;&|(]\s*)(?:&\s*)?"
    r"(?:[\"'][^\"']*[/\\]python(?:3(?:\.\d+)?)?(?:\.exe)?[\"']"
    r"|(?:[^\s\"';&|]*[/\\])?python(?:3(?:\.\d+)?)?(?:\.exe)?)"
    r"\s+(?:(?:-X\s+[^\s]+|-W\s+[^\s]+|-[bBEsSuUqIO]+)\s+)*-c\s+$"
)
# No inner-language parser: process APIs/aliases remain conservative confirms.
_PYTHON_PROCESS = re.compile(
    r"\bsubprocess\b|\bos\s*\.\s*(?:system|popen|spawn\w*|exec\w*|startfile)\b"
    r"|\bimport\s+os\s+as\b|\bfrom\s+os\s+import\b"
)


def _command_view(command: str, shell: str | None = None, *, path_launch: bool = False) -> str:
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
                if payload and path_launch and _PYTHON_PAYLOAD.search("".join(out)):
                    # Single-quoted shell payload has no executable substitutions.
                    out.append(" " * len(quoted))
                elif payload:
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
                python_data = path_launch and bool(_PYTHON_PAYLOAD.search(prefix))
                inner, after, closed = scan(i + 1, '"', python_data or not executable, depth + 1)
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


_PS_PLAIN_STRING = r"(?:'(?:[^']|'')*'|\"[^\"$`]*\")"
_PS_READ_CMDLETS = frozenset({
    "get-content", "get-childitem", "get-item", "test-path", "select-string", "select-object",
})


def _powershell_common_literals(command: str) -> str:
    """T0340 represent only literal argument arrays and a finite Test-Path guard.

    Not a PowerShell evaluator: unsupported forms remain unchanged and meet the
    existing unknown-shape gate. Both branches are inspected, never executed.
    """
    out: list[str] = []
    i = 0
    quote = ""
    statement_start = 0
    while i < len(command):
        ch = command[i]
        if quote:
            out.append(ch)
            if ch == "`" and quote == '"' and i + 1 < len(command):
                out.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                if quote == "'" and command[i:i + 2] == "''":
                    out.append("'")
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
        if ch == "`" and i + 1 < len(command):
            out.extend(command[i:i + 2])
            i += 2
            continue
        if command[i:i + 2] == "@(" and (i == 0 or command[i - 1].isspace()):
            match = re.match(r"@\(\s*(" + _PS_PLAIN_STRING + r"(?:\s*,\s*" + _PS_PLAIN_STRING + r")*)\s*\)", command[i:])
            prefix = command[statement_start:i]
            recipients = deny_floor.shell_commands(prefix, "powershell")
            if (match and len(recipients) == 1 and recipients[0]["words"]
                    and not recipients[0]["words"][0][0].startswith(("$", "@"))
                    and recipients[0]["words"][0][0] != "."
                    and (i + len(match[0]) == len(command) or command[i + len(match[0])] in " ;|\r\n")):
                # Keep quoted literal argv, replace only commas/array delimiters.
                literals = re.findall(_PS_PLAIN_STRING, match[1])
                out.append(" ".join(literals))
                i += len(match[0])
                continue
        if not command[statement_start:i].strip() and re.match(r"(?i)if\s*\(", command[i:]):
            # All delimiters inside these finite token/body forms are quoted.
            path = r"(?:" + _PS_PLAIN_STRING + r"|[A-Za-z0-9_./\\:][A-Za-z0-9_./\\:-]*)"
            body = r"(?:" + _PS_PLAIN_STRING + r"|[^'\"{}])*"
            guard = re.match(r"(?is)if\s*\(\s*Test-Path\s+(?:(?:-Path|-LiteralPath)\s+)?(" + path
                             + r")\s*\)\s*\{(" + body + r")\}\s*(?:else\s*\{(" + body + r")\})?", command[i:])
            if guard and (i + len(guard[0]) == len(command) or command[i + len(guard[0])] in ";\r\n"):
                branches = [guard[2], *( [guard[3]] if guard[3] is not None else [])]
                ordinary = True
                for branch in branches:
                    shaped, unknown = _powershell_shape(branch)
                    parts = deny_floor.shell_commands(shaped, "powershell")
                    for part in parts:
                        argv = [word[0] for word in part["words"]]
                        if (unknown or not part["complete"] or part["redirects"] or not argv
                                or argv[0].lower() not in _PS_READ_CMDLETS
                                or any("$" in arg or "`" in arg for arg in argv[1:])
                                or any(arg.startswith('-') and any(
                                    name.startswith(arg[1:].lower().split(':')[0])
                                    for name in ('outvariable', 'ov', 'pipelinevariable', 'pv',
                                                 'errorvariable', 'ev', 'warningvariable', 'wv',
                                                 'informationvariable', 'iv')) for arg in argv[1:])):
                            ordinary = False
                    if not parts:
                        ordinary = False
                if ordinary:
                    out.append(";".join(branches))
                    i += len(guard[0])
                    continue
        out.append(ch)
        if ch in ";\r\n":
            statement_start = i + 1
        i += 1
    return "".join(out)


def _powershell_shape(command: str) -> tuple[str, bool]:
    """Approval-only view of literal PS operators; never changes the floor's input.

    Mask only represented stream merges and the all-stream prefix. Quotes and
    escapes protect operator-looking data. Expressions/control blocks remain
    opaque: representing argv is not permission to evaluate PowerShell.
    """
    chars = list(command)
    quote = ""
    unknown = False
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            if command[i:i + 2] == "''":
                i += 2
                continue
            if ch == "'":
                quote = ""
            i += 1
            continue
        if ch == "`":
            if i + 1 == len(command):
                unknown = True
            i += 2
            continue
        if ch == '"':
            quote = "" if quote == '"' else '"'
            i += 1
            continue
        if quote:
            if command[i:i + 2] == "$(":
                unknown = True
            i += 1
            continue
        if ch == "'":
            if i and command[i - 1] == "@":
                unknown = True  # here-string, not an ordinary quoted token
            quote = ch
            i += 1
            continue
        if command[i:i + 2] == '@"':
            unknown = True
        if ch in "(){}[]" or command[i:i + 2] in {"||", "|&", "<<"}:
            unknown = True
        # PS merges only streams 2..6 (or all) into success stream 1.
        merge = re.match(r"(?:[2-6]|\*)>&1(?=$|[\s;|])", command[i:])
        if merge and (i == 0 or command[i - 1].isspace()):
            chars[i:i + len(merge[0])] = " " * len(merge[0])
            i += len(merge[0])
            continue
        if ch == ">":
            if command[i:i + 2] == ">&":
                unknown = True
            if i and command[i - 1] == "*":
                chars[i - 1] = " "
            elif (i and command[i - 1].isdigit()
                  and (command[i - 1] not in "123456" or (i > 1 and command[i - 2].isdigit()))):
                unknown = True
        if ch == "<":
            unknown = True  # PS has no shell input redirect
        i += 1
    return "".join(chars), unknown or bool(quote)


_PS_ASSIGNMENT = re.compile(r"^\s*(\$(?:env:)?[A-Za-z_]\w*)\s*=\s*(.*)$", re.IGNORECASE | re.DOTALL)
_PS_SCALAR = re.compile(r"(?:\$[A-Za-z_]\w*|\$env:[A-Za-z_]\w*|[-+]?\d+(?:\.\d+)?|'(?:[^']|'')*'|\"(?:[^\"`]|`.)*\")", re.DOTALL)


def _powershell_json_reads(command: str) -> str:
    """Represent a closed language of file-backed JSON queries, never evaluate PS.

    Every statement must be a literal file read/JSON assignment or a projection
    of an earlier JSON binding. Every pipeline stage must match below in full.
    This provenance matters: arbitrary PS objects can have executable getters.
    Any unsupported statement leaves the ENTIRE command untouched, including
    its unknown-shape guard. The deny floor always receives the original input.
    """
    name = r"[A-Za-z_][A-Za-z_0-9]*"
    member = name + r"(?:\." + name + r")*"
    properties = name + r"(?:\s*,\s*" + name + r")*"
    literal = _PS_PLAIN_STRING + r"|[-+]?\d+|\$(?:true|false|null)"
    scalar = r"(?:" + literal + r")"
    array = r"@\(\s*" + scalar + r"(?:\s*,\s*" + scalar + r")*\s*\)"
    comparison = (r"\$_\." + member + r"\s+-(?:eq|ne|like|notlike|match|notmatch)\s+" + scalar
                  + r"|\$_\." + member + r"\s+-(?:in|notin)\s+" + array)
    predicate = r"(?:" + comparison + r")(?:\s+-(?:and|or)\s+(?:" + comparison + r"))*"
    stage = re.compile(
        r"\s*(?:Where-Object\s+\{\s*" + predicate + r"\s*\}"
        r"|Select-Object\s+(?:(?:-Property\s+)?" + properties
        + r"|-ExpandProperty\s+" + name + r"|-First\s+\d+)"
        r"|Sort-Object\s+(?:-Property\s+)?" + properties
        + r"|ConvertTo-Json(?:\s+-Depth\s+\d+)?(?:\s+-Compress)?"
        r"|Write-Output)\s*", re.IGNORECASE)
    # No variable/interpolated paths, provider expressions, or extra parameters.
    path = r"(?:" + _PS_PLAIN_STRING + r"|[A-Za-z0-9_./\\:][A-Za-z0-9_./\\:-]*)"
    source = re.compile(
        r"\s*(?:\$(?P<binding>" + name + r")\s*=\s*)?"
        r"(?P<read>Get-Content\s+(?:-Raw\s+)?(?:(?:-LiteralPath|-Path)\s+)?"
        + path + r"(?:\s+-Raw)?\s*\|\s*ConvertFrom-Json)\s*", re.IGNORECASE)
    projection = re.compile(r"\s*\$(?P<binding>" + name + r")(?:\." + member + r")?\s*")
    # Split only separators outside plain strings. Delimiters inside a predicate
    # or any other unsupported expression fail its full grammar match below.
    tokens = re.compile(_PS_PLAIN_STRING + r"|[;\r\n|]")
    statements: list[list[str]] = [[]]
    start = 0
    for token in tokens.finditer(command):
        if token[0] not in {";", "\r", "\n", "|"}:
            continue
        statements[-1].append(command[start:token.start()])
        if token[0] != "|":
            statements.append([])
        start = token.end()
    statements[-1].append(command[start:])
    bindings: set[str] = set()
    represented: list[str] = []
    for parts in statements:
        if len(parts) == 1 and not parts[0].strip():
            continue
        # A file source consumes two pipeline stages; everything else must be
        # a projection of a binding established by such a source in THIS call.
        read = source.fullmatch("|".join(parts[:2])) if len(parts) >= 2 else None
        if read:
            if read["binding"]:
                bindings.add(read["binding"].lower())
            represented.append(read["read"])
            tail = parts[2:]
        else:
            project = projection.fullmatch(parts[0])
            if not project or project["binding"].lower() not in bindings:
                return command
            represented.append("Write-Output 'JSON projection'")
            tail = parts[1:]
        if any(not stage.fullmatch(part) for part in tail):
            return command
    return ";".join(represented) if represented else command


def _powershell_parts(command: str) -> tuple[list[dict], bool]:
    normalized, unknown = _powershell_shape(_powershell_common_literals(_powershell_json_reads(command)))
    parts = deny_floor.shell_commands(normalized, "powershell")
    result: list[dict] = []
    for part in parts:
        assignment = _PS_ASSIGNMENT.fullmatch(part["raw"])
        if assignment:
            rhs = assignment[2].strip()
            if _PS_SCALAR.fullmatch(rhs):
                # Scalar assignment has no command verb. It still contributes
                # redirects, and path exemptions separately track its value.
                part = {**part, "words": []}
            else:
                rhs_parts = deny_floor.shell_commands(rhs, "powershell")
                if not rhs_parts:
                    unknown = True
                result.extend({**rhs_part, "ps_assignment": (assignment[1], "")} for rhs_part in rhs_parts)
                continue
        elif part["words"] and part["words"][0][0].startswith("$"):
            unknown = True
        if part["words"]:
            head = part["words"][0][0]
            if head.startswith(("$", "@")) or "::" in head:
                unknown = True
            if head.lower() == "exit" and any(not re.fullmatch(r"\$[A-Za-z_]\w*|\d+", w[0]) for w in part["words"][1:]):
                unknown = True
        result.append(part)
    for part in result:
        if part["shell"] == "powershell" and part["words"]:
            head = part["words"][0][0]
            if head.startswith(("$", "@")) or head == "." or "::" in head:
                unknown = True
    return result, unknown


def _iter_danger(command: str, workspace: Path, shell: str | None, trusted_interpreters: object = ()):
    """Classify actual executable argv/redirections, never prose arguments.

    The shared lexer supplies literal command boundaries and shell wrappers.
    Known inline Python/Node code retains bounded API matching; scripts, eval,
    aliases and dynamically computed targets remain outside this classifier.
    """
    # Core shell tools are Bash/PowerShell. cmd grammar is selected only by
    # an explicit cmd wrapper or proven cmd caller, not by single-quoted prose.
    ps_unknown = False
    if shell == "powershell":
        parts, ps_unknown = _powershell_parts(command)
    else:
        parts = deny_floor.shell_commands(command, shell or "bash")
        if shell is None and not any(part.get("heredoc") for part in parts):
            parts += deny_floor.shell_commands(command, "powershell")
    # Owner: "Ship, rare shapes ask". ONE explicit fail-closed prompt guard;
    # no claim that incomplete stream/operator representation proves inert data.
    # The mandatory jobs floor still runs separately and before this policy.
    view = _command_view(_powershell_shape(command)[0] if shell == "powershell" else command, shell)
    if (ps_unknown or any(part.get("heredoc") or not part["complete"]
            or any(op.startswith("<<") for op, _ in part["redirects"]) for part in parts)
            or re.search(r"<<|\|\||\|&|[<>]&", view)):
        if ps_unknown:
            # Preserve known executable substitutions' danger label; unknown
            # grammar still prevents any worktree/output exemption below.
            for label, pattern in _DANGER:
                if pattern.search(view):
                    yield label, -5
                    return
        yield UNKNOWN_SHAPE, -5
        return
    if (any([w[0].lower() for w in part["words"][:1]] in (["sh"], ["bash"], ["iex"], ["invoke-expression"])
            for part in parts) and "|" in command
            and re.search(r"(?i)\b(?:curl|wget|iwr|invoke-webrequest)\b", command)):
        yield DANGEROUS, -4
    for part in parts:
        argv = [word[0] for word in part["words"]]
        if any(op.startswith(">") for op, _ in part["redirects"]):
            yield OVERWRITE, -3
        if part["shell"] == "bash":
            while argv and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", argv[0]):
                argv = argv[1:]
        if not argv:
            continue
        while argv and argv[0].lower() in {"sudo", "command", "env", "xargs", "npx"}:
            argv = argv[1:]
            while argv and (argv[0].startswith("-") or "=" in argv[0]):
                takes_value = argv[0] in {"-u", "-g", "--user", "--group", "-n", "-P", "-I", "-L", "--unset"}
                argv = argv[2:] if takes_value else argv[1:]
        if not argv:
            continue
        head = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        head = head.lstrip("\\")
        args = argv[1:]
        if head == "git":
            i = 0
            while i < len(args):
                if args[i] in {"-C", "-c", "--git-dir", "--work-tree"} and i + 1 < len(args):
                    i += 2
                elif args[i].startswith("-"):
                    i += 1
                else:
                    break
            args = args[i:]
        if head in {"tee-object", "tee"} and part["shell"] == "powershell":
            variable_only = (len(args) == 2 and args[0].lower() in {"-variable", "-v"}
                             and re.fullmatch(r"[A-Za-z_]\w*", args[1]))
            if not variable_only:
                yield OVERWRITE, -6
        if head in {"git", "ffprobe"} and any(a in {"-o", "--output", "-output", "-report"} or a.startswith("--output=") for a in args):
            yield OVERWRITE, -7  # not a shell output target we can prove
        # Operand separators cannot manufacture command positions for regexes.
        view = " ".join([head, *(re.sub(r"[;&|\s]", "_", arg) for arg in args)])
        for index, (label, pattern) in enumerate(_DANGER):
            if pattern.match(view):
                yield label, index
        if head in {"python", "python3", "py", "node"}:
            option = "-e" if head == "node" else "-c"
            if option in args and args.index(option) + 1 < len(args):
                code = args[args.index(option) + 1]
                # This is executable language code, not a shell -c argument.
                for index, (label, pattern) in enumerate(_DANGER):
                    if ("shutil" in DANGER_TABLE[index][1] or "extractall" in DANGER_TABLE[index][1]) and pattern.search(code):
                        yield label, index
        if not part["complete"]:
            # Malformed/unknown quoting must not hide an obvious destructive
            # suffix. This fallback is intentionally limited to malformed input.
            for index, (label, pattern) in enumerate(_DANGER):
                if pattern.search(part["raw"]):
                    yield label, index


def danger(command: str, workspace: Path, *, shell: str | None = None, trusted_interpreters: object = ()) -> str | None:
    """The danger CLASS of a shell command, or None when it is ordinary."""
    return next((label for label, _ in _iter_danger(command, workspace, shell, trusted_interpreters)), None)


#: T0246: which DANGER_TABLE rows a command CONFINED to the seat's own worktree may
#: run without a prompt. An ALLOWLIST by row, so a row added later is NOT exempt
#: until someone decides it is (pinned in tests/test_worktree_scope.py). Deleting,
#: extracting and the explicit launch verbs are about the tree the command runs in;
#: the git history/working-tree rows are about that tree too. NOT here, because
#: confinement to a path says nothing about them: the system rows (registry,
#: shutdown, kill, firewall, scheduled tasks, services, policy), force-push, and
#: msiexec/regsvr32/runas-style launches.
_WORKTREE_SCOPED_ANCHORS: dict[str, tuple[str, ...] | None] = {
    DELETION: None,
    ARCHIVE: None,
    OVERWRITE: None,
    DANGEROUS: (r"git\s+(?:reset", r"git\s+restore", "(?:chmod|chown)"),
}
_WORKTREE_SCOPED_ROWS = frozenset(
    index for index, (label, pattern) in enumerate(DANGER_TABLE)
    if label in _WORKTREE_SCOPED_ANCHORS
    and (_WORKTREE_SCOPED_ANCHORS[label] is None
         or any(anchor in pattern for anchor in _WORKTREE_SCOPED_ANCHORS[label]))
)

#: Matches that share a scoped row (or the git history row) but act on the SHARED
#: repository, not on this tree: removing/pruning/moving a worktree (the tree
#: itself, or someone else's), deleting a branch, rewriting history or refs.
_NEVER_SCOPED = re.compile(
    r"(?ix)\bgit\b[^;&|]*\b(?:worktree\s+(?:remove|prune|move)|filter-(?:branch|repo)"
    r"|reflog\s+expire|update-ref|stash\s+(?:drop|clear))\b"
    r"|\bgit\b[^;&|]*\bbranch\b[^;&|]*(?:\s-[a-z]*d[a-z]*\b|--delete\b)"
)


def _powershell_output_allows(args: Mapping[str, object], workspace: Path,
                              seat_name: str | None, shell: str | None) -> bool:
    """Prove output destinations only; never exempt another destructive effect.

    Trusted roots come from linked-worktree identity, its own card scratch
    parent, and the host TEMP. Variables are not evaluated: only literal scalar
    assignments and TEMP/TMP expansions are tracked in statement order.
    """
    if shell != "powershell":
        return False
    command = str(args.get("command") or "")
    hits = list(_iter_danger(command, workspace, shell))
    if not hits or any(label != OVERWRITE or index not in {-3, -6} for label, index in hits):
        return False
    roots, variables = worktree_scope.output_context(workspace, seat_name)
    if not roots:
        return False
    cwd = _resolve_path(args.get("cwd") or workspace, workspace)
    parts, unknown = _powershell_parts(command)
    if unknown:
        return False

    def path_value(raw: str) -> Path | None:
        # Tokens are decoded by the literal lexer. Unknown interpolation, PS
        # providers, wildcards, drive-relative paths and ADS stay unproved.
        def expand(match):
            return variables.get(match[0].lower(), match[0])
        raw = re.sub(r"\$env:[A-Za-z_]\w*|\$[A-Za-z_]\w*", expand, raw)
        if (not raw or any(ch in raw for ch in "$`*?[]{}\x00")
                or re.match(r"^[A-Za-z]:[^/\\]", raw)
                or ":" in raw[2:] or re.match(r"^[A-Za-z]{2,}:", raw)):
            return None
        try:
            return _resolve_path(raw, cwd)
        except (OSError, ValueError):
            return None

    def safe(raw: str) -> bool:
        target = path_value(raw)
        return target is not None and any(_inside(target, root.resolve()) for root in roots)

    found = False
    for part in parts:
        # Decoding erases whether $ was quoted/escaped. Without full runtime
        # expansion semantics such targets must not gain the TEMP exception.
        if ("`" in part["raw"] or ("$" in part["raw"] and "'" in part["raw"]
                                   and not _PS_ASSIGNMENT.fullmatch(part["raw"]))):
            return False
        if part.get("ps_assignment"):
            variables[part["ps_assignment"][0].lower()] = ""
        assignment = _PS_ASSIGNMENT.fullmatch(part["raw"])
        if assignment:
            # Runtime automatic/read-only variables are not ordinary storage:
            # an assignment may fail or be overwritten by the next command.
            if assignment[1].lower() in {
                    "$home", "$pid", "$pshome", "$pwd", "$error", "$args", "$_", "$input",
                    "$this", "$null", "$true", "$false", "$lastexitcode", "$psitem",
                    "$pscommandpath", "$psscriptroot", "$myinvocation", "$psboundparameters",
                    "$executioncontext", "$host", "$matches", "$nestedpromptlevel",
                    "$stacktrace", "$ofs", "$shellid", "$profile", "$psversiontable",
                    "$psculture", "$psuiculture", "$psdebugcontext", "$pscmdlet",
                    "$iswindows", "$islinux", "$ismacos", "$iscoreclr", "$enabledexperimentalfeatures"}:
                return False
            literal = deny_floor.shell_commands(assignment[2], "powershell")
            words = literal[0]["words"] if len(literal) == 1 else []
            # Single quotes are literal, so do not expand their dollar signs.
            value = words[0][0] if len(words) == 1 else ""
            variables[assignment[1].lower()] = value if "$" not in value else ""
        argv = [word[0] for word in part["words"]]
        head = argv[0].lower() if argv else ""
        if "\\" in head:
            return False  # module-qualified state changes are not represented
        # Common parameters can replace literal variables without assignment
        # syntax. Do not speculate about cmdlet binding/abbreviations here.
        variable_parameters = {
            "outvariable", "pipelinevariable", "errorvariable", "warningvariable", "informationvariable",
        }
        for arg in argv[1:]:
            parameter = re.split(r"[:=]", arg.lower().removeprefix("-"), maxsplit=1)[0]
            if (arg.startswith("-") and parameter
                    and (parameter in {"ov", "pv", "ev", "wv", "iv"}
                         or any(name.startswith(parameter) for name in variable_parameters))):
                return False
        if (part["shell"] != "powershell" or head in {
                "popd", "pop-location", "set-variable", "sv", "new-variable", "nv",
                "set-item", "si", "new-item", "ni", "set-itemproperty", "sp",
                "pwsh", "powershell", "cmd", "bash", "sh"}):
            return False  # unrepresented location/environment or child-shell state
        if head in {"cd", "chdir", "set-location", "sl", "pushd", "push-location"}:
            operands = argv[1:]
            if operands and operands[0].lower() in {"-path", "-literalpath"}:
                operands = operands[1:]
            target = path_value(operands[0]) if len(operands) == 1 else None
            if target is None or not target.is_dir():
                return False  # failed cd would leave relative writes elsewhere
            cwd = target
        for op, word in part["redirects"]:
            if op.startswith(">"):
                # A single quoted $env:TEMP is a literal filename, not TEMP.
                spelling = part["raw"]  # token offsets may belong to an RHS
                if "$" in word[0] and ("'" in spelling):
                    return False
                if not safe(word[0]):
                    return False
                found = True
        if head in {"tee", "tee-object"}:
            operands = argv[1:]
            if operands and operands[0].lower() in {"-variable", "-v"}:
                if len(operands) != 2 or not re.fullmatch(r"[A-Za-z_]\w*", operands[1]):
                    return False  # scoped/unsupported names may alias another variable
                variables.pop("$" + operands[1].lower(), None)
                continue
            if operands and operands[0].lower() in {"-filepath", "-literalpath"}:
                operands = operands[1:]
            operands = [v for v in operands if v.lower() != "-append"]
            if len(operands) != 1 or not safe(operands[0]):
                return False
            found = True
    return found


def _own_worktree_allows(args: Mapping[str, object], workspace: Path, seat_name: str | None,
                         shell: str | None) -> bool:
    """True when every danger this shell command hits is a scoped row AND the whole
    command is confined to a worktree this seat owns (see worktree_scope)."""
    command = str(args.get("command") or "")
    hits = list(_iter_danger(command, workspace, shell))
    if not hits:
        return False
    if shell == "powershell" and any(index in {-3, -6} for _, index in hits):
        # The dedicated output proof has already failed. The older broad
        # path scanner must not turn unproved output variables/cd into ALLOW.
        return False
    # The table's command-position anchor has no multiline flag, so scanning the text
    # whole misses a command that starts a LINE. The exemption widens what is allowed,
    # so it also judges every line on its own (heredoc bodies are data, not lines):
    # a system row on line 2 must not ride on a scoped row on line 1. Card T0251 fixes
    # the table itself; this stays as the exemption's own guarantee.
    lines = worktree_scope.command_lines(command)
    if lines is None:
        return False
    for line in lines:
        hits.extend(_iter_danger(line.strip(), workspace, shell))
    if any(index not in {-1, -3} and index not in _WORKTREE_SCOPED_ROWS for _, index in hits):
        return False
    if _NEVER_SCOPED.search(_command_view(command, shell)):
        return False
    roots = worktree_scope.own_roots(workspace, seat_name)
    if not roots:
        return False
    start = workspace
    tool_cwd = args.get("cwd")
    if isinstance(tool_cwd, str) and tool_cwd:
        start = workspace / tool_cwd          # an absolute cwd replaces the workspace
    return worktree_scope.confined(command, roots, start,
                                   deleting=any(label == DELETION for label, _ in hits))


def _command_text(args: Mapping[str, object] | None) -> str:
    """The command a shell call runs, as text (Codex may pass an argv list)."""
    command = (args or {}).get("command") or ""
    return command if isinstance(command, str) else " ".join(map(str, command))


def _floor(args: Mapping[str, object] | None, workspace: Path, *,
           shell: str | None = None) -> str | None:
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
        # T1085: ownership-only jobs guard lives in seat_authority.
        # core_tools.tool_powershell accepts a string and calls a PowerShell
        # executable with -Command, shell=False. An argv-list is not that route.
        if reason := deny_floor.refusal(text, base, jobs=False, shell=shell if isinstance(command, str) else None):
            return reason
    return None


def classify_shell(args: Mapping[str, object], workspace: Path, *, shell: str | None = None, trusted_interpreters: object = ()) -> Iterable[str]:
    if danger(str(args.get("command") or ""), workspace, shell=shell, trusted_interpreters=trusted_interpreters):
        return (DESTRUCTIVE_IRREVERSIBLE,)
    return ()


def classify_pccontrol(args: Mapping[str, object], _workspace: Path) -> Iterable[str]:
    action = str(args.get("action") or "").lower()
    if action in {"windows", "status", "screenshot"}:
        return (READ_ONLY,)
    if action == "launch":
        return (DESKTOP_CONTROL, PROCESS_EXECUTION)
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


def _fleet_danger(args: Mapping[str, object], workspace: Path, trusted_interpreters: object = ()) -> str | None:
    for key in ("command", "cmd", "script", "code"):
        text = args.get(key)
        if not isinstance(text, str):
            continue
        # A code field is executable language code, not shell argv. Retain the
        # bounded known destructive API recognition without scanning inbox prose.
        if key == "code":
            for index, (label, pattern) in enumerate(_DANGER):
                if ("shutil" in DANGER_TABLE[index][1] or "extractall" in DANGER_TABLE[index][1]) and pattern.search(text):
                    return label
        if what := danger(text, workspace, trusted_interpreters=trusted_interpreters):
            return what
    return None


def classify_fleet_mcp(args: Mapping[str, object], workspace: Path, *, trusted_interpreters: object = ()) -> Iterable[str]:
    """Inspect executable payload fields, not quoted data in inbox messages."""
    if _fleet_danger(args, workspace, trusted_interpreters):
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
