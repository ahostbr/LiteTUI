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
SCHEDULED = "scheduled"
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
    #: Can a human CHOOSE this as their conversation authority?
    #:
    #: T085, Ryan: "scheduled should not be its own mode". `scheduled` remains
    #: the read-only FLOOR that `unattended()` degrades to -- it is a mechanism,
    #: not a mode -- but shift+tab and the settings dropdown must not offer it.
    #:
    #: 🔴 A FLAG ON THE PROFILE, NOT A SECOND TUPLE OF NAMES. `SELECTABLE_
    #: PROFILES` is derived from this, so a profile added later declares its own
    #: visibility next to its own behaviour and there is nothing to keep in
    #: agreement. Same reason PROFILE_NAMES is derived from PROFILES.
    selectable: bool = True


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    profile: str
    capabilities: frozenset[str]
    reason: str

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.action == CONFIRM


# Interactive conversation turns may inspect the machine and fetch information
# without ceremony.  Mutating the machine, launching processes, or controlling
# another UI is visible to the host and therefore requires Ryan's confirmation.
INTERACTIVE_PROFILE = ToolProfile(
    INTERACTIVE,
    allow=frozenset({READ_ONLY, NETWORK, SELF_STORE}),
    confirm=frozenset(
        {
            WORKSPACE_WRITE,
            EXTERNAL_WRITE,
            PROCESS_EXECUTION,
            DESKTOP_CONTROL,
            DESTRUCTIVE_IRREVERSIBLE,
        }
    ),
    summary="inspect freely, confirm sensitive actions",
)

