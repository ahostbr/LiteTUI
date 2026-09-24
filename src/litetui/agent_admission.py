"""Local-vs-hosted launch admission: the capacity-reservation boundary.

`launch_for_app` and `prepare_child` today refuse everything that is not a
hosted, headless, isolated-worktree launch with one opaque string. This module
turns that refusal into a *decision* -- a typed verdict with a distinct
reason per case -- and opens the seam where WS3's calibrated capacity resolver
will verdict a local load.

A local engine load (lmstudio / llamacpp / ninfer) consumes VRAM/RAM that
belongs to the parent host. Admitting one without a verified reservation is
exactly the "silent eviction / optimistic loading" the spawn contract forbids,
so the gate refuses a local request UNLESS the caller hands it a verified
reservation. Under the current state nothing verified exists -- the composed
services pass no reservation, so every local request is BLOCKED. This module
never invents a reservation itself; it only honours one a caller has verified.
"""
from dataclasses import dataclass

_HOSTED = 'codex'
_LOCAL = frozenset({'lmstudio', 'llamacpp', 'ninfer'})


@dataclass(frozen=True)
class Admission:
    """Typed launch-admission verdict.

    A refusal always carries a user-facing `reason` (the contract's "BLOCKED
    with ... options") and a `category` naming the decision. An admission
    leaves both `None`.
    """
    admitted: bool
    reason: str = None
    category: str = None


def classify_backend(backend):
    """`codex` is hosted (its model lives outside this host); the other three
    backends load an engine ON this host; anything else is not integrated."""
    if backend == _HOSTED:
        return 'hosted'
    if backend in _LOCAL:
        return 'local'
    return 'unknown'


def admit_launch(spec, *, reservation=None):
    """Decide launch admission WITHOUT loading anything.

    `reservation` is the WS3 capacity-reservation verdict for a LOCAL load --
    an object a caller holds only after its calibrated resolver verified enough
    headroom and recorded a claim. `None` (the default, and the only state the
    composed services currently reach) means no verified reservation exists, so
    a local request is refused with a capacity reason. A hosted request needs no
    reservation. Headed and explicit-workspace launches are refused regardless
    of backend: neither integration is wired.
    """
    kind = classify_backend(spec.backend)
    if kind == 'local':
        if reservation is None:
            return Admission(False,
                             'Local backend requires a verified resource '
                             'reservation; none is available. BLOCKED: no '
                             'silent eviction or backend fallback.',
                             'local-capacity')
    elif kind != 'hosted':
        return Admission(False,
                         f'Backend {spec.backend!r} is not integrated',
                         'unknown-backend')
    if spec.headed:
        return Admission(False, 'Headed launch is not integrated', 'headed')
    if spec.workspace_mode != 'worktree':
        return Admission(False, 'Explicit-workspace mode is not integrated',
                         'explicit-workspace')
    return Admission(True)
