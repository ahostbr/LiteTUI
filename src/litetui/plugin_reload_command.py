"""App-state assembly for the handler-generation reload (WS7 full scope) -- the
"command wiring" the orchestration unit (``plugin_reload_handler``) leaves as
App state.

``reload_handler_generation`` takes everything injected: the fresh source, the
live manifests, the per-owner eligibility, and the swap seams. This module is
where that App state is assembled for one command invocation:

  * resolve the plugin's declared reload compatibility + on-disk source from the
    live app (``resolve`` -- injected so the assembly is testable fake-only; the
    production resolver lives in ``plugin_reload_ui._resolve_reload``);
  * classify the owner's eligibility -- the HOST decides ``reviewed`` (a plugin
    cannot review itself); a declaration alone never makes a plugin eligible;
  * build the swap seams from the declared lifecycle seams; and
  * drive the no-rollback orchestration. The concise operator line comes from
    ``render_handler_reload``.

TRUST MODEL (default-to-restart-required): a plugin opts in by declaring a
module-level ``RELOAD_COMPATIBLE`` = a ``ReloadCompatibility`` (its stateless /
owns facts + the four lifecycle seams). That declaration is NECESSARY but not
SUFFICIENT: eligibility is still ``reviewed AND stateless AND owns=() AND
complete seams`` (``plugin_reload_eligibility``). An owner that declares nothing
is reported restart-required before any seam runs; an unreviewed declaration is
restart-required because a plugin cannot self-review.

FAKE-ONLY boundary: nothing here imports/execs a real plugin or touches
App-owned resources. ``handler_reload`` reads the App only through the injected
``resolve``, ``live_manifests`` and ``activity`` -- the only mutation on success
is the registry pointer swap performed by the no-rollback orchestration. Every
failure path leaves ``app.plugins`` on the live generation and claims no
rollback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

from litetui.plugin_reload_eligibility import (
    LifecycleSeams,
    assess_reload_eligibility,
)
from litetui.plugin_reload_handler import (
    HandlerReloadResult,
    reload_handler_generation,
)
from litetui.plugin_reload_swap import SwapSeams


@dataclass(frozen=True)
class ReloadCompatibility:
    """What a plugin module declares (module-level ``RELOAD_COMPATIBLE``) to be
    handler-reload-eligible.

    ``stateless`` -- the handler mutates no retained / process-global state a
                  swap would have to roll back.
    ``owns``      -- the side-effect resource classes it owns (timers /
                  processes / mcp / children); non-empty disqualifies (no safe
                  teardown across a swap in this scope).
    ``seams``     -- the four lifecycle seams; all must be present for the swap
                  to bracket the change (validate before, deactivate + join old,
                  activate new). ``prepare`` maps to the fresh generation load.

    The host does NOT read ``reviewed`` from this -- that is a host allowlist
    decision, never a self-review.
    """

    stateless: bool
    owns: tuple[str, ...] = ()
    seams: LifecycleSeams = field(default_factory=LifecycleSeams)


@dataclass(frozen=True)
class ReloadTarget:
    """The App-state resolved for one handler reload: the plugin's on-disk fresh
    source plus its declared compatibility. Produced by the injected ``resolve``
    (the production resolver reads a real module's ``__file__`` + declaration)."""

    owner: str
    module_name: str
    source: str
    compat: ReloadCompatibility


ResolveFn = Callable[[str], ReloadTarget | None]


def handler_reload(
    app: Any,
    target: str,
    *,
    resolve: ResolveFn,
    live_manifests,
    reviewed_owners: frozenset[str],
    activity: Callable[[], Any],
    approved_new_tools: frozenset[str] = frozenset(),
) -> HandlerReloadResult:
    """Assemble and run one handler-generation reload for ``target``.

    ``resolve(target)`` is the App-state seam: it returns the plugin's on-disk
    fresh source + declared ``ReloadCompatibility`` (a ``ReloadTarget``), or
    ``None`` when the plugin declares no handler-reload compatibility -- the
    default, reported restart-required before any seam runs. ``reviewed_owners``
    is the host allowlist (a plugin cannot review itself); ``live_manifests`` is
    the registered ``PluginManifest`` list; ``activity`` is the idle-boundary
    snapshot callable. Eligibility is assessed here and the swap seams are built
    from the declared lifecycle seams; the swap itself is the no-rollback
    ``reload_handler_generation``.
    """
    t = resolve(target)
    if t is None:
        return HandlerReloadResult(
            'restart-required',
            (f'{target}: no handler-reload compatibility declared; restart required',))

    elig = assess_reload_eligibility(
        t.owner,
        reviewed=(t.owner in reviewed_owners),
        stateless=t.compat.stateless,
        owns=t.compat.owns,
        seams=t.compat.seams)
    if not elig.eligible:
        return HandlerReloadResult('restart-required', elig.reasons)

    # The eligible set is stateless + owns nothing, so the seams bracket the
    # change (validate -> deactivate + join old -> activate new) even though the
    # old generation holds no resources to join.
    eligible = {t.owner: True}
    swap_seams = {t.owner: SwapSeams(
        validate_new=t.compat.seams.validate,
        deactivate_old=t.compat.seams.deactivate,
        activate_new=t.compat.seams.activate)}
    return reload_handler_generation(
        app, module_name=t.module_name, source=t.source,
        live_manifests=live_manifests, reloaded_owner=t.owner,
        reviewed_owners=reviewed_owners, eligible=eligible,
        swap_seams=swap_seams, activity=activity,
        approved_new_tools=approved_new_tools)
