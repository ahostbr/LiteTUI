"""Install fail-closed local-model admission on a backend, classified by LOCALITY.

Admission gates a load that consumes THIS host's VRAM. Classification is by
LOCALITY, never ownership — the two are independent, and ownership (a spawned
process handle) governs only unload authority, never the admit decision:

- a LOOPBACK endpoint, or an NInfer spawn (always local, no host yet), is LOCAL ->
  the load is admitted; an uncalibrated / unresolvable identity BLOCKS it with an
  actionable reason. Attached-but-local is INCLUDED: driving a load into a local
  engine we did not spawn still consumes this host's capacity.
- an explicitly-marked (trusted runtime/config) REMOTE endpoint is passed through:
  host admission is not applicable and no host-memory coverage is claimed. There
  is no production remote-marker config yet, so in production a non-loopback host
  is UNKNOWN and blocks.
- anything else (UNKNOWN locality, an unknown backend shape, or ANY classification
  error) BLOCKS the load.

codex and every non-`_VramGate` backend never reach here.

Sessions are PER-BACKEND: each backend gets its own ModelResourceSession over the
app's single shared coordinator/owner/store, and the app keeps a registry of them
for the eventual (separate) teardown slice — a backend never inherits another
backend's session. This module does not compute an identity and does not release a
lease: the resolver is injected and returns None today, so local loads block -- an
explicit, actionable diagnostic state, NOT a production-ready admission claim.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlparse

from litetui.llm_backend import LlamaCppBackend, LMStudioBackend, _VramGate
from litetui.ninfer_backend import NInferBackend
from litetui.model_resource_session import AdmissionBlocked

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

#: Per-backend-TYPE endpoint adapters — the exact attr each backend exposes,
#: verified from source (llm_backend/ninfer_backend). host() on LlamaCppBackend is
#: a METHOD, not a property. An unknown _VramGate shape is deliberately absent, so
#: it classifies UNKNOWN -> block.
_ENDPOINT_ADAPTERS = (
    (LlamaCppBackend, lambda b: b.host()),
    (LMStudioBackend, lambda b: getattr(b, "_host", None)),
    (NInferBackend, lambda b: getattr(b, "_host", None)),
)


def _adapter(backend):
    for cls, fn in _ENDPOINT_ADAPTERS:
        if isinstance(backend, cls):
            return fn
    return None


def _hostname(endpoint):
    if not isinstance(endpoint, str) or not endpoint:
        return None
    parsed = urlparse(endpoint if "://" in endpoint else "http://" + endpoint)
    return (parsed.hostname or "").lower() or None


def classify_locality(backend, *, remote_marker=None) -> str:
    """'local' | 'remote' | 'unknown'. ANY error (malformed host, adapter or
    remote_marker raising) fails closed to 'unknown' — a block, never a crash."""
    try:
        adapter = _adapter(backend)
        if adapter is None:
            return "unknown"
        endpoint = adapter(backend)
        if isinstance(backend, NInferBackend) and endpoint is None:
            return "local"
        host = _hostname(endpoint)
        if host in _LOOPBACK:
            return "local"
        if remote_marker is not None and remote_marker(backend):
            return "remote"
        return "unknown"
    except Exception:  # noqa: BLE001 - classification failure must block, not crash
        return "unknown"


def make_admission(backend, session, *, remote_marker=None):
    """The `resource_admission(key, *, reload)` callable a `_VramGate` consumes.

    Only the OUTERMOST vram_guard reaches this (reentrancy short-circuit), so a
    load is classified and reserved once; the reload flag is forwarded unchanged.
    """
    def admission(key, *, reload=False):
        scope = classify_locality(backend, remote_marker=remote_marker)
        if scope == "remote":
            return _passthrough()             # host admission not applicable
        if scope == "unknown":
            return _refuse(
                "BLOCKED: cannot confirm this endpoint is local, so a load here is "
                "refused. Remote-endpoint admission is not supported yet."
            )
        return session.load(key, reload=reload)   # local: gate (uncalibrated blocks)

    return admission


@asynccontextmanager
async def _passthrough():
    yield


def _refuse(reason):
    @asynccontextmanager
    async def refuse():
        raise AdmissionBlocked(reason)
        yield  # unreachable; keeps this a generator for asynccontextmanager
    return refuse()


def install_on(app, backend, *, remote_marker=None) -> bool:
    """Install admission on `backend`, idempotently. Returns True iff it installed.

    - A non-`_VramGate` backend (codex) is left untouched.
    - A `resource_admission` already present is RETAINED, never overwritten or
      chained. In production a fresh backend starts from None, so this only
      preserves a DELIBERATE injection (a test double, or an explicit override);
      the boundary is intentional and documented — this module trusts a
      pre-installed hook and will not second-guess or wrap it.
    - Each backend gets its OWN ModelResourceSession (backend-owned, plus an app
      registry for the eventual teardown slice); no backend inherits another's.
    """
    if not isinstance(backend, _VramGate):
        return False
    if getattr(backend, "resource_admission", None) is not None:
        return False
    session = _new_session(app)
    backend._admission_session = session
    registry = getattr(app, "_admission_sessions", None)
    if registry is None:
        registry = app._admission_sessions = []
    registry.append((backend, session))
    backend.resource_admission = make_admission(backend, session, remote_marker=remote_marker)
    return True


def _default_resolver(key):
    """Temporary: no verified host/build identity source is wired yet, so every
    local model is uncalibrated and blocks. Replaced by an injected async resolver
    once identity acquisition (hashed MachineGuid / engine bytes / artifact) lands.
    """
    return None


def _bundle(app):
    """The app's single shared (coordinator, owner, store), built once and cached.
    A test injects `app._admission_bundle` on a temp store so this never touches
    the real one."""
    bundle = getattr(app, "_admission_bundle", None)
    if bundle is not None:
        return bundle
    from litetui import resource_telemetry, tasks
    from litetui.resource_admission import ResourceCoordinator
    from litetui.resource_identity import ResourceOwner
    from litetui.resource_calibration import CalibrationStore

    coordinator = ResourceCoordinator(telemetry=resource_telemetry.host_snapshot)
    owner = ResourceOwner.current(tasks._INSTANCE_ID).encode()   # real per-process UUID
    bundle = app._admission_bundle = (coordinator, owner, CalibrationStore())
    return bundle


def _new_session(app):
    from litetui.resource_calibration import make_demand_for
    from litetui.model_resource_session import ModelResourceSession

    coordinator, owner, store = _bundle(app)
    return ModelResourceSession(coordinator, owner,
                                demand_for=make_demand_for(store, _default_resolver))
