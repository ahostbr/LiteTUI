"""Owned derivation of a request effort without changing the inherited agent default.

AgentAuthority continues to own backend, model and base thinking. A conversation's
per-model preference can change the request only after a trusted source boundary
captures its independently read durable value. Validation never adopts a new
preference from mutable app state.
"""
from __future__ import annotations

from dataclasses import dataclass

from litetui.llm_backend import BackendError

BACKENDS = frozenset(("lmstudio", "llamacpp", "custom", "ninfer", "strata"))


def selection(settings, model):
    rows = getattr(settings, "model_infer_overrides", {})
    if not isinstance(rows, dict):
        raise BackendError("Invalid owned model effort preferences")
    row = rows.get(model, {})
    if not isinstance(row, dict):
        raise BackendError("Invalid owned model effort preference")
    if "reasoning_effort" not in row:
        return False, None
    value = row["reasoning_effort"]
    if value is not None and (not isinstance(value, str) or not value or len(value) > 64):
        raise BackendError("Invalid owned model effort value")
    return True, value


def resolve(base, selected, invocation=None):
    if invocation is not None:
        return invocation
    present, value = selected
    return ("default" if value is None else value) if present else base


def request_level(app):
    return resolve(app.thinking_level, selection(app.settings, app.model_id),
                   getattr(app, "_cli_effective_thinking", None))


def _graded(settings):
    value = getattr(settings, "lmstudio_graded_thinking_models", ())
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
        raise BackendError("Invalid owned effort mapping")
    return tuple(value)


@dataclass(frozen=True)
class Invocation:
    level: str | None


def _invocation(app):
    actual = getattr(app, "_cli_effective_thinking", None)
    captured = getattr(app, "_owned_effort_invocation", None)
    if captured is not None and (not isinstance(captured, Invocation) or captured.level != actual):
        raise BackendError("Invocation effort changed outside its owned launch")
    return actual


@dataclass(frozen=True)
class Projection:
    session: object
    store: object
    backend: object
    conversation: str
    authority: object
    selected: tuple
    graded: tuple
    invocation: str | None


def _saved(app):
    from litetui import settings_runtime

    session, store = app._agent_session, app.store
    if (store.agent_session is not session or store.convo_id != app.convo_id
            or store.convo_dir != session.conversation_directory(app.convo_id)):
        raise BackendError("Model effort lost its owned conversation")
    return settings_runtime.service_for(app).snapshot(
        app.convo_id, read_only=True, max_bytes=1024 * 1024).saved


def install(app):
    """Called only after owned hydration, explicit selection or verified persistence.

    A half-initialized app has no request projection yet. Its existing base-only
    execution check remains in force; a per-model override requires this proof.
    """
    session = getattr(app, "_agent_session", None)
    backend = getattr(app, "backend", None)
    if session is None or getattr(backend, "name", None) not in BACKENDS:
        return
    authority = session.authority
    if (not getattr(app, "convo_dir", None) or not getattr(app, "store", None)
            or not getattr(app, "model_id", None) or app.model_id != authority.model
            or backend.name != authority.backend):
        return
    saved = _saved(app)
    selected = selection(saved, authority.model)
    if selected != selection(app.settings, authority.model):
        raise BackendError("Saved and active model effort disagree")
    if (saved.thinking_level != authority.thinking_level
            or app.settings.thinking_level != authority.thinking_level
            or (getattr(app, "_thinking_level", None) or "default") != authority.thinking_level):
        raise BackendError("Inherited effort disagrees with owned agent authority")
    if _graded(saved) != _graded(app.settings):
        raise BackendError("Saved and active effort mapping disagree")
    app._owned_effort_projection = Projection(
        session, app.store, backend, app.convo_id, authority, selected,
        _graded(saved), _invocation(app))


def validate(app, actual_wire):
    """Verify both immutable base authority and its exact published derivation."""
    from litetui.turn_engine import TurnEngine

    authority = app._agent_session.authority
    base = authority.thinking_level
    selected = selection(app.settings, authority.model)
    graded = _graded(app.settings)
    invocation = _invocation(app)
    if ((getattr(app, "_thinking_level", None) or "default") != base
            or getattr(app.settings, "thinking_level", base) != base):
        raise BackendError("Inherited effort disagrees with owned agent authority")
    proof = getattr(app, "_owned_effort_projection", None)
    if proof is not None:
        current = Projection(app._agent_session, app.store, app.backend, app.convo_id,
                             authority, selected, graded, invocation)
        if (not isinstance(proof, Projection) or proof != current
                or proof.session is not app._agent_session or proof.store is not app.store
                or proof.backend is not app.backend):
            raise BackendError("Effective request effort changed outside its owned projection")
        saved = _saved(app)
        if (selection(saved, authority.model) != proof.selected
                or _graded(saved) != proof.graded or saved.thinking_level != base):
            raise BackendError("Durable model effort changed outside its owned projection")
    elif selected[0]:
        raise BackendError("Per-model effort lacks its owned execution projection")
    expected = TurnEngine.resolve_reasoning_effort(
        resolve(base, selected, invocation), authority.backend, authority.model, graded)
    if actual_wire != expected:
        raise BackendError("Effective request effort disagrees with owned execution projection")
