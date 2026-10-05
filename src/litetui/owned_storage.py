"""Mutable conversation destinations require a borrowed process-owned session."""
from pathlib import Path

from litetui.agent_store import StoreError, _unlinked


def require_conversation(directory, agent_session):
    if agent_session is None:
        raise StoreError('An owned agent session is required; legacy archives are read-only')
    directory = Path(directory)
    expected = agent_session.conversation_directory(directory.name)
    if directory != expected:
        raise StoreError('Conversation destination lies outside the owned agent')
    return _unlinked(expected)
