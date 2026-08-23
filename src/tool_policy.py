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


# The required vocabulary.  A tool may carry more than one capability: shell
# execution is process_execution, and a command that formats a disk is also
# destructive_irreversible.
READ_ONLY = "read_only"
WORKSPACE_WRITE = "workspace_write"
EXTERNAL_WRITE = "external_write"
PROCESS_EXECUTION = "process_execution"
NETWORK = "network"
DESKTOP_CONTROL = "desktop_control"
DESTRUCTIVE_IRREVERSIBLE = "destructive_irreversible"

CAPABILITIES = frozenset(
    {
        READ_ONLY,
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
PROFILE_NAMES = (INTERACTIVE, SCHEDULED)

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
    allow=frozenset({READ_ONLY, NETWORK}),
    confirm=frozenset(
        {
            WORKSPACE_WRITE,
            EXTERNAL_WRITE,
            PROCESS_EXECUTION,
            DESKTOP_CONTROL,
            DESTRUCTIVE_IRREVERSIBLE,
        }
    ),
)

# Scheduled prompts are unattended.  Their default is intentionally narrower:
# inspection is allowed, every other authority is refused rather than opening a
# modal nobody is present to answer.
SCHEDULED_PROFILE = ToolProfile(
    SCHEDULED,
    allow=frozenset({READ_ONLY}),
    confirm=frozenset(),
)

PROFILES = {
    INTERACTIVE: INTERACTIVE_PROFILE,
    SCHEDULED: SCHEDULED_PROFILE,
}


def evaluate(
    profile_name: str,
    policy: ToolPolicy,
    args: Mapping[str, object] | None,
    workspace: Path,
) -> PolicyDecision:
    """Return the host action for one proposed tool call.

    Unknown profiles fail closed.  A settings typo must never turn scheduled
    work into an unrestricted interactive turn.
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

    outside = capabilities - profile.allow - profile.confirm
    if outside:
        return PolicyDecision(
            DENY,
            profile.name,
            capabilities,
            f"{profile.name} profile does not grant {', '.join(sorted(outside))}",
        )
    if policy.confirm_always or capabilities & profile.confirm:
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
    target = _resolve_path(args.get("path"), workspace)
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
