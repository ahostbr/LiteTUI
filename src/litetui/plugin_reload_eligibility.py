"""Plugin handler-reload eligibility + lifecycle-seam contract.

This is the safe PRECURSOR to the handler-generation swap (WS7 full scope,
sub-plugin-reload.md implementation slices "reload compatibility metadata" and
"prepare/validate/activate/deactivate seams"). The bounded slice
(``plugin_reload_commit.commit_metadata_candidate``) deliberately REFUSES to
swap handlers (``old.run is not new.run`` -> restart-required); the later,
coordinated swap slice will consult this module to decide which plugins MAY swap
and which seams to call.

CONTRACTUAL PURITY, NOT A SANDBOX. This module is pure data + policy: it does
NOT import or reload modules, does NOT call a plugin's ``activate()``/
``deactivate()``, and does NOT tear down resources. It only classifies whether a
plugin *could* be swapped, given its declared characteristics. The risky part --
actually deactivating/joining owned resources and swapping handler generations at
an idle boundary -- is a separate, later slice and is deliberately NOT done here.

POLICY: default-to-restart-required. A plugin is handler-reload-eligible ONLY if
every one of these holds; any failure yields ``eligible=False`` with the failing
reason(s), and an unknown / unreviewed owner is restart-required BEFORE anything
about it is trusted. Never optimistic.

  * reviewed   -- the owner is on the reviewed pure-register allowlist (the same
                  gate ``plugin_reload.stage_candidate`` uses; unreviewed owners
                  are never trusted to have a side-effect-free register/activate).
  * stateless  -- the handler mutates no retained or process-global state that a
                  swap would have to roll back (a dishonest/unknown mutation is
                  unrollbackable, so a stateful handler is never eligible).
  * owns=()    -- it owns no side-effect resources (timers, processes, MCP
                  connections, durable children) that would leak or orphan across
                  a swap; any owned resource class disqualifies it here.
  * seams      -- it declares all four lifecycle seams (prepare / validate /
                  activate / deactivate) so the later swap slice can bracket the
                  change: validate before, deactivate+join old, activate new.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable


@dataclass(frozen=True)
class LifecycleSeams:
    """The four seams a handler-reload-eligible plugin must expose.

    ``prepare``  -- the plugin can (re)load cleanly for this generation (no I/O).
    ``validate`` -- a pre-swap safety check; returning a problem refuses the swap.
    ``activate`` -- bring this generation's handlers/resources online.
    ``deactivate`` -- take the OLD generation offline and JOIN its owned resources
                      so nothing (timer, process, connection, child) leaks.

    All are ``None`` unless the plugin declares them. A handler-reload-eligible
    plugin MUST provide all four: a swap cannot bracket the change without a
    deactivate/activate pair, and cannot prove safety without a validate gate.
    """

    prepare: Callable[[], object] | None = None
    validate: Callable[[], object] | None = None
    activate: Callable[[], object] | None = None
    deactivate: Callable[[], object] | None = None

    @property
    def complete(self) -> bool:
        return all((self.prepare, self.validate, self.activate, self.deactivate))

    def missing(self) -> tuple[str, ...]:
        return tuple(n for n, v in (
            ("prepare", self.prepare),
            ("validate", self.validate),
            ("activate", self.activate),
            ("deactivate", self.deactivate),
        ) if v is None)


@dataclass(frozen=True)
class ReloadEligibility:
    """One plugin's handler-reload eligibility assessment.

    ``eligible`` is True ONLY when the plugin is a reviewed, stateless,
    resource-free handler that declares all four lifecycle seams. ``reasons``
    names every failed gate (empty when eligible). ``owns`` records the resource
    classes the plugin declared (timers/processes/mcp/children) for the later
    teardown slice to join.
    """

    owner: str
    eligible: bool
    reasons: tuple[str, ...] = ()
    owns: tuple[str, ...] = ()
    seams: LifecycleSeams = field(default_factory=LifecycleSeams)


def assess_reload_eligibility(
    owner: str,
    *,
    reviewed: bool,
    stateless: bool = True,
    owns: tuple[str, ...] = (),
    seams: LifecycleSeams = LifecycleSeams(),
) -> ReloadEligibility:
    """Classify one plugin's handler-reload eligibility. Default-to-restart-required.

    ``eligible`` is ``reviewed AND no blocking gate failed``. An unreviewed owner
    is restart-required outright (its register/activate is never trusted to be
    pure). A stateful handler, any owned side-effect resource, or an incomplete
    seam set each disqualify independently and are all reported.
    """
    reasons: list[str] = []
    if not reviewed:
        reasons.append("not in reviewed pure-register allowlist (default restart-required)")
    if not stateless:
        reasons.append("stateful (mutates retained/process-global state); no safe rollback")
    if owns:
        reasons.append(f"owns side-effect resources {sorted(owns)}; cannot be torn down across a swap")
    missing = seams.missing()
    if missing:
        reasons.append(f"lifecycle seams incomplete (missing: {', '.join(missing)})")
    eligible = reviewed and not reasons
    return ReloadEligibility(owner=owner, eligible=eligible,
                             reasons=tuple(reasons), owns=tuple(owns), seams=seams)
