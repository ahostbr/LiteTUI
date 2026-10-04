"""Child bootstrap for explicit agent-owned launches, before mutable startup.

The process owns this context through shutdown; callers do not transfer parent
locks. Merely importing/looking up a context never creates an agent or store.
"""
from __future__ import annotations

from pathlib import Path

from litetui.agent_ownership import AgentSession
from litetui.agent_store import AgentStore, StoreError, valid_id


def acquire(root: Path, name: str, *, conversation_id: str | None = None,
            backend: str | None = None, model: str | None = None,
            thinking_level: str | None = None) -> AgentSession:
    session = AgentSession.acquire_existing(AgentStore(root), name=name)
    try:
        authority = session.authority
        # Explicit matching flags are harmless; divergent flags must not silently
        # override folder truth or mutate persistent settings during a launch.
        for supplied, saved in ((backend, authority.backend), (model, authority.model),
                                (thinking_level, authority.thinking_level)):
            if supplied is not None and supplied != saved:
                raise StoreError('Launch override disagrees with agent settings; edit agent settings explicitly')
        if conversation_id is not None:
            directory = session.conversation_directory(valid_id(conversation_id))
            if not (directory / 'convo.jsonl').is_file():
                raise StoreError('Conversation is absent from the selected agent')
        return session
    except BaseException:
        session.release()
        raise


def apply_settings(session: AgentSession, settings) -> None:
    authority = session.authority
    settings.backend = authority.backend
    settings.default_model = authority.model
    settings.thinking_level = authority.thinking_level
    settings.seat_name = authority.name
    settings.backend_chosen = True
