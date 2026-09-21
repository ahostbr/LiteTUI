"""Session-state transfer and the idle-commit gate for a plugin-reload swap.

WS7 Slice — the building blocks that sit between a validated candidate
(``litetui.plugin_reload.stage_candidate``) and the eventual swap. This module
does NOT swap, does NOT assign anything onto the app, and does NOT invalidate
any provider cache. It only:

  1. carries the conversation-visible SESSION STATE from the live registry onto
     a validated candidate — and only that state, never the capability tables
     (host-row ownership is unresolved; see ws7-swap-invariants INV-S4); and
  2. answers "is it safe to commit right now?" from an EXPLICIT, immutable
     snapshot of activity the caller supplies — this module never introspects
     the app, so the gate is pure, testable, and cannot race live state.

No source side effects: ``live`` is read-only, and the only object mutated is
the throwaway candidate handed in by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass

from litetui.plugins import PluginRegistry


@dataclass(frozen=True)
class ActivitySnapshot:
    """Immutable liveness flags supplied by the caller.

    The caller measures the app's activity and hands in booleans; this module
    never reaches into the app for them. A snapshot is a value, so the gate that
    reads it cannot observe a different state than the caller intended.
    """

    turn_active: bool = False
    tool_active: bool = False
    management_active: bool = False
    children_active: bool = False
    mcp_active: bool = False

    def __post_init__(self) -> None:
        # Fail closed at the boundary: a flag that is not EXACTLY a bool (None,
        # a string, 1) must never slip through as a falsy "not active" and
        # permit a commit. `type(v) is not bool` also rejects ints, since bool
        # is an int subclass and 1/0 are not valid activity flags.
        for f in ("turn_active", "tool_active", "management_active",
                  "children_active", "mcp_active"):
            v = getattr(self, f)
            if type(v) is not bool:
                raise TypeError(
                    f"ActivitySnapshot.{f} must be a bool, got {type(v).__name__}")


#: (flag attribute, reason shown when it defers). One row per idle blocker from
#: ws7-swap-invariants group B. Defer, never cancel: a reload waits for these,
#: it does not end them.
_BLOCKERS: tuple[tuple[str, str], ...] = (
    ("turn_active", "a model turn / stream is in progress"),
    ("tool_active", "a tool call is executing"),
    ("management_active", "a management operation is in progress"),
    ("children_active", "owned children / child-delivery are in flight"),
    ("mcp_active", "an MCP connection operation is in progress"),
)


def blocking_reasons(activity: ActivitySnapshot) -> tuple[str, ...]:
    """Every reason a commit must defer right now, in blocker order. Empty means
    clear to commit. The caller reports these to the operator and retries; it
    never cancels the activity to make room for the reload."""
    # `is not False` (not truthiness): a flag is a blocker unless it is exactly
    # False. ActivitySnapshot already enforces bool, so this is belt-and-braces
    # — anything that is not the literal False fails closed into "blocked".
    return tuple(reason for flag, reason in _BLOCKERS if getattr(activity, flag) is not False)


def can_commit_candidate(activity: ActivitySnapshot) -> bool:
    """ELIGIBILITY ONLY. True when nothing in the snapshot blocks a commit right
    now — it does not commit anything, does not swap, and does not guarantee a
    later commit will succeed or even be attempted. It is the caller's gate, not
    the act."""
    return not blocking_reasons(activity)


def transfer_session_state(live: PluginRegistry, candidate: PluginRegistry) -> None:
    """Carry the conversation-visible session state from ``live`` onto a
    validated ``candidate`` — and ONLY that state.

    Copied:
      - ``activated``: a NEW set, not an alias. The candidate must be able to
        diverge without touching the live generation (and, by extension, a
        sibling App's generation is never reachable from here at all).
      - ``deferred_static``: a frozenset, immutable, so sharing the reference is
        safe.
      - ``defer_dynamic``: a bool value.
      - ``tools_disabled``: the live provider CALLABLE is shared on purpose —
        it is the user's denylist source, and the point of keeping it live is
        that unchecking a tool still takes effect after a reload. A snapshot
        would freeze the denylist at swap time.

    Deliberately NOT copied: tools, dynamic, commands, palette_rows,
    prompt_sections, observers, turn_finalizers, status. Those tables mix
    plugin-owned and host-owned rows, and host-row ownership is unresolved
    (INV-S4); copying them blindly would duplicate or misattribute rows.

    Mutates only ``candidate``. ``live`` is read, never modified.
    """
    if live is candidate:
        # Fail closed: transferring a registry onto itself would alias
        # activated to itself and defeat the copy-not-alias guarantee, and it
        # signals a caller that lost track of which generation is which.
        raise ValueError("transfer_session_state: live and candidate are the same registry")
    candidate.activated = set(live.activated)        # copied, not aliased
    candidate.deferred_static = live.deferred_static  # frozenset: immutable, safe to share
    candidate.defer_dynamic = live.defer_dynamic
    candidate.tools_disabled = live.tools_disabled    # the live provider, intentionally shared
