"""Orchestration for one plugin's handler-generation reload (WS7 full scope).

This is the "consume the contract" slice: it ties the three in-gate building
blocks into a single, fail-closed reload of ONE plugin's handlers, so that
together they cover the spec's handler-reload matrix (minus the per-plugin
owned-resource teardown mapping and the command wiring, which are App state):

  1. ``load_fresh_generation`` (7cfd2b6) -- load the new handlers into a FRESH
     module object; the live module is never mutated, so a failure here is a
     clean "live generation preserved, no rollback" refusal.
  2. ``stage_candidate`` (plugin_reload) -- build and validate a candidate
     registry with the fresh manifest swapped in for the reloaded owner; every
     other owner is re-registered unchanged. Authority/schema/duplicate issues
     keep the live registry intact.
  3. ``commit_handler_candidate`` (fae1c3c) -- the idle-bounded, no-rollback
     swap (eligibility gate -> validate -> deactivate-old -> swap -> activate-new).

The caller supplies ``eligible`` (per owner; produced by
``plugin_reload_eligibility.assess_reload_eligibility``) and ``swap_seams``
(per owner; the live generation's teardown + the new generation's
validate/activate). Mapping a real plugin's owned resources (timers,
processes, MCP, children) onto those seams is per-plugin App state and is
deliberately OUTSIDE this unit's trust boundary -- injecting it is what keeps
this composition testable fake-only.

Nothing here imports/reloads a real module or touches App-owned resources; the
source string and the seams are injected. The only mutation on success is the
registry pointer swap performed by ``commit_handler_candidate``.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from litetui.plugin_reload import stage_candidate
from litetui.plugin_reload_generation import load_fresh_generation
from litetui.plugin_reload_swap import SwapSeams, commit_handler_candidate


@dataclass(frozen=True)
class HandlerReloadResult:
    """The outcome of one handler-generation reload.

    ``status`` is the swap's status (swapped / degraded / deferred / failed /
    restart-required / no-op). ``token`` is the fresh generation's identity --
    present whenever the fresh generation loaded, even when a later stage
    refused; ``None`` only when the fresh load itself failed. ``swapped`` is
    True only when the pointer was installed (never implies rollback).
    """

    status: str
    reasons: tuple[str, ...] = ()
    token: str | None = None
    swapped: bool = False


def reload_handler_generation(
    app: Any,
    *,
    module_name: str,
    source: str,
    live_manifests,
    reloaded_owner: str,
    reviewed_owners,
    eligible: dict[str, bool],
    swap_seams: dict[str, SwapSeams],
    activity: Callable[[], Any],
    approved_new_tools: frozenset[str] = frozenset(),
) -> HandlerReloadResult:
    """Reload ``reloaded_owner``'s handlers from ``source``. See the module
    docstring for the three stages and the fail-closed guarantee: every failure
    path leaves ``app.plugins`` on the live generation and claims no rollback."""
    # 1. Fresh generation (the new handlers). The live module is never mutated.
    try:
        gen = load_fresh_generation(module_name, source)
    except Exception as e:  # noqa: BLE001 -- any failure -> live preserved
        return HandlerReloadResult(
            'failed',
            (f'{module_name}: fresh generation failed ({type(e).__name__}); '
             f'live generation preserved, no rollback',))

    # 2. Validated candidate: the fresh manifest swapped in for the reloaded
    #    owner, every other owner re-registered unchanged. Any authority/schema/
    #    duplicate issue keeps the live registry intact.
    manifests = [gen.manifest if m.id == reloaded_owner else m for m in live_manifests]
    staged = stage_candidate(
        app, manifests, live=app.plugins,
        reviewed_owners=reviewed_owners, approved_new_tools=approved_new_tools)
    if not staged.ok:
        kinds = sorted({i.kind for i in staged.issues})
        return HandlerReloadResult(
            'failed',
            (f'{module_name}: candidate rejected {kinds}; live generation preserved',),
            token=gen.token)

    # 3. The idle-bounded, no-rollback swap (eligibility gate inside).
    swap = commit_handler_candidate(
        app, expected=app.plugins, candidate=staged.registry,
        activity=activity, eligible=eligible, swap_seams=swap_seams)
    return HandlerReloadResult(swap.status, swap.reasons,
                               token=gen.token, swapped=swap.swapped)


def render_handler_reload(name: str, result: HandlerReloadResult) -> str:
    """The concise operator line for one handler-generation reload: reloaded /
    unchanged / deferred / failed / restart-required (plus degraded), with
    actionable reasons. Never presents a partial failure as a full reload.
    Consistent in style with ``plugin_reload_ui._render`` (the metadata-only
    path) so the two reload modes read the same way to the operator.

    Pure: formats the result; touches no App state.
    """
    p = "[reload-plugins]"
    if result.status == "swapped":
        return (f"{p} reloaded: {name!r} now runs the new handler generation; the "
                f"next turn sees it. (no rollback -- the new generation is live)")
    if result.status == "no-op":
        return f"{p} unchanged: {name!r} already matches the loaded generation."
    reasons = "; ".join(result.reasons) if result.reasons else "no reason given"
    if result.status == "deferred":
        return (f"{p} deferred: {reasons}. Not retried automatically -- run the "
                f"reload again after the active work finishes.")
    if result.status == "restart-required":
        return f"{p} restart required: {reasons}. Live handlers unchanged."
    if result.status == "degraded":
        return (f"{p} degraded: {reasons}. The new generation is partially live; "
                f"a restart is required. Live handlers were not rolled back.")
    return f"{p} failed: {reasons}. Live handlers unchanged."
