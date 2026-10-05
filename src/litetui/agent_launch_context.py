"""Child bootstrap for explicit agent-owned launches, before mutable startup.

The process owns this context through shutdown; callers do not transfer parent
locks. Merely importing/looking up a context never creates an agent or store.
"""
from __future__ import annotations

from pathlib import Path

from litetui.agent_ownership import AgentSession
from litetui.agent_store import AgentStore, StoreError, valid_id


def validate_execution(session: AgentSession, *, backend=None, model=None, thinking_level=None) -> None:
    """Matching explicit flags are harmless; divergence never becomes authority."""
    authority = session.authority
    for supplied, saved in ((backend, authority.backend), (model, authority.model),
                            (thinking_level, authority.thinking_level)):
        if supplied is not None and supplied != saved:
            raise StoreError('Launch override disagrees with agent settings; edit agent settings explicitly')


def acquire(root: Path, name: str, *, conversation_id: str | None = None,
            backend: str | None = None, model: str | None = None,
            thinking_level: str | None = None) -> AgentSession:
    session = AgentSession.acquire_existing(AgentStore(root), name=name)
    try:
        validate_execution(session, backend=backend, model=model, thinking_level=thinking_level)
        if conversation_id is not None:
            directory = session.conversation_directory(valid_id(conversation_id))
            if not (directory / 'convo.jsonl').is_file():
                raise StoreError('Conversation is absent from the selected agent')
        return session
    except BaseException:
        session.release()
        raise


def create(root: Path, name: str, *, agent_id: str, backend: str,
           model: str, thinking_level: str) -> AgentSession:
    """Child-only fresh creation; never adopt an existing or partial home."""
    return AgentSession.create_fresh(AgentStore(root), name=name, agent_id=agent_id,
                                    backend=backend, model=model, thinking_level=thinking_level)


def ordinary(root: Path, settings, *, spawned_identity=None, notice=print,
             backend=None, model=None, thinking_level=None) -> AgentSession:
    """Open default owned seat; only standalone contention creates a new name."""
    from uuid import uuid4
    from litetui.agent_ownership import OwnershipError
    store = AgentStore(root)
    agents = store.list_agents()  # corrupt/inactive metadata remains fail-closed
    explicit = spawned_identity is not None
    identity, name = (spawned_identity[:2] if explicit else
                      (None, (settings.seat_name or '').strip() or 'LiteTUI'))
    existing = [a for a in agents if a.name.casefold() == name.casefold()]
    if existing:
        if explicit and existing[0].agent_id != valid_id(identity):
            raise StoreError('Spawn identity disagrees with existing agent home')
        try:
            return acquire(root, name, backend=backend, model=model, thinking_level=thinking_level)
        except OwnershipError as exc:
            if explicit or getattr(exc.__cause__, 'errno', None) not in (13, 11, 36):
                raise
    elif store.agent_directory(name).exists():
        raise StoreError('Default home is inactive; operator verification/activation required')
    else:
        try:
            return AgentSession.create_fresh(store, name=name, agent_id=identity or str(uuid4()),
                          backend=backend if backend is not None else settings.backend,
                          model=model if model is not None else settings.default_model,
                          thinking_level=thinking_level or settings.thinking_level, allow_unchosen=not explicit)
        except (FileExistsError, OwnershipError):
            if explicit:
                raise
        except StoreError:
            # Another default reservation can win between catalog read and mkdir.
            if explicit or not store.agent_directory(name).exists():
                raise
    identity = str(uuid4())
    unique = 'LiteTUI-' + identity[:8]
    session = AgentSession.create_fresh(store, name=unique, agent_id=identity,
                     backend=backend if backend is not None else settings.backend,
                     model=model if model is not None else settings.default_model,
                     thinking_level=thinking_level or settings.thinking_level, allow_unchosen=True)
    notice(f'Default agent is busy; opened {unique} in its own home.')
    return session


def apply_settings(session: AgentSession, settings) -> None:
    authority = session.authority
    settings.backend = authority.backend
    settings.default_model = authority.model
    settings.thinking_level = authority.thinking_level
    settings.seat_name = authority.name
    settings.backend_chosen = True