# Scheduled prompts are unattended.  Their default is intentionally narrower:
# inspection is allowed, every other authority is refused rather than opening a
# modal nobody is present to answer.
SCHEDULED_PROFILE = ToolProfile(
    SCHEDULED,
    allow=frozenset({READ_ONLY, SELF_STORE}),
    confirm=frozenset(),
    summary="read-only tools only",
    # NOT user-selectable since T085 -- the floor, not a mode. See
    # ToolProfile.selectable.
    selectable=False,
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
#: ⚠️ THIS PROFILE HAS NO CONFIRM STEP, SO THE ONLY THING BETWEEN IT AND ANY
#: TOOL IS A STANDING `deny` RULE. Those still win — the deny gate runs before
#: the profile is consulted at all — and that is the one brake left.
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
#: 📌 This REORDERS an existing dropdown (interactive/scheduled becomes
#: scheduled/interactive). Held out of the derivation commit deliberately, so a
#: reviewer could verify that one changed nothing visible; it belongs here,
#: with the change that makes ordering matter.
PROFILES = {
    SCHEDULED: SCHEDULED_PROFILE,
    INTERACTIVE: INTERACTIVE_PROFILE,
    AUTONOMOUS: AUTONOMOUS_PROFILE,
}

#: Derived, never hand-written. `tuple` so it stays immutable and ordered.
PROFILE_NAMES = tuple(PROFILES)

def selectable_profile_names() -> tuple[str, ...]:
    """The levels a human may CHOOSE -- shift+tab and the settings dropdown.

    Derived from `ToolProfile.selectable`, so this can never disagree with the
    profiles themselves: `scheduled` is absent by DECLARATION, not by omission.

    🔴 A FUNCTION, NOT A MODULE CONSTANT, AND I SHIPPED THE CONSTANT FIRST.
    `settings_screen.tool_profile_choices` already carried this exact warning
    -- "a constant is computed once at import and a test cannot then add a
    profile and watch it appear, which is the property that makes the drift
    impossible rather than merely fixed" -- and I introduced a constant one
    module away and broke three of its tests. Same shape as the class list in
    side_panel: the rule was written down, in a file I had read, about the very
    thing I was doing.
    """
    return tuple(name for name, p in PROFILES.items() if p.selectable)


def unattended(profile_name: str) -> str:
    """The profile a turn NOBODY IS WATCHING actually runs under.

    Honours the human's choice, with one mechanical exception: **a profile
    whose mechanism is ASKING cannot be honoured when there is nobody to
    ask.** It degrades to the read-only floor rather than opening a modal
    against an empty room — the hang `evaluate` documents at its `profile.
    confirm` guard, arriving by the other door.

    🔴 THE TEST IS `.confirm`, NEVER THE PROFILE NAME. `interactive` is not
    special; it is simply the profile that currently has a confirm set. A
    fourth profile added later is classified by WHAT IT DOES, so this cannot
    become a second table that has to agree with `PROFILES`.

    ⚠️ THE DEGRADE DIRECTION IS DOWN, NEVER UP. An unrecognised name (a
    hand-edited settings.json, a profile removed in a later version) also
    returns the floor. `evaluate` would deny it outright anyway; returning
    SCHEDULED here means such a turn can still read and still write its own
    store, instead of failing every tool call it makes.
    """
    profile = PROFILES.get(profile_name)
    if profile is None or profile.confirm:
        return SCHEDULED
    return profile_name


def stops_you(profile_name: str) -> bool:
    """Does this level ever STOP the agent -- by asking, or by refusing?

    The footer glyph is derived from this rather than from a table of names,
    for the reason `PROFILE_NAMES` is derived: a fourth profile must be
    classified by WHAT IT DOES, not by having been remembered in a second
    list. Ryan's model is Claude Code's footer, where the leading glyph tells
    you at a glance whether this level will interrupt you, without reading the
    words.

    📌 NOTE THIS IS NOT `profile.confirm`. `scheduled` has an EMPTY confirm set
    and still stops you constantly -- it refuses. Asking and refusing are both
    interruptions from the user's side, and the glyph answers the user's
    question ("will this run?"), not the implementation's.
    """
    profile = PROFILES.get(profile_name)
    if profile is None:
        return True  # unknown authority is not something to advertise as free
    return profile.allow < CAPABILITIES


def cycle(profile_name: str) -> str:
    """shift+tab: one step DOWN the authority scale, wrapping.

    Ryan's order, from his own screenshots of Claude Code:
    autonomous -> interactive -> scheduled -> autonomous.

    Since T085 it walks `SELECTABLE_PROFILE_NAMES`, not every profile: Ryan
    ruled that "scheduled should not be its own mode", so the cycle is
    autonomous <-> interactive and the floor is unreachable from the keyboard.
    That set is DERIVED from `ToolProfile.selectable`, so removing a level from
    the cycle is one flag on the profile rather than an edit here.

    `PROFILES` is ordered by authority ASCENDING, so descending is that same
    order stepped backwards -- the direction is expressed ONCE here rather
    than as a second hand-written tuple that has to agree with `PROFILES`.

    ⚠️ AN UNKNOWN CURRENT VALUE LANDS ON THE FLOOR, NOT ON THE DEFAULT. This
    returned AUTONOMOUS -- the WIDEST authority -- when settings held a name
    that is not a profile. Nothing was actually granted by it (`evaluate`
    denies an unknown profile outright, so such a turn has no authority to
    escalate FROM), but it is the same permissive-on-an-error-path shape that
    T084 removed from three `app.py` fallbacks in the same commit, and it
    contradicted `unattended()`, which sends unknown DOWN. Corrupt settings
    plus one keypress should not be a route to full authority.
    """
    order = selectable_profile_names()
    if profile_name not in order:
        # Includes `scheduled` itself, which is no longer selectable: someone
        # arriving on it (an old settings.json, or the floor written back by an
        # earlier build) cycles INTO the selectable set rather than being stuck
        # outside it. Lands on the narrowest selectable level, never the widest.
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
        return PolicyDecision(
            CONFIRM,
            profile.name,
            capabilities,
            f"human confirmation required for {names}",
        )
    return PolicyDecision(ALLOW, profile.name, capabilities, f"allowed: {names}")


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


def classify_write(args: Mapping[str, object], workspace: Path) -> Iterable[str]:
    """Three answers, not two: the agent's own store, the workspace, elsewhere.

    🔴 THE SELF-STORE CHECK MUST COME FIRST, because `.convos` lives INSIDE the
    workspace — asking "is it in the workspace?" first would answer yes for
    every self-store write and the third case would be unreachable.

    The store is read from `paths.CONVO_DIR` — the ONE anchor — rather than
    rebuilt as `workspace / ".convos"`. That second spelling would agree with
    the real store only by coincidence, which is the drift pair this codebase
    keeps paying for.

    Escapes are handled by `_resolve_path`, which resolves before either test:
    `.convos/<id>/../../src/x.py` resolves out of the store and is classified
    as the workspace write it actually is.
    """
    target = _resolve_path(args.get("path"), workspace)
    if _inside(target, Path(paths.CONVO_DIR).resolve()):
        return (SELF_STORE,)
    return (WORKSPACE_WRITE,) if _inside(target, workspace) else (EXTERNAL_WRITE,)


_DESTRUCTIVE_COMMAND = re.compile(
    r"(?ix)(?:"
    r"\brm\s+(?:-[a-z]*[rf][a-z]*\s+)+|"
    r"\b(?:del|erase|rmdir|rd)\b|"
    r"\bremove-item\b|"
    r"\bformat(?:\.com)?\b|"
    r"\bdiskpart\b|"
    r"\bgit\s+(?:clean\b|reset\s+--hard\b)|"
    r"\b(?:shutdown|restart-computer|stop-computer)\b"
    r")"
)


def classify_shell(args: Mapping[str, object], _workspace: Path) -> Iterable[str]:
    command = str(args.get("command") or "")
    if _DESTRUCTIVE_COMMAND.search(command):
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
