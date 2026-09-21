"""Install fail-closed local-model admission on a backend, classified by LOCALITY.

Admission gates a load that consumes THIS host's VRAM. Classification is by
LOCALITY, never ownership — the two are independent, and ownership (a spawned
process handle) governs only unload authority, never the admit decision:

- a LOOPBACK endpoint, or an NInfer spawn (which is always local and has no host
  yet), is LOCAL -> the load is admitted; an uncalibrated / unresolvable identity
  BLOCKS it with an actionable reason. Attached-but-local is INCLUDED: driving a
  load into a local engine we did not spawn still consumes this host's capacity.
- an explicitly-marked (trusted runtime/config) REMOTE endpoint is passed through:
  host admission is not applicable and no host-memory coverage is claimed.
- anything else (UNKNOWN locality, or an unknown backend shape) BLOCKS the load.

codex and every non-`_VramGate` backend never reach here.

This module does not compute an identity and does not release a lease. The
resolver is injected; today it returns None, so local loads block -- an explicit,
actionable diagnostic state, NOT a production-ready admission claim. Teardown /
release / async identity acquisition are separate, coordinated slices.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlparse

from litetui.llm_backend import LlamaCppBackend, LMStudioBackend, _VramGate
from litetui.ninfer_backend import NInferBackend
from litetui.model_resource_session import AdmissionBlocked

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

#: Per-backend-TYPE endpoint adapters — the exact attr each backend exposes,
#: verified from source (llm_backend/ninfer_backend). An unknown _VramGate shape
#: is deliberately absent, so it classifies UNKNOWN -> block.
_ENDPOINT_ADAPTERS = (
    (LlamaCppBackend, lambda b: b.host()),                     # method: _attached_host or _host
    (LMStudioBackend, lambda b: getattr(b, "_host", None)),    # settings.lm_host
    (NInferBackend, lambda b: getattr(b, "_host", None)),      # _owned.host, or None pre-spawn
)


def _adapter(backend):
    for cls, fn in _ENDPOINT_ADAPTERS:
        if isinstance(backend, cls):
            return fn
    return None


def _hostname(endpoint):
    if not endpoint:
        return None
    parsed = urlparse(endpoint if "://" in endpoint else "http://" + endpoint)
    return (parsed.hostname or "").lower() or None


def classify_locality(backend, *, remote_marker=None) -> str:
    """'local' | 'remote' | 'unknown'.

    remote_marker is a trusted runtime/config predicate (never a model tool arg);
    there is no such production setting yet, so without an injected marker a
    non-loopback endpoint is UNKNOWN and blocks.
    """
    adapter = _adapter(backend)
    if adapter is None:
        return "unknown"                      # unknown backend shape -> block
    endpoint = adapter(backend)
    if isinstance(backend, NInferBackend) and endpoint is None:
        return "local"                        # pre-spawn: NInfer only ever spawns locally
    host = _hostname(endpoint)
    if host in _LOOPBACK:
        return "local"
    if remote_marker is not None and remote_marker(backend):
        return "remote"
    return "unknown"


def make_admission(backend, session, *, remote_marker=None):
    """The `resource_admission(key, *, reload=)` callable a `_VramGate` consumes.

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
                "refused. Point the backend at a local engine, or mark the remote "
                "endpoint explicitly in config."
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
    - An already-installed admission is RETAINED (never silently overwritten or
      chained): a re-assignment of the same or a rebuilt backend does not clobber
      a live session. An intentional replace is a caller's explicit choice, not a
      side effect of this call.
    """
    if not isinstance(backend, _VramGate):
        return False
    if getattr(backend, "resource_admission", None) is not None:
        return False
    backend.resource_admission = make_admission(backend, _session_for(app),
                                                remote_marker=remote_marker)
    return True


def _default_resolver(key):
    """Temporary: no verified host/build identity source is wired yet, so every
    local model is uncalibrated and blocks. Replaced by an injected async resolver
    once identity acquisition (hashed MachineGuid / engine bytes / artifact) lands.
    """
    return None


def _session_for(app):
    """The App's single admission session, built lazily and cached. Tests inject
    `app._resource_session` on a temp store so this never touches the real one."""
    session = getattr(app, "_resource_session", None)
    if session is not None:
        return session
    from litetui import resource_telemetry
    from litetui.resource_admission import ResourceCoordinator
    from litetui.resource_identity import ResourceOwner
    from litetui.resource_calibration import CalibrationStore, make_demand_for
    from litetui.model_resource_session import ModelResourceSession

    coordinator = ResourceCoordinator(telemetry=resource_telemetry.host_snapshot)
    owner = ResourceOwner.current(getattr(app, "instance_id", None) or "litetui").encode()
    demand_for = make_demand_for(CalibrationStore(), _default_resolver)
    session = ModelResourceSession(coordinator, owner, demand_for=demand_for)
    app._resource_session = session
    return session
