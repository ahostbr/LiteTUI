"""Handler-generation swap for the WS7 plugin-reload full scope.

Extends the metadata-only bounded slice (``plugin_reload_commit.
commit_metadata_candidate``, which REFUSES handler changes) with the risky
generation swap: replace a plugin's handlers with a new generation's at a safe
idle boundary, joining the old generation's owned resources first so nothing
duplicates or orphans (sub-plugin-reload.md slice "Swap schema and handler
generations together at a safe idle boundary" + "Explicitly deactivate and join
old plugin-owned resources before activating replacements").

This module CONSUMES the eligibility + lifecycle-seam contract defined in
``plugin_reload_eligibility`` (``d8e9030``): the caller has already re-imported
the replacement modules, built a validated candidate registry with the new
handlers, and assessed each swapped owner's eligibility. This module does the
swap. It does NOT import or re-import modules and does NOT build the candidate --
those are the caller's / a later slice's responsibility.

TWO HARD INVARIANTS (pinned by test_plugin_reload_swap.py):

  1. NO ROLLBACK IS EVER CLAIMED. Once the candidate pointer is installed the new
     generation's modules are live and may have mutated module/process globals
     that cannot be restored, so the outcome is ``swapped`` (clean) or
     ``degraded`` (activate failed after the swap -> restart required). The
     result NEVER says "restored" / "rolled back".

  2. THE OLD GENERATION IS PRESERVED UNTIL THE POINT OF NO RETURN. Every pre-swap
     check (eligibility, generation, native, idle, validate) runs before ANY
     mutation seam; a failure there returns ``deferred`` / ``failed`` /
     ``restart-required`` with the old generation untouched and NO deactivate or
     activate called. Deactivate-old runs before the pointer swap; activate-new
     runs after it.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from litetui.plugin_reload_state import ActivitySnapshot, blocking_reasons


@dataclass(frozen=True)
class SwapSeams:
    """The three seams the swap calls for one swapped owner.

    ``validate_new``   -- pre-swap safety gate; raising refuses the swap (old gen
                          untouched). Called before any mutation seam.
    ``deactivate_old`` -- join the OLD generation's owned resources (timers,
                          processes, MCP connections, durable children) so nothing
                          leaks or duplicates. Called BEFORE the pointer swap.
    ``activate_new``   -- bring the NEW generation's resources online. Called
                          AFTER the pointer swap (a failure here is degraded).
    """

    validate_new: Callable[[], object]
    deactivate_old: Callable[[], object]
    activate_new: Callable[[], object]


@dataclass(frozen=True)
class SwapResult:
    status: str   # swapped | degraded | deferred | failed | restart-required
    reasons: tuple[str, ...] = ()
    swapped: bool = False   # True only when the pointer was installed (no rollback)


def _changed_handler_owners(expected: Any, candidate: Any) -> set[str]:
    """Owners of EXISTING tools whose handler (``run``) changed. A tool name/owner
    that is absent from the live generation is NOT a handler swap (new-tool path)."""
    live = {e.name: e for e in expected.tools}
    out: set[str] = set()
    for e in candidate.tools:
        prior = live.get(e.name)
        if prior is not None and prior.run is not e.run:
            out.add(e.owner)
    return out


def commit_handler_candidate(
    app: Any,
    expected: Any,
    candidate: Any,
    *,
    activity: Callable[[], Any],
    eligible: dict[str, bool],
    swap_seams: dict[str, SwapSeams],
) -> SwapResult:
    """Swap handler generations at an idle boundary. See the module docstring for
    the no-rollback + preserve-until-no-return invariants.

    ``eligible`` maps each swapped owner to its handler-reload eligibility (from
    ``plugin_reload_eligibility.assess_reload_eligibility``). ``swap_seams`` maps
    each swapped owner to its ``SwapSeams``. An owner whose handler changed but
    is not eligible, or has no swap seams, is refused (restart-required) before
    anything runs.
    """
    changed = sorted(_changed_handler_owners(expected, candidate))
    if not changed:
        return SwapResult('no-op', ('no handler changes; nothing to swap',), swapped=False)

    # 1. Eligibility + seam-availability gate (before any check or seam).
    for owner in changed:
        if not eligible.get(owner):
            return SwapResult('restart-required',
                              (f'{owner}: not handler-reload-eligible',))
        if owner not in swap_seams:
            return SwapResult('restart-required',
                              (f'{owner}: no lifecycle seams supplied',))

    # 2. Generation identity, native boundary, idle boundary (all read-only).
    if app.plugins is not expected:
        return SwapResult('deferred', ('Registry generation changed; stage again.',))
    if hasattr(app.backend, 'app_server'):
        return SwapResult('restart-required',
                          ('Native Codex thread inventory cannot be replaced in place.',))
    try:
        snapshot = activity()
    except Exception as e:  # noqa: BLE001
        return SwapResult('failed', (f'Activity evidence unavailable ({type(e).__name__})',))
    if not isinstance(snapshot, ActivitySnapshot):
        return SwapResult('failed', ('Activity evidence unavailable',))
    reasons = blocking_reasons(snapshot)
    if reasons:
        return SwapResult('deferred', tuple(reasons))

    # 3. Pre-swap validate (NO mutation seam yet; old gen preserved on failure).
    for owner in changed:
        try:
            swap_seams[owner].validate_new()
        except Exception as e:  # noqa: BLE001
            return SwapResult('failed',
                              (f'{owner}: validate refused the swap ({type(e).__name__})',))

    # 4. Deactivate old (join old resources) BEFORE the pointer swap. A failure
    #    here has NOT crossed the point of no return, so the old generation is
    #    preserved; any partial teardown is reported degraded, never rolled back.
    deactivated: list[str] = []
    for owner in changed:
        try:
            swap_seams[owner].deactivate_old()
        except Exception as e:  # noqa: BLE001
            partial = (f'; partial teardown of {deactivated} -- restart required'
                       if deactivated else '; old generation preserved')
            return SwapResult('failed',
                              (f'{owner}: deactivate failed ({type(e).__name__}){partial}',))
        deactivated.append(owner)

    # 5. POINT OF NO RETURN: install the candidate. No rollback from here on.
    app.plugins = candidate

    # 6. Activate new. A failure here leaves the new generation partially live --
    #    degraded, restart required, and explicitly NOT rolled back.
    for owner in changed:
        try:
            swap_seams[owner].activate_new()
        except Exception as e:  # noqa: BLE001
            return SwapResult('degraded',
                              (f'{owner}: activate failed after swap '
                               f'({type(e).__name__}); restart required; no rollback',),
                              swapped=True)
    return SwapResult('swapped', (), swapped=True)
